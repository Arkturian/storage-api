"""
AI Analysis Tasks for Storage API

Handles:
- Image analysis (Vision AI + Safety)
- Video analysis (5-frame sampling + Vision AI)
- Text/CSV analysis
- Knowledge Graph embedding generation

Migrated from scripts/ai_worker.py to Celery for production reliability.
"""

# Fix ChromaDB SQLite issue - MUST BE FIRST!
import sys
sys.modules['sqlite3'] = __import__('pysqlite3')

import asyncio
import base64
import shutil
from pathlib import Path
from typing import Optional, Dict, Any
import logging

from celery_app import app
from tasks.base import BaseStorageTask
from models import StorageObject
from ai_analysis.service import analyze_content
from knowledge_graph.pipeline import KnowledgeGraphPipeline

logger = logging.getLogger(__name__)


def _cleanup_scratch_dir(scratch_path: str) -> None:
    """
    Remove the pre-processing scratch directory after a task finishes.

    Called only on the success path — on failure we keep the inputs around
    so retries can re-use them and humans can inspect what tripped the
    classifier. Scratch lives outside /tmp to survive the nightly tmp cleanup,
    but it is *not* meant to grow unbounded — every successful task purges
    its own dir. Best-effort; never raises.
    """
    try:
        p = Path(scratch_path)
        # Only delete dirs we created (under AI_SCRATCH_DIR or /tmp/ai_*),
        # never an arbitrary path that ended up in the queue.
        name = p.name
        if not (name.startswith("ai_thumbs_") or name.startswith("ai_images_")):
            return
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        elif p.parent.name.startswith(("ai_thumbs_", "ai_images_")):
            shutil.rmtree(p.parent, ignore_errors=True)
    except Exception as e:
        logger.warning(f"   ⚠️  Failed to cleanup scratch dir {scratch_path}: {e}")


def _maybe_alert_unsafe(storage_obj, safety_info: dict, danger: int) -> None:
    """
    Fire a moderator alert when a high-confidence unsafe verdict comes in.

    Trigger: isSafe=False AND confidence>=0.9 AND danger>=8 — tuned so routine
    NSFW false-positives don't spam, but actual illegal/CSAM-grade hits do
    page a human. Channel + threshold are env-configurable; webhook is a
    POST with JSON body (works for Slack/Discord/Telegram bridges).

    Best-effort, never raises. Failure to alert must not affect the task.
    """
    import os
    import httpx as _httpx

    if not safety_info or safety_info.get("isSafe") is not False:
        return

    confidence = float(safety_info.get("confidence") or 0)
    min_conf = float(os.getenv("SAFETY_ALERT_MIN_CONFIDENCE", "0.9"))
    min_danger = int(os.getenv("SAFETY_ALERT_MIN_DANGER", "8"))
    if confidence < min_conf or (danger or 0) < min_danger:
        return

    webhook = os.getenv("SAFETY_ALERT_WEBHOOK", "").strip()
    if not webhook:
        # No webhook configured — fall back to structured log line so an
        # operator scanning the journal can grep for these.
        logger.error(
            f"🚨 SAFETY_ALERT object_id={storage_obj.id} tenant={storage_obj.tenant_id} "
            f"danger={danger} conf={confidence:.2f} flags={safety_info.get('flags')} "
            f"reasoning={safety_info.get('reasoning')!r}"
        )
        return

    payload = {
        "event": "storage.safety.unsafe",
        "object_id": storage_obj.id,
        "tenant_id": storage_obj.tenant_id,
        "original_filename": storage_obj.original_filename,
        "owner_user_id": storage_obj.owner_user_id,
        "ai_safety_rating": "unsafe",
        "ai_danger_potential": danger,
        "confidence": confidence,
        "flags": safety_info.get("flags") or [],
        "reasoning": safety_info.get("reasoning") or "",
    }
    try:
        _httpx.post(webhook, json=payload, timeout=5.0)
        logger.info(f"   🚨 Safety alert dispatched for object {storage_obj.id}")
    except Exception as e:
        logger.error(f"   ⚠️  Safety alert webhook failed: {e}")


