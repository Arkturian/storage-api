#!/usr/bin/env python3
"""
Import O'Neal product images into storage as external references.

This script:
1. Fetches products from O'Neal API
2. Downloads image bytes for AI analysis
3. Creates storage objects with storage_mode="external" (no file copy)
4. Triggers AI analysis and knowledge graph embeddings
5. Returns proxy URLs for accessing images
"""

import asyncio
import httpx
import os
import sys
from pathlib import Path

# Add parent directory to path to import from artrack
sys.path.insert(0, str(Path(__file__).parent.parent))

from database import get_db
from storage.domain import save_file_and_record


ONEAL_API_URL = "https://oneal-api.arkturian.com/v1/products"
ONEAL_API_KEY = os.environ.get("ONEAL_API_KEY", "")  # #3597: never a literal key in the repo


async def fetch_oneal_products(limit: int = 10):
    """Fetch products from O'Neal API."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            f"{ONEAL_API_URL}?format=resolved&limit={limit}",
            headers={"X-API-Key": ONEAL_API_KEY}
        )
        response.raise_for_status()
        data = response.json()
        return data.get("results", []), data.get("count", 0)


async def download_image_bytes(image_url: str) -> bytes:
    """Download image bytes for AI analysis."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(image_url)
        response.raise_for_status()
        return response.content


async def import_product_images(limit: int = 10, skip_ai: bool = False):
    """
    Import O'Neal product images as external references.

    Args:
        limit: Number of products to import
        skip_ai: Skip AI analysis (useful for testing)
    """
    print(f"🔍 Fetching {limit} products from O'Neal API...")
    products, total_count = await fetch_oneal_products(limit)

    print(f"✅ Found {len(products)} products (out of {total_count} total)\n")

    db = next(get_db())
    imported = []

    for idx, product in enumerate(products, 1):
        product_id = product.get("id")
        name = product.get("name", "Unknown")
        brand = product.get("brand", "")
        season = product.get("season", "")
        category = product.get("category", [])
        if isinstance(category, list):
            category = " > ".join(category)

        # Extract hero image URL from media.hero.variants
        media = product.get("media", {})
        hero = media.get("hero", {})
        variants = hero.get("variants", {})
        image_url = variants.get("preview") or variants.get("thumb") or variants.get("print")

        if not image_url:
            print(f"⚠️  [{idx}/{len(products)}] Skipping {name} - no image URL")
            continue

        print(f"📦 [{idx}/{len(products)}] Processing: {name} ({brand} {season})")
        print(f"   Image: {image_url}")

        try:
            # Download image bytes for AI analysis
            print(f"   ⬇️  Downloading image bytes...")
            image_data = await download_image_bytes(image_url)
            print(f"   ✅ Downloaded {len(image_data)} bytes")

            # Extract filename from URL
            filename = image_url.split("/")[-1]
            if not filename or "?" in filename:
                filename = f"{product_id}_{name.replace(' ', '_')}.jpg"

            # Create AI context metadata
            ai_context = {
                "product_id": product_id,
                "brand": brand,
                "season": season,
                "category": category,
                "name": name,
                "source": "oneal_api",
                "original_url": image_url,
                "price": product.get("price", {}),
                "product_url": product.get("meta", {}).get("product_url", "")
            }

            # Save as external reference (no file copy, but AI analysis runs)
            print(f"   💾 Creating storage object with external reference...")
            storage_obj = await save_file_and_record(
                db=db,
                owner_user_id=8,  # O'Neal user ID
                data=image_data,  # Needed for AI analysis
                original_filename=filename,
                context=f"oneal_product_{product_id}",
                is_public=True,
                title=name,
                description=f"{brand} {season} - {category}",
                storage_mode="external",  # Don't store file, use proxy
                external_uri=image_url,  # Original O'Neal URL
                ai_context_metadata=ai_context,
                tenant_id="oneal"
            )

            # Queue AI analysis unless skipped
            if not skip_ai:
                from storage.service import enqueue_ai_safety_and_transcoding
                print(f"   🤖 Queueing AI analysis and embeddings...")
                await enqueue_ai_safety_and_transcoding(storage_obj, db, skip_ai_safety=False)

            proxy_url = storage_obj.file_url
            print(f"   ✅ Created storage object #{storage_obj.id}")
            print(f"   🔗 Proxy URL: {proxy_url}")

            imported.append({
                "id": storage_obj.id,
                "product_id": product_id,
                "name": name,
                "proxy_url": proxy_url,
                "original_url": image_url
            })

            print()

        except Exception as e:
            print(f"   ❌ Error: {e}")
            print()
            continue

    db.close()

    print("\n" + "="*60)
    print(f"✅ Import completed: {len(imported)}/{len(products)} products imported")
    print("="*60)

    if imported:
        print("\nImported products:")
        for item in imported:
            print(f"  • {item['name']} (ID: {item['id']})")
            print(f"    Proxy: {item['proxy_url']}")

    return imported


async def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Import O'Neal products into storage")
    parser.add_argument("--limit", type=int, default=10, help="Number of products to import (default: 10)")
    parser.add_argument("--skip-ai", action="store_true", help="Skip AI analysis (faster for testing)")

    args = parser.parse_args()

    print("="*60)
    print("O'NEAL PRODUCT IMAGE IMPORT")
    print("="*60)
    print(f"Settings:")
    print(f"  • API: {ONEAL_API_URL}")
    print(f"  • Limit: {args.limit} products")
    print(f"  • AI Analysis: {'DISABLED' if args.skip_ai else 'ENABLED'}")
    print(f"  • Storage Mode: external (proxy only, no file copy)")
    print("="*60 + "\n")

    try:
        await import_product_images(limit=args.limit, skip_ai=args.skip_ai)
    except KeyboardInterrupt:
        print("\n\n⚠️  Import cancelled by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n\n❌ Fatal error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
