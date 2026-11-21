#!/usr/bin/env python3
"""
O'Neal Product Import Script

Imports products from oneal-api into storage-api with:
- External reference mode (no file copies)
- Tenant ID: "oneal"
- AI-generated embeddings in tenant_oneal_knowledge collection

Usage:
    python import_oneal_products.py [--limit=N] [--dry-run] [--skip-embeddings]
"""

import sys
import os
import asyncio
import argparse
from pathlib import Path
from typing import List, Dict, Optional
import httpx

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from sqlalchemy.orm import Session
from models import StorageObject, User
from database import get_db, database
from knowledge_graph.pipeline import kg_pipeline


ONEAL_API_BASE = os.getenv("ONEAL_API_BASE", "https://oneal-api.arkturian.com")
ONEAL_API_KEY = os.getenv("ONEAL_API_KEY", "")
STORAGE_API_KEY = os.getenv("API_KEY", "Inetpass1")
TENANT_ID = "oneal"


async def fetch_oneal_products(limit: Optional[int] = None) -> List[Dict]:
    """Fetch all products from O'Neal API."""
    print(f"\n📥 Fetching O'Neal products from {ONEAL_API_BASE}...")

    all_products = []
    offset = 0
    batch_size = 50

    headers = {"X-API-KEY": ONEAL_API_KEY}
    async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
        while True:
            url = f"{ONEAL_API_BASE}/v1/products?limit={batch_size}&offset={offset}"
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()

            products = data.get("results", [])
            if not products:
                break

            all_products.extend(products)
            print(f"   Fetched {len(all_products)} products...")

            if limit and len(all_products) >= limit:
                all_products = all_products[:limit]
                break

            offset += batch_size

            # Check if we've fetched all
            if len(products) < batch_size:
                break

    print(f"   ✅ Total products: {len(all_products)}")
    return all_products


def create_storage_object_from_product(product: Dict, owner_user: User, db: Session) -> StorageObject:
    """Create a StorageObject from O'Neal product data."""

    # Extract product info
    product_id = product.get("id", "unknown")
    product_name = product.get("name", "Unnamed Product")
    brand = product.get("brand", "O'Neal")
    category = product.get("category", [])
    price_info = product.get("price", {})
    media = product.get("media", [])

    # Get hero image
    hero_image = None
    for img in media:
        if img.get("role") == "hero":
            hero_image = img
            break
    if not hero_image and media:
        hero_image = media[0]

    image_url = hero_image.get("src") if hero_image else None
    if not image_url:
        print(f"   ⚠️  Product {product_id} has no image, skipping")
        return None

    # Create StorageObject
    obj = StorageObject(
        owner_user_id=owner_user.id,
        object_key=f"oneal_{product_id}",
        original_filename=f"{product_id}.jpg",
        file_url=image_url,  # Will be proxied via storage-api
        mime_type="image/jpeg",
        file_size_bytes=0,  # Unknown for external references
        checksum="",  # Not applicable for external references
        is_public=True,
        context="oneal_product",
        collection_id="oneal_catalog",
        link_id=product_id,
        title=product_name,
        description=f"{brand} - {product_name}",
        tenant_id=TENANT_ID,
        storage_mode="external",  # Don't copy files, just reference them
        external_uri=image_url,
        ai_context_metadata={
            "product_data": {
                "product_id": product_id,
                "name": product_name,
                "brand": brand,
                "category": category,
                "price": price_info,
                "product_url": product.get("meta", {}).get("product_url", "")
            },
            "embedding_info": {
                "embeddingText": f"{brand} {product_name}. Categories: {', '.join(category) if isinstance(category, list) else category}. Price: {price_info.get('formatted', 'N/A')}. High-quality motocross and mountain biking gear.",
                "source": "oneal_api_import"
            }
        }
    )

    # Add AI tags for searchability
    obj.ai_tags = [brand] + (category if isinstance(category, list) else [category])
    obj.ai_title = product_name
    obj.ai_category = category[0] if isinstance(category, list) and category else str(category)

    return obj