@app.task(base=BaseStorageTask, name='tasks.ai_analysis.process_safety_check_only')
def process_safety_check_only(object_id: int, image_path: str, filename: str) -> Dict[str, Any]:
    """
    Quick safety check only (Gemini Flash)

    Fast safety analysis without full vision or embedding generation.
    Ideal for immediate feedback to users.

    Args:
        object_id: Storage object ID
        image_path: Path to image file or directory containing image.jpg
        filename: Original filename

    Returns:
        Safety check results dictionary
    """
    logger.info(f"🛡️ Processing SAFETY CHECK for object {object_id}: {filename}")

    # Resolve image path
    img_path = Path(image_path)
    if img_path.is_dir():
        img_path = img_path / "image.jpg"
        if not img_path.exists():
            raise FileNotFoundError(f"Expected image.jpg in {image_path}")

    # Safety Check using Claude with image paths (fast with haiku model)
    from ai_analysis.service import _run_safety_check_with_paths, _sanitize_for_prompt
    context_info = f"Filename: {_sanitize_for_prompt(filename, label='filename')}"
    result = asyncio.run(_run_safety_check_with_paths(
        image_paths=[str(img_path)],
        text_content=None,
        context_info=context_info
    ))

    logger.info(f"   🛡️ Safety check complete:")
    logger.info(f"      isSafe: {result.get('safety_info', {}).get('isSafe', 'unknown')}")
    logger.info(f"      Category: {result.get('category', 'unknown')}")

    # Update database with safety info only
    task = process_safety_check_only
    db = task.get_db_session()

    storage_obj = db.query(StorageObject).filter(StorageObject.id == object_id).first()
    if not storage_obj:
        raise Exception(f"Storage object {object_id} not found in database")

    # Update only safety fields
    storage_obj.ai_safety_rating = "safe" if result.get("safety_info", {}).get("isSafe") else "unsafe"
    storage_obj.ai_category = result.get("category")
    storage_obj.ai_danger_potential = result.get("danger_potential")
    storage_obj.safety_info = result.get("safety_info")
    storage_obj.ai_safety_status = "completed"

    db.commit()
    _maybe_alert_unsafe(storage_obj, result.get("safety_info") or {}, result.get("danger_potential") or 0)
    logger.info(f"   ✅ Safety check saved to database for object {object_id}")

    _cleanup_scratch_dir(image_path)
    return {
        "object_id": object_id,
        "status": "safety_check_completed",
        "ai_safety_rating": storage_obj.ai_safety_rating,
        "ai_category": storage_obj.ai_category
    }


@app.task(base=BaseStorageTask, name='tasks.ai_analysis.process_vision_analysis_only')
def process_vision_analysis_only(object_id: int, image_path: str, filename: str) -> Dict[str, Any]:
    """
    Full vision analysis WITHOUT embedding generation

    Comprehensive vision AI analysis but no Knowledge Graph embedding.
    Useful when you want metadata but don't need semantic search.

    Args:
        object_id: Storage object ID
        image_path: Path to image file or directory containing image.jpg
        filename: Original filename

    Returns:
        Vision analysis results dictionary
    """
    logger.info(f"🎨 Processing VISION ANALYSIS (no embedding) for object {object_id}: {filename}")

    # Resolve image path
    img_path = Path(image_path)
    if img_path.is_dir():
        img_path = img_path / "image.jpg"
        if not img_path.exists():
            raise FileNotFoundError(f"Expected image.jpg in {image_path}")

    # Full Vision Analysis using Claude with image paths (sonnet model)
    from ai_analysis.service import _run_vision_analysis_with_paths, _sanitize_for_prompt
    context_info = f"Filename: {_sanitize_for_prompt(filename, label='filename')}\nMedia type: image"

    result = asyncio.run(_run_vision_analysis_with_paths(
        image_paths=[str(img_path)],
        context_info=context_info,
        vision_mode="generic"
    ))

    logger.info(f"   🎨 Vision analysis complete:")
    logger.info(f"      Safety: {result.get('safety_info', {}).get('isSafe', 'unknown')}")
    logger.info(f"      Category: {result.get('category', 'unknown')}")
    logger.info(f"      Tags: {len(result.get('ai_tags', []))} extracted")

    # Update database with all vision data (NO EMBEDDING)
    task = process_vision_analysis_only
    db = task.get_db_session()

    storage_obj = db.query(StorageObject).filter(StorageObject.id == object_id).first()
    if not storage_obj:
        raise Exception(f"Storage object {object_id} not found in database")

    # Update all AI fields (using new result format from Claude-based analysis)
    storage_obj.ai_safety_rating = "safe" if result.get("safety_info", {}).get("isSafe") else "unsafe"
    storage_obj.ai_category = result.get("category")
    storage_obj.ai_danger_potential = result.get("danger_potential")
    storage_obj.ai_title = result.get("ai_title")
    storage_obj.ai_subtitle = result.get("ai_subtitle")
    storage_obj.ai_tags = result.get("ai_tags", [])
    storage_obj.ai_collections = []  # Not provided in new format
    storage_obj.safety_info = result.get("safety_info")

    # Build ai_context_metadata (without embedding)
    ai_context = {
        "prompt": result.get("prompt", ""),
        "ai_response": result.get("ai_response", ""),
        "mode": "vision_only"
    }
    storage_obj.ai_context_metadata = ai_context
    storage_obj.ai_safety_status = "completed"

    db.commit()
    _maybe_alert_unsafe(storage_obj, result.get("safety_info") or {}, result.get("danger_potential") or 0)
    logger.info(f"   ✅ Vision analysis saved to database for object {object_id}")

    # NO embedding generation!
    _cleanup_scratch_dir(image_path)

    return {
        "object_id": object_id,
        "status": "vision_analysis_completed",
        "ai_safety_rating": storage_obj.ai_safety_rating,
        "ai_category": storage_obj.ai_category,
        "tags_count": len(result.get("ai_tags", []))
    }


