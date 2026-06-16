"""
Knowledge Graph Pipeline

Orchestrates the process of creating knowledge graph entries from storage objects.

Pipeline flow:
1. Check if object already has embeddings (delete if exists - UPDATE STRATEGY)
2. Generate embedding text from AI tags and context
3. Create embedding vector using OpenAI
4. Store embedding in ChromaDB
5. (Future) Extract entities and relations
"""

from typing import Optional, Dict, Any, List
from sqlalchemy.orm import Session
import json

from models import StorageObject, User
from knowledge_graph.models import EmbeddingVector, KnowledgeGraphEntry
from knowledge_graph.embedding_service import embedding_service
from knowledge_graph.vector_store import vector_store, get_vector_store, VectorStore
from ai_analysis.uri_handler import select_best_uri, is_valid_url

import contextlib
import io


@contextlib.contextmanager
def _kg_debug_log():
    """Best-effort debug log. Must NEVER crash the embedding pipeline — the
    hardcoded /tmp path may be owned by another user (e.g. uvicorn=www-data vs
    the celery worker=root), so a write here previously raised PermissionError
    and aborted the whole embedding. Falls back to an in-memory sink."""
    f = None
    try:
        f = open("/tmp/kg_pipeline_debug.log", "a")
    except Exception:
        f = io.StringIO()
    try:
        yield f
    finally:
        try:
            f.close()
        except Exception:
            pass