async def import_products(products: List[Dict], db: Session, skip_embeddings: bool = False, dry_run: bool = False):
    """Import products into storage-api."""
    print(f"\n🚀 Importing {len(products)} products to storage-api (tenant: {TENANT_ID})")

    # Get or create system user for O'Neal products
    system_user = db.query(User).filter(User.email == "oneal@system").first()
    if not system_user:
        from auth import generate_api_key
        system_user = User(
            email="oneal@system",
            display_name="O'Neal Product System",
            password_hash="",
            api_key=generate_api_key(),
            trust_level="admin",
            device_ids=[]
        )
        db.add(system_user)
        db.commit()
        db.refresh(system_user)
        print(f"   ✅ Created system user: {system_user.email}")

    imported_count = 0
    skipped_count = 0
    embedding_count = 0

    for idx, product in enumerate(products, 1):
        product_id = product.get("id", f"unknown_{idx}")

        try:
            # Check if already exists
            existing = db.query(StorageObject).filter(
                StorageObject.link_id == product_id,
                StorageObject.tenant_id == TENANT_ID
            ).first()

            if existing:
                print(f"   [{idx}/{len(products)}] ⏭️  {product_id} already exists (ID: {existing.id})")
                skipped_count += 1
                continue

            # Create storage object
            storage_obj = create_storage_object_from_product(product, system_user, db)
            if not storage_obj:
                skipped_count += 1
                continue

            if dry_run:
                print(f"   [{idx}/{len(products)}] [DRY RUN] Would import: {product_id} - {storage_obj.title}")
                imported_count += 1
                continue

            # Save to database
            db.add(storage_obj)
            db.commit()
            db.refresh(storage_obj)
            print(f"   [{idx}/{len(products)}] ✅ Imported: {storage_obj.id} - {storage_obj.title}")
            imported_count += 1

            # Generate embedding
            if not skip_embeddings:
                try:
                    kg_entry = await kg_pipeline.process_storage_object(storage_obj, db)
                    if kg_entry:
                        print(f"      🔢 Generated embedding in tenant_oneal_knowledge")
                        embedding_count += 1
                    else:
                        print(f"      ⚠️  No embedding created (may need manual review)")
                except Exception as e:
                    print(f"      ❌ Embedding failed: {e}")

        except Exception as e:
            print(f"   [{idx}/{len(products)}] ❌ Error importing {product_id}: {e}")
            import traceback
            traceback.print_exc()
            continue

    print(f"\n📊 Import Summary:")
    print(f"   Imported: {imported_count}")
    print(f"   Skipped: {skipped_count}")
    if not skip_embeddings:
        print(f"   Embeddings: {embedding_count}")


async def main():
    parser = argparse.ArgumentParser(description="Import O'Neal products to storage-api")
    parser.add_argument('--limit', type=int, help='Limit number of products to import')
    parser.add_argument('--dry-run', action='store_true', help='Show what would be imported without importing')
    parser.add_argument('--skip-embeddings', action='store_true', help='Skip embedding generation')

    args = parser.parse_args()

    print("=" * 80)
    print("🏍️  O'Neal Product Import")
    print("=" * 80)
    print(f"Source API:  {ONEAL_API_BASE}")
    print(f"Tenant ID:   {TENANT_ID}")
    print(f"Mode:        {'DRY RUN' if args.dry_run else 'LIVE IMPORT'}")
    print(f"Embeddings:  {'Disabled' if args.skip_embeddings else 'Enabled'}")
    if args.limit:
        print(f"Limit:       {args.limit} products")
    print("=" * 80)

    # Connect to database
    await database.connect()

    try:
        # Fetch products
        products = await fetch_oneal_products(limit=args.limit)

        if not products:
            print("\n❌ No products found")
            return 1

        # Import products
        db = next(get_db())
        await import_products(products, db, skip_embeddings=args.skip_embeddings, dry_run=args.dry_run)

        if args.dry_run:
            print("\n💡 This was a dry run. Re-run without --dry-run to perform actual import.")
        else:
            print("\n✅ Import completed!")
            print(f"\n🔍 Check embeddings: tenant_oneal_knowledge collection")

    finally:
        await database.disconnect()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