@app.task(base=BaseStorageTask, name='tasks.ai_analysis.process_image_analysis')
def process_image_analysis(object_id: int, image_path: str, filename: str) -> Dict[str, Any]:
    """
    FULL analysis: Vision + Embedding generation

    Complete AI pipeline with comprehensive vision analysis AND Knowledge Graph embedding.
    This is the most thorough analysis mode.

    Args:
        object_id: Storage object ID
        image_path: Path to image file or directory containing image.jpg
        filename: Original filename

    Returns:
        Analysis results dictionary
    """
    logger.info(f"📸 Processing FULL ANALYSIS (vision + embedding) for object {object_id}: {filename}")

    # Resolve image path (might be directory with image.jpg inside)
    img_path = Path(image_path)
    if img_path.is_dir():
        img_path = img_path / "image.jpg"
        if not img_path.exists():
            raise FileNotFoundError(f"Expected image.jpg in {image_path}")
        logger.debug(f"   Found image in directory: {img_path}")

    # Full Vision Analysis using Claude with image paths (sonnet model)
    from ai_analysis.service import _run_vision_analysis_with_paths, _sanitize_for_prompt
    context_info = f"Filename: {_sanitize_for_prompt(filename, label='filename')}\nMedia type: image"

    result = asyncio.run(_run_vision_analysis_with_paths(
        image_paths=[str(img_path)],
        context_info=context_info,
        vision_mode="generic"
    ))

    logger.info(f"   🎨 AI analysis complete:")
    logger.info(f"      Safety: {result.get('safety_info', {}).get('isSafe', 'unknown')}")
    logger.info(f"      Category: {result.get('category', 'unknown')}")
    logger.info(f"      Tags: {len(result.get('ai_tags', []))} extracted")

    # Update database
    task = process_image_analysis
    db = task.get_db_session()

    storage_obj = db.query(StorageObject).filter(StorageObject.id == object_id).first()
    if not storage_obj:
        raise Exception(f"Storage object {object_id} not found in database")

    # Update AI fields (using new result format from Claude-based analysis)
    storage_obj.ai_safety_rating = "safe" if result.get("safety_info", {}).get("isSafe") else "unsafe"
    storage_obj.ai_category = result.get("category")
    storage_obj.ai_danger_potential = result.get("danger_potential")
    storage_obj.ai_title = result.get("ai_title")
    storage_obj.ai_subtitle = result.get("ai_subtitle")
    storage_obj.ai_tags = result.get("ai_tags", [])
    storage_obj.ai_collections = []  # Not provided in new format
    storage_obj.safety_info = result.get("safety_info")

    # Build ai_context_metadata
    ai_context = {
        "embedding_info": result.get("embedding_info", {}),
        "prompt": result.get("prompt", ""),
        "ai_response": result.get("ai_response", ""),
        "mode": result.get("mode", "unknown")
    }
    storage_obj.ai_context_metadata = ai_context
    storage_obj.ai_safety_status = "completed"

    db.commit()
    _maybe_alert_unsafe(storage_obj, result.get("safety_info") or {}, result.get("danger_potential") or 0)
    logger.info(f"   ✅ Database updated for object {object_id}")

    # Trigger embedding generation (chained task)
    generate_embedding.delay(object_id)
    _cleanup_scratch_dir(image_path)

    return {
        "object_id": object_id,
        "status": "completed",
        "ai_safety_rating": storage_obj.ai_safety_rating,
        "ai_category": storage_obj.ai_category,
        "tags_count": len(result.get("ai_tags", []))
    }