class KnowledgeGraphPipeline:
    """
    Orchestrates knowledge graph creation for storage objects.

    This is the main entry point for processing storage objects into
    the knowledge graph.
    """

    def __init__(self):
        self.embedding_service = embedding_service
        self.vector_store = vector_store  # Legacy default store
        # Note: Use get_vector_store(tenant_id) for tenant-specific operations

    async def process_storage_object(
        self,
        storage_obj: StorageObject,
        db: Session
    ) -> Optional[KnowledgeGraphEntry]:
        """
        Process a storage object and create/update its knowledge graph entry.

        **TENANT ISOLATION**: Uses tenant-specific vector store collection based on
        storage_obj.tenant_id for hard database-level isolation.

        **UPDATE STRATEGY**: If the object already has an embedding, it will be
        deleted before creating a new one. This ensures we don't have stale
        embeddings when content is re-uploaded or re-analyzed.

        **MULTI-EMBEDDING SUPPORT**: If AI returns embeddingsList, creates multiple
        embeddings (e.g., one per CSV row). Otherwise creates single embedding.

        Steps:
        1. Get tenant-specific vector store
        2. Delete existing embedding(s) (if any)
        3. Check if AI returned embeddingsList (multi-embedding mode)
        4. Generate embedding vector(s) from AI results
        5. Store in tenant's ChromaDB collection
        6. Return knowledge graph entry

        Args:
            storage_obj: StorageObject with AI analysis results (includes tenant_id)
            db: Database session

        Returns:
            KnowledgeGraphEntry or None if processing failed
        """
        tenant_id = storage_obj.tenant_id or "arkturian"
        print(f"📊 KG Pipeline: Processing object {storage_obj.id} for tenant '{tenant_id}'")

        # Get tenant-specific vector store for hard isolation
        tenant_vector_store = get_vector_store(tenant_id=tenant_id)
        print(f"📊 Using collection: {tenant_vector_store.collection_name}")

        with _kg_debug_log() as log:
            log.write(f"📊 KG Pipeline START for object {storage_obj.id}\n")
            log.write(f"📊 Tenant: {tenant_id}, Collection: {tenant_vector_store.collection_name}\n")
            log.flush()

        try:
            # STEP 1: DELETE OLD EMBEDDINGS (Update Strategy)
            # This ensures we don't keep outdated embeddings when re-uploading files
            tenant_vector_store.delete_embedding(storage_obj.id)

            # STEP 2: Check for AI embeddingsList (multi-embedding mode)
            embeddings_list = None
            primary_embedding_text = None
            embedding_quality = {}  # Default to empty dict to avoid NoneType errors

            if storage_obj.ai_context_metadata:
                embedding_info = storage_obj.ai_context_metadata.get("embedding_info", {})
                embeddings_list = embedding_info.get("embeddingsList")
                primary_embedding_text = embedding_info.get("embeddingText")
                embedding_quality = embedding_info.get("embeddingQuality", {})

                with _kg_debug_log() as log:
                    log.write(f"  ai_context_metadata EXISTS\n")
                    log.write(f"  embeddings_list: {len(embeddings_list) if embeddings_list else 'None'}\n")
                    log.write(f"  primary_embedding_text: {'Yes' if primary_embedding_text else 'No'}\n")
                    log.write(f"  embedding_quality: {embedding_quality}\n")
                    log.flush()
            else:
                with _kg_debug_log() as log:
                    log.write(f"  ai_context_metadata is None or empty!\n")
                    log.flush()

            # STEP 2.5: Check if manual review is required
            if embedding_quality.get("needs_review", False):
                recommendation = embedding_quality.get("recommendation", "review_required")
                issues = embedding_quality.get("issues", [])
                quality_score = embedding_quality.get("quality_score", 0)

                with _kg_debug_log() as log:
                    log.write(f"  needs_review=True, recommendation={recommendation}\n")
                    log.flush()

                print(f"⚠️  KG Pipeline: Data quality warning for object {storage_obj.id}")
                print(f"   Quality Score: {quality_score}/10")
                print(f"   Recommendation: {recommendation}")
                print(f"   Issues: {', '.join(issues)}")

                # ONLY skip if recommendation is explicitly "skip_embedding"
                # Otherwise proceed with embedding creation (with warnings logged)
                if recommendation == "skip_embedding":
                    with _kg_debug_log() as log:
                        log.write(f"  RETURNING NONE - skip_embedding\n")
                        log.flush()
                    print(f"❌ KG Pipeline: Skipping embeddings due to skip_embedding recommendation")
                    return None
                else:
                    with _kg_debug_log() as log:
                        log.write(f"  Proceeding despite quality warnings\n")
                        log.flush()
                    print(f"⚙️  KG Pipeline: Proceeding with embeddings despite quality warnings")

            # STEP 3: Create embeddings based on AI analysis
            if embeddings_list and len(embeddings_list) > 0:
                # MULTI-EMBEDDING MODE: AI returned multiple embeddings to create
                with _kg_debug_log() as log:
                    log.write(f"  Entering MULTI-EMBEDDING mode with {len(embeddings_list)} embeddings\n")
                    log.flush()

                print(f"🎯 KG Pipeline: Multi-embedding mode - creating {len(embeddings_list)} embeddings")
                embeddings = await self._create_multiple_embeddings(
                    storage_obj, embeddings_list, db, tenant_vector_store
                )

                with _kg_debug_log() as log:
                    log.write(f"  _create_multiple_embeddings returned: {len(embeddings) if embeddings else 'None'}\n")
                    log.flush()

                if not embeddings:
                    with _kg_debug_log() as log:
                        log.write(f"  RETURNING NONE - no embeddings created\n")
                        log.flush()
                    print(f"⚠️ KG Pipeline: Failed to create multiple embeddings")
                    return None

                # STEP 3.5: Process image URIs from embeddings metadata
                with _kg_debug_log() as log:
                    log.write(f"About to call process_image_uris with {len(embeddings_list)} embeddings\n")
                    log.flush()

                created_ids = await self.process_image_uris(storage_obj, embeddings_list, db)

                with _kg_debug_log() as log:
                    log.write(f"process_image_uris returned: {created_ids}\n")
                    log.flush()

                # Return KG entry with first embedding (all share same object_id)
                kg_entry = KnowledgeGraphEntry(
                    storage_object_id=storage_obj.id,
                    entities=[],
                    relations=[],
                    embedding=embeddings[0]
                )
                return kg_entry

            else:
                # SINGLE EMBEDDING MODE: Traditional flow
                print(f"📝 KG Pipeline: Single embedding mode")

                # Use AI's embeddingText if available, otherwise generate from metadata
                if primary_embedding_text and primary_embedding_text.strip():
                    embedding_text = primary_embedding_text
                    print(f"📝 KG Pipeline: Using AI embeddingText ({len(embedding_text)} chars)")
                else:
                    embedding_text = self.embedding_service.create_embedding_text(storage_obj)
                    print(f"📝 KG Pipeline: Generated embedding text ({len(embedding_text)} chars)")

                if not embedding_text.strip():
                    print(f"⚠️ KG Pipeline: No embeddable content for object {storage_obj.id}")
                    return None

                # Create embedding vector
                vector = await self.embedding_service.generate_embedding(embedding_text)
                print(f"🔢 KG Pipeline: Generated {len(vector)}-dimensional vector")

                # Get metadata
                metadata = self._build_embedding_metadata(storage_obj, db)

                # Create EmbeddingVector object
                embedding = EmbeddingVector(
                    object_id=storage_obj.id,
                    vector=vector,
                    embedding_text=embedding_text,
                    metadata=metadata
                )

                # Store in tenant-specific ChromaDB collection
                tenant_vector_store.upsert_embedding(embedding)
                print(f"✅ KG Pipeline: Stored embedding for object {storage_obj.id} in {tenant_vector_store.collection_name}")

                # Create Knowledge Graph Entry
                kg_entry = KnowledgeGraphEntry(
                    storage_object_id=storage_obj.id,
                    entities=[],
                    relations=[],
                    embedding=embedding
                )
                return kg_entry

        except Exception as e:
            print(f"❌ KG Pipeline: Error processing object {storage_obj.id}: {e}")
            import traceback
            traceback.print_exc()
            return None

    def _build_embedding_metadata(self, storage_obj: StorageObject, db: Session) -> Dict[str, Any]:
        """
        Build metadata dictionary for an embedding.

        Extracts tenant info, ownership, and other relevant metadata.
        """
        owner_user_id: Optional[int] = getattr(storage_obj, "owner_user_id", None)
        owner_email: Optional[str] = None
        tenant_id: Optional[str] = None
        is_public: bool = bool(getattr(storage_obj, "is_public", False))

        try:
            if owner_user_id is not None:
                user: Optional[User] = db.query(User).filter(User.id == owner_user_id).first()
                if user and getattr(user, "email", None):
                    owner_email = user.email
                    # Prefer tenant override from storage_obj.metadata_json if present
                    override_tenant = None
                    try:
                        if storage_obj.metadata_json and isinstance(storage_obj.metadata_json, dict):
                            override_tenant = storage_obj.metadata_json.get("tenant_id")
                    except Exception:
                        override_tenant = None
                    if override_tenant:
                        tenant_id = str(override_tenant).strip().lower()
                    elif "@" in owner_email:
                        tenant_id = owner_email.split("@", 1)[1].strip().lower()
        except Exception:
            owner_email = None
            tenant_id = None

        return {
            "object_id": storage_obj.id,  # Required for text search
            "category": storage_obj.ai_category,
            "collection_id": storage_obj.collection_id,
            "link_id": storage_obj.link_id,
            "mime_type": storage_obj.mime_type,
            # Tenant-aware metadata
            "owner_user_id": owner_user_id,
            "owner_email": owner_email,
            "tenant_id": tenant_id,
            "is_public": is_public,
        }

    async def _create_multiple_embeddings(
        self,
        storage_obj: StorageObject,
        embeddings_list: list,
        db: Session,
        tenant_vector_store: VectorStore
    ) -> Optional[list]:
        """
        Create multiple embeddings from AI's embeddingsList.

        Each embedding gets a unique ChromaDB ID: obj_{object_id}_{index}
        All embeddings share the same object_id in metadata for filtering/deletion.

        Args:
            storage_obj: Storage object
            embeddings_list: List of embedding dicts from AI (text, type, metadata)
            db: Database session

        Returns:
            List of created EmbeddingVector objects, or None if failed
        """
        created_embeddings = []
        base_metadata = self._build_embedding_metadata(storage_obj, db)

        for idx, embedding_item in enumerate(embeddings_list):
            try:
                # Extract AI's embedding specification
                embedding_text = embedding_item.get("text", "").strip()
                embedding_type = embedding_item.get("type", "item")
                embedding_meta = embedding_item.get("metadata", {})

                if not embedding_text:
                    print(f"⚠️ KG Pipeline: Skipping empty embedding at index {idx}")
                    continue

                print(f"  Creating embedding {idx}: {embedding_text[:100]}...")

                # Generate vector embedding
                vector = await self.embedding_service.generate_embedding(embedding_text)

                # Combine base metadata with embedding-specific metadata
                full_metadata = {**base_metadata}
                full_metadata["embedding_type"] = embedding_type
                full_metadata["embedding_index"] = idx
                full_metadata.update(embedding_meta)

                # CRITICAL: ChromaDB only accepts str, int, float, bool, None
                # Convert lists and dicts to JSON strings
                import json
                sanitized_metadata = {}
                for key, value in full_metadata.items():
                    if value is None:
                        # Skip None values - ChromaDB doesn't accept them
                        continue
                    elif isinstance(value, (list, dict)):
                        sanitized_metadata[key] = json.dumps(value, ensure_ascii=False)
                    elif isinstance(value, (str, int, float, bool)):
                        sanitized_metadata[key] = value
                    else:
                        # Convert other types to string
                        sanitized_metadata[key] = str(value)

                # Create EmbeddingVector with suffix ID
                # Note: EmbeddingVector.to_chroma_document() will generate ID as obj_{object_id}
                # We need to modify it to support suffix
                embedding = EmbeddingVector(
                    object_id=storage_obj.id,
                    vector=vector,
                    embedding_text=embedding_text,
                    metadata=sanitized_metadata
                )

                # Store with custom ID (obj_{object_id}_{idx}) in tenant-specific collection
                chroma_id = f"obj_{storage_obj.id}_{idx}"
                tenant_vector_store.collection.upsert(
                    ids=[chroma_id],
                    embeddings=[vector],
                    metadatas=[sanitized_metadata],
                    documents=[embedding_text]
                )

                created_embeddings.append(embedding)
                print(f"  ✅ Stored embedding {chroma_id}")

            except Exception as e:
                with _kg_debug_log() as log:
                    log.write(f"  ERROR creating embedding {idx}: {e}\n")
                    import traceback as tb
                    tb.print_exc(file=log)
                    log.flush()
                print(f"❌ KG Pipeline: Failed to create embedding {idx}: {e}")
                # Continue with other embeddings even if one fails

        with _kg_debug_log() as log:
            log.write(f"  created_embeddings count: {len(created_embeddings)}\n")
            log.flush()

        if not created_embeddings:
            with _kg_debug_log() as log:
                log.write(f"  RETURNING NONE - no embeddings were successfully created\n")
                log.flush()
            return None

        print(f"✅ KG Pipeline: Created {len(created_embeddings)} embeddings for object {storage_obj.id}")
        return created_embeddings

    async def _delete_existing_embeddings(self, object_id: int) -> None:
        """
        Delete existing embeddings for an object.

        This implements the UPDATE STRATEGY: when a file is re-uploaded or
        re-analyzed, we delete the old embedding before creating a new one.

        Supports multiple embeddings per object (all deleted via metadata filter).

        Args:
            object_id: Storage object ID
        """
        try:
            self.vector_store.delete_embedding(object_id)
            print(f"🗑️ KG Pipeline: Deleted old embedding(s) for object {object_id}")
        except Exception as e:
            # It's OK if there's no embedding to delete (first upload)
            print(f"ℹ️ KG Pipeline: No existing embedding to delete for object {object_id}: {e}")

    async def find_similar_objects(
        self,
        object_id: int,
        limit: int = 10,
        where: Optional[Dict[str, Any]] = None,
        tenant_id: Optional[str] = None
    ) -> list:
        """
        Find objects similar to the given storage object.

        Args:
            object_id: Storage object ID
            limit: Maximum number of results
            where: Optional metadata filters
            tenant_id: Tenant identifier for collection selection

        Returns:
            List of similar objects with similarity scores
        """
        try:
            # Get tenant-specific vector store
            tenant_vector_store = get_vector_store(tenant_id=tenant_id)
            print(f"🔍 KG Pipeline: find_similar_objects for object_id={object_id}, tenant={tenant_id}, collection={tenant_vector_store.collection_name}")
            result = tenant_vector_store.find_similar(object_id, limit, where=where)
            print(f"🔍 KG Pipeline: find_similar returned {len(result)} results")
            return result
        except Exception as e:
            import traceback
            error_msg = f"❌ KG Pipeline: Error finding similar objects: {e}\n{traceback.format_exc()}"
            print(error_msg)
            # Also write to debug file
            try:
                with open("/tmp/kg_similarity_debug.log", "a") as f:
                    import datetime
                    f.write(f"\n[{datetime.datetime.now()}] {error_msg}\n")
            except:
                pass
            return []

    def get_stats(self, tenant_id: Optional[str] = None) -> dict:
        """
        Get knowledge graph statistics for a specific tenant.

        Args:
            tenant_id: Optional tenant identifier. If provided, returns stats for
                      tenant-specific collection. If None, uses legacy default collection.

        Returns:
            Dictionary with stats:
            {
                "total_embeddings": 123,
                "collection": "tenant_oneal_knowledge" (or "arkturian_knowledge" for legacy)
            }
        """
        # Get tenant-specific vector store if tenant_id provided
        if tenant_id:
            tenant_vector_store = get_vector_store(tenant_id=tenant_id)
            return {
                "total_embeddings": tenant_vector_store.count(),
                "collection": tenant_vector_store.collection_name
            }
        else:
            # Legacy: use default vector store for backward compatibility
            return {
                "total_embeddings": self.vector_store.count(),
                "collection": self.vector_store.collection_name
            }

    async def process_image_uris(
        self,
        storage_obj: StorageObject,
        embeddings_list: list,
        db: Session
    ) -> List[int]:
        """
        Process image URIs from embeddingsList metadata.

        Supports two modes:
        1. NEW: uri_groups with intelligent grouping (select_best or create_separate)
        2. LEGACY: image_uris (treated as select_best for backward compatibility)

        Args:
            storage_obj: Parent storage object (CSV/Excel file)
            embeddings_list: List of embedding dicts from AI
            db: Database session

        Returns:
            List of created storage object IDs
        """
        with _kg_debug_log() as log:
            log.write(f"🖼️  process_image_uris START for object {storage_obj.id}\n")
            log.write(f"   Received {len(embeddings_list)} embeddings\n")
            log.flush()

        print(f"🖼️  Processing image URIs for object {storage_obj.id}")

        created_object_ids = []
        processed_uri_sets = set()  # Track processed URIs to avoid duplicates

        for idx, embedding_item in enumerate(embeddings_list):
            try:
                metadata = embedding_item.get("metadata", {})

                # Check for NEW uri_groups format
                uri_groups = metadata.get("uri_groups", [])

                # LEGACY: Check for old image_uris format
                if not uri_groups and metadata.get("image_uris"):
                    # Convert legacy format to new format
                    uri_groups = [{
                        "uris": metadata["image_uris"],
                        "mode": "select_best",
                        "description": "Legacy image URIs"
                    }]

                if not uri_groups:
                    continue

                print(f"  📌 Processing {len(uri_groups)} URI group(s) at index {idx}")

                # Process each URI group
                for group_idx, uri_group in enumerate(uri_groups):
                    uris = uri_group.get("uris", [])
                    mode = uri_group.get("mode", "select_best")
                    description = uri_group.get("description", "")

                    if not uris:
                        continue

                    # Deduplication check
                    uri_tuple = tuple(sorted(uris))
                    if uri_tuple in processed_uri_sets:
                        print(f"    ⏭️  Skipping duplicate URI group {group_idx}")
                        continue

                    processed_uri_sets.add(uri_tuple)

                    print(f"    🔍 Group {group_idx}: {len(uris)} URI(s), mode={mode}")
                    if description:
                        print(f"       Description: {description}")

                    # Handle based on mode
                    if mode == "select_best":
                        # Select best URI from group
                        if len(uris) == 1:
                            best_uri = uris[0] if is_valid_url(uris[0]) else None
                        else:
                            best_uri = await select_best_uri(uris)

                        if not best_uri:
                            print(f"    ⚠️  No valid URI found in group {group_idx}")
                            continue

                        # Create single storage object for best URI
                        created_id = await self._create_storage_object_for_uri(
                            best_uri, storage_obj, embedding_item, db, description
                        )

                        if created_id:
                            created_object_ids.append(created_id)
                            print(f"    ✅ Created storage object {created_id} (best from group)")

                    elif mode == "create_separate":
                        # Create separate storage object for EACH URI
                        print(f"    🔀 Creating separate storage objects for each URI")

                        for uri_idx, uri in enumerate(uris):
                            if not is_valid_url(uri):
                                print(f"      ⚠️  Invalid URI at position {uri_idx}: {uri}")
                                continue

                            # Create storage object for this specific URI
                            uri_description = f"{description} ({uri_idx + 1}/{len(uris)})" if description else f"URI {uri_idx + 1}"
                            created_id = await self._create_storage_object_for_uri(
                                uri, storage_obj, embedding_item, db, uri_description
                            )

                            if created_id:
                                created_object_ids.append(created_id)
                                print(f"      ✅ Created storage object {created_id} for URI {uri_idx + 1}")

                    else:
                        print(f"    ⚠️  Unknown mode '{mode}' in group {group_idx}, skipping")

            except Exception as e:
                print(f"❌ Failed to process URIs at index {idx}: {e}")
                import traceback
                traceback.print_exc()
                # Continue with other embeddings

        print(f"✅ Created {len(created_object_ids)} storage objects for image URIs")
        return created_object_ids

    async def _create_storage_object_for_uri(
        self,
        uri: str,
        parent_obj: StorageObject,
        embedding_item: dict,
        db: Session,
        uri_description: str = ""
    ) -> Optional[int]:
        """
        Create a storage object for an external image URI.

        Uses "external" storage mode - file remains on external server.
        Downloads minimal data for MIME type detection only.

        Args:
            uri: Verified image URI
            parent_obj: Parent storage object (CSV/Excel)
            embedding_item: Embedding metadata containing context
            db: Database session
            uri_description: Optional description for this specific URI (e.g., "Front view")

        Returns:
            Created storage object ID, or None if failed
        """
        try:
            import httpx
            from storage.domain import save_file_and_record
            from urllib.parse import urlparse
            from pathlib import Path

            # HEAD request to get metadata WITHOUT downloading full file
            async with httpx.AsyncClient(follow_redirects=True, timeout=10) as client:
                response = await client.head(uri)

                if response.status_code != 200:
                    print(f"  ⚠️  URI not accessible: {uri} (status: {response.status_code})")
                    return None

                content_type = response.headers.get('content-type', 'image/jpeg')
                content_length = int(response.headers.get('content-length', 0))

            # Generate filename from URI
            parsed = urlparse(uri)
            original_filename = Path(parsed.path).name or "imported_image.jpg"

            # Extract row data from embedding metadata for context
            row_data = embedding_item.get("metadata", {})
            embedding_text = embedding_item.get("text", "")
            embedding_type = embedding_item.get("type", "item")

            # Build context string from embedding text
            context_parts = []
            if uri_description:
                context_parts.append(f"URI Description: {uri_description}")
            if embedding_text:
                context_parts.append(f"External URI: {embedding_text[:200]}")
            if parent_obj.original_filename:
                context_parts.append(f"Source file: {parent_obj.original_filename}")

            context = " | ".join(context_parts) if context_parts else "External URI from CSV/Excel"

            # Prepare AI context metadata
            ai_context_metadata = {
                "source": "csv_uri_import",
                "uri_description": uri_description,
                "source_uri": uri,
                "parent_object_id": parent_obj.id,
                "parent_file": parent_obj.original_filename,
                "embedding_type": embedding_type,
                "row_data": row_data,
                "storage_mode": "external",  # Mark as external reference
            }

            # Download FULL image for comprehensive vision analysis
            print(f"  🖼️  Downloading full image for vision analysis: {uri}")

            async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
                response = await client.get(uri)
                if response.status_code != 200:
                    print(f"  ⚠️  Failed to download full image: {uri} (status: {response.status_code})")
                    return None
                image_data = response.content

            print(f"  Downloaded {len(image_data)} bytes")

            # Create storage object in EXTERNAL mode (file stays on original server)
            storage_obj = await save_file_and_record(
                db,
                owner_user_id=parent_obj.owner_user_id,
                data=image_data,  # FULL image data for vision analysis
                original_filename=original_filename,
                context=context,
                is_public=parent_obj.is_public,
                collection_id=parent_obj.collection_id,
                link_id=str(parent_obj.id),  # Link to parent CSV/Excel object
                storage_mode="external",  # EXTERNAL MODE - file stays on server!
                external_uri=uri,  # Store external URI
                ai_context_metadata=ai_context_metadata,
            )

            # Trigger comprehensive vision analysis for this image
            print(f"  🎨 Starting vision analysis for storage object {storage_obj.id}")

            # Import analyze_content from ai_analysis
            from ai_analysis.service import analyze_content

            try:
                # Build AI context from metadata
                ai_context = {
                    "metadata": ai_context_metadata,
                    "file_path": uri,
                    "context_text": f"Product image from CSV: {parent_obj.original_filename}"
                }

                # Run comprehensive vision analysis
                analysis_result = await analyze_content(
                    image_data,
                    storage_obj.mime_type,
                    context=ai_context,
                    object_id=storage_obj.id
                )

                # Save AI analysis results to database
                storage_obj.ai_category = analysis_result.get("category")
                storage_obj.danger_potential = analysis_result.get("danger_potential", 1)
                storage_obj.ai_title = analysis_result.get("ai_title")
                storage_obj.ai_subtitle = analysis_result.get("ai_subtitle")
                storage_obj.ai_tags = json.dumps(analysis_result.get("ai_tags", []))
                storage_obj.ai_collections = json.dumps(analysis_result.get("ai_collections", []))
                storage_obj.ai_prompt = analysis_result.get("prompt", "")
                storage_obj.ai_response = analysis_result.get("ai_response", "")

                # Extract and store safety info
                safety_info = analysis_result.get("safety_info", {})
                storage_obj.is_safe = safety_info.get("isSafe", True)
                storage_obj.safety_confidence = safety_info.get("confidence", 1.0)
                storage_obj.safety_reasoning = safety_info.get("reasoning", "")
                storage_obj.safety_flags = json.dumps(safety_info.get("flags", []))

                # Store extracted tags
                extracted_tags = analysis_result.get("extracted_tags", {})
                storage_obj.extracted_tags = json.dumps(extracted_tags)

                db.commit()
                print(f"  ✅ Vision analysis complete for storage object {storage_obj.id}")

                # Create vector embedding from vision analysis
                embedding_info = analysis_result.get("embedding_info", {})
                if embedding_info and embedding_info.get("embeddingText"):
                    from knowledge_graph.pipeline import kg_pipeline

                    # Create embedding for this image
                    await kg_pipeline.process_embedding(
                        storage_obj,
                        embedding_info,
                        db
                    )
                    print(f"  ✅ Created vector embedding for image {storage_obj.id}")

            except Exception as e:
                print(f"  ⚠️  Vision analysis failed for {storage_obj.id}: {e}")
                import traceback
                traceback.print_exc()
                # Continue even if analysis fails - storage object is already created

            print(f"  ✅ Created external storage object {storage_obj.id} for: {original_filename}")
            print(f"     URI: {uri}")
            print(f"     Proxy URL: {storage_obj.file_url}")
            return storage_obj.id

        except httpx.TimeoutException:
            print(f"⏰ Timeout accessing image URI: {uri}")
            return None
        except Exception as e:
            print(f"❌ Failed to create storage object for URI {uri}: {e}")
            import traceback
            traceback.print_exc()
            return None


# Global pipeline instance
kg_pipeline = KnowledgeGraphPipeline()