@app.task(base=BaseStorageTask, name='tasks.ai_analysis.process_video_analysis')
def process_video_analysis(object_id: int, thumb_dir: str, filename: str) -> Dict[str, Any]:
    """
    Process video thumbnails for AI analysis

    Loads 5 video screenshots and sends to AI for comprehensive analysis.

    Args:
        object_id: Storage object ID
        thumb_dir: Directory containing thumb_01.jpg - thumb_05.jpg
        filename: Original filename

    Returns:
        Analysis results dictionary
    """
    logger.info(f"🎬 Processing video analysis for object {object_id}: {filename}")

    thumb_path = Path(thumb_dir)

    # Collect paths to all 5 video thumbnails
    image_paths = []
    for i in range(1, 6):
        thumb_file = thumb_path / f"thumb_0{i}.jpg"
        if thumb_file.exists():
            image_paths.append(str(thumb_file))
            logger.debug(f"   ✅ Found screenshot {i}: {thumb_file}")
        else:
            logger.warning(f"   ⚠️  Missing screenshot {i}: {thumb_file}")

    if not image_paths:
        raise Exception(f"No thumbnails found in {thumb_dir}")

    logger.info(f"   📸 Found {len(image_paths)}/5 screenshots, analyzing ALL frames with Claude...")

    # Video analysis using Claude with ALL frame paths (sonnet model for quality)
    from ai_analysis.service import _run_vision_analysis_with_paths, _sanitize_for_prompt
    context_info = (
        f"Filename: {_sanitize_for_prompt(filename, label='filename')}\n"
        f"Media type: video\n"
        f"Analyzing {len(image_paths)} sampled frames from video\n"
        f"Check ALL frames for safety - if ANY frame is unsafe, mark as unsafe"
    )

    result = asyncio.run(_run_vision_analysis_with_paths(
        image_paths=image_paths,  # Pass ALL 5 frames!
        context_info=context_info,
        vision_mode="video"
    ))

    logger.info(f"   🎨 AI analysis complete:")
    logger.info(f"      Safety: {result.get('safety_info', {}).get('isSafe', 'unknown')}")
    logger.info(f"      Category: {result.get('category', 'unknown')}")
    logger.info(f"      Title: {result.get('ai_title', 'N/A')[:50] if result.get('ai_title') else 'N/A'}...")

    # Update database
    task = process_video_analysis
    db = task.get_db_session()

    storage_obj = db.query(StorageObject).filter(StorageObject.id == object_id).first()
    if not storage_obj:
        raise Exception(f"Storage object {object_id} not found in database")

    # Update AI fields (using new result format from Claude-based analysis)
    storage_obj.ai_safety_rating = "safe" if result.get("safety_info", {}).get("isSafe") else "unsafe"
    storage_obj.ai_category = result.get("category")
    storage_obj.ai_danger_potential = result.get("danger_potential")
    storage_obj.ai_title = result.get("ai_title")
    storage_obj.ai_subtitle = result.get("ai_subtitle")
    storage_obj.ai_tags = result.get("ai_tags", [])
    storage_obj.ai_collections = []  # Not provided in new format
    storage_obj.safety_info = result.get("safety_info")

    # Technical/perceptual quality score (0-100) — same claude call as safety.
    # Storage only EXPOSES the score; the consuming app decides what to publish.
    quality = result.get("quality_assessment") or {}
    if quality.get("qualityScore") is not None:
        storage_obj.ai_quality_score = quality.get("qualityScore")

    # Build ai_context_metadata
    ai_context = {
        "embedding_info": result.get("embedding_info", {}),
        "ai_response": result.get("ai_response", ""),
        "quality_assessment": quality,  # full breakdown: sharpness/exposure/stability/composition/noise
        "mode": "video",
        "frames_analyzed": len(image_paths)
    }
    storage_obj.ai_context_metadata = ai_context
    storage_obj.ai_safety_status = "completed"

    db.commit()
    _maybe_alert_unsafe(storage_obj, result.get("safety_info") or {}, result.get("danger_potential") or 0)
    logger.info(f"   ✅ Database updated for object {object_id}")

    # Trigger embedding generation
    generate_embedding.delay(object_id)
    _cleanup_scratch_dir(thumb_dir)

    return {
        "object_id": object_id,
        "status": "completed",
        "frames_analyzed": len(image_paths)
    }


@app.task(base=BaseStorageTask, name='tasks.ai_analysis.process_text_analysis')
def process_text_analysis(object_id: int, file_path: str, filename: str) -> Dict[str, Any]:
    """
    Process text/CSV files for AI analysis

    Args:
        object_id: Storage object ID
        file_path: Path to text file
        filename: Original filename

    Returns:
        Analysis results dictionary
    """
    logger.info(f"📄 Processing text analysis for object {object_id}: {filename}")

    # Read file content
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
    except UnicodeDecodeError:
        # Fallback to latin-1
        with open(file_path, "r", encoding="latin-1") as f:
            content = f.read()

    # Determine MIME type
    mime_type = "application/csv" if filename.lower().endswith('.csv') else "text/plain"

    logger.info(f"   📄 File size: {len(content)} bytes, MIME: {mime_type}")

    context = {
        "filename": filename,
        "media_type": "text",
        "file_extension": filename.split('.')[-1].lower() if '.' in filename else "txt"
    }

    result = asyncio.run(analyze_content(
        data=content.encode('utf-8'),
        mime_type=mime_type,
        context=context,
        object_id=object_id
    ))

    logger.info(f"   📊 AI analysis complete:")
    logger.info(f"      Mode: {result.get('mode', 'unknown')}")
    logger.info(f"      Title: {result.get('ai_title', 'N/A')}")

    # Update database
    task = process_text_analysis
    db = task.get_db_session()

    storage_obj = db.query(StorageObject).filter(StorageObject.id == object_id).first()
    if not storage_obj:
        raise Exception(f"Storage object {object_id} not found")

    # Update AI fields
    storage_obj.ai_title = result.get("ai_title")
    storage_obj.ai_subtitle = result.get("ai_subtitle")
    storage_obj.ai_tags = result.get("ai_tags")
    storage_obj.ai_collections = result.get("ai_collections")
    storage_obj.ai_category = result.get("ai_category", "text")

    # Build ai_context_metadata
    ai_context = {
        "embedding_info": result.get("embedding_info", {}),
        "prompt": result.get("prompt", ""),
        "ai_response": result.get("ai_response", ""),
        "mode": result.get("mode", "text")
    }
    storage_obj.ai_context_metadata = ai_context
    storage_obj.ai_safety_status = "completed"

    db.commit()
    _maybe_alert_unsafe(storage_obj, result.get("safety_info") or {}, result.get("danger_potential") or 0)
    logger.info(f"   ✅ Database updated for object {object_id}")

    # Trigger embedding generation
    generate_embedding.delay(object_id)

    return {
        "object_id": object_id,
        "status": "completed",
        "file_size": len(content)
    }


@app.task(base=BaseStorageTask, name='tasks.ai_analysis.generate_embedding', queue='embeddings')
def generate_embedding(object_id: int) -> Dict[str, Any]:
    """
    Generate Knowledge Graph embedding for storage object

    Uses OpenAI text-embedding-3-large (3072-dim) and stores in ChromaDB.

    Args:
        object_id: Storage object ID

    Returns:
        Embedding generation result
    """
    logger.info(f"   🔗 Generating Knowledge Graph embedding for object {object_id}...")

    task = generate_embedding
    db = task.get_db_session()

    storage_obj = db.query(StorageObject).filter(StorageObject.id == object_id).first()
    if not storage_obj:
        raise Exception(f"Storage object {object_id} not found")

    try:
        pipeline = KnowledgeGraphPipeline()
        kg_entry = pipeline.process_storage_object(storage_obj, db)

        if kg_entry:
            logger.info(f"   ✅ Embedding generated and stored in ChromaDB")
            return {
                "object_id": object_id,
                "status": "embedded",
                "vector_dim": 3072
            }
        else:
            logger.warning(f"   ⚠️  No embedding generated (object may not have AI context)")
            return {
                "object_id": object_id,
                "status": "skipped",
                "reason": "no_ai_context"
            }

    except Exception as kg_error:
        logger.error(f"   ❌ Knowledge Graph error: {kg_error}")
        raise  # Will trigger retry
