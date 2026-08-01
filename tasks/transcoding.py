"""
Celery tasks for video transcoding
"""

import logging
from pathlib import Path
from celery import Task
from .base import BaseStorageTask

logger = logging.getLogger(__name__)


class TranscodingTask(BaseStorageTask):
    """Base class for transcoding tasks with DB session management"""
    pass


def _process_video_transcoding_impl(storage_object_id: int) -> dict:
    """
    Process video transcoding for a storage object

    This is the actual task function that will be called by Celery.
    Uses the transcoding helper which handles both local and remote transcoding.

    Args:
        storage_object_id: ID of the storage object to transcode

    Returns:
        dict: Result with success status and message
    """
    from database import SessionLocal
    from models import StorageObject
    from storage.transcoding_helper import TranscodingHelper
    from config import settings

    logger.info(f"🎬 Starting video transcoding for storage object {storage_object_id}")

    db = SessionLocal()
    try:
        # Get storage object
        storage_obj = db.get(StorageObject, storage_object_id)
        if not storage_obj:
            logger.error(f"❌ Storage object {storage_object_id} not found")
            return {"success": False, "error": "Storage object not found"}

        # Check if it's a video
        if not storage_obj.mime_type or not storage_obj.mime_type.startswith("video/"):
            logger.warning(f"⚠️ Storage object {storage_object_id} is not a video (mime: {storage_obj.mime_type})")
            return {"success": False, "error": "Not a video file"}

        # Update status to processing
        storage_obj.transcoding_status = "processing"
        storage_obj.transcoding_progress = 0
        storage_obj.transcoding_error = None
        db.commit()

        # Get file path
        # Construct path based on storage mode
        if storage_obj.storage_mode == "reference" and storage_obj.reference_path:
            source_path = Path(storage_obj.reference_path)
        elif storage_obj.storage_mode == "external":
            logger.error(f"❌ Cannot transcode external storage object {storage_object_id}")
            storage_obj.transcoding_status = "failed"
            storage_obj.transcoding_error = "Cannot transcode external storage"
            db.commit()
            return {"success": False, "error": "Cannot transcode external storage"}
        else:
            # Standard copy mode - file is in uploads directory
            # The authoritative tenant is the COLUMN — metadata_json["tenant_id"]
            # is only written when the upload came through a tenant API key, so
            # relying on it silently sent every other tenant's videos to the
            # "arkturian" directory and failed with "Source file not found"
            # (verified on object 116760, tenant=arkserver, 2026-08-01).
            tenant_id = (
                getattr(storage_obj, "tenant_id", None)
                or (storage_obj.metadata_json or {}).get("tenant_id")
                or "arkturian"
            )
            uploads_dir = Path(settings.STORAGE_UPLOAD_DIR)
            source_path = uploads_dir / "media" / tenant_id / storage_obj.object_key

        if not source_path.exists():
            logger.error(f"❌ Source file not found: {source_path}")
            storage_obj.transcoding_status = "failed"
            storage_obj.transcoding_error = f"Source file not found: {source_path}"
            db.commit()
            return {"success": False, "error": f"Source file not found: {source_path}"}

        # Create output directory
        # Output goes to: uploads/media/{tenant}/{object_key_without_ext}_transcoded/
        base_key = storage_obj.object_key.rsplit(".", 1)[0]  # Remove extension
        output_dir = source_path.parent / f"{base_key}_transcoded"
        output_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"📂 Source: {source_path}")
        logger.info(f"📂 Output: {output_dir}")
        logger.info(f"🔧 Mode: {settings.TRANSCODING_MODE}")
        logger.info(f"📦 Response Mode: {settings.TRANSCODING_RESPONSE_MODE}")

        # Transcode using helper (handles both local and remote)
        success = TranscodingHelper.transcode_video_sync(
            source_path=source_path,
            output_dir=output_dir,
            storage_object_id=storage_object_id
        )

        if success:
            logger.info(f"✅ Transcoding completed for storage object {storage_object_id}")
            return {"success": True, "message": "Transcoding completed"}
        else:
            logger.error(f"❌ Transcoding failed for storage object {storage_object_id}")
            return {"success": False, "error": "Transcoding failed"}

    except Exception as e:
        logger.error(f"❌ Transcoding error for storage object {storage_object_id}: {e}")
        import traceback
        traceback.print_exc()

        # Update DB with error
        try:
            storage_obj = db.get(StorageObject, storage_object_id)
            if storage_obj:
                storage_obj.transcoding_status = "failed"
                storage_obj.transcoding_error = str(e)
                db.commit()
        except Exception as db_error:
            logger.error(f"Failed to update DB with error: {db_error}")

        return {"success": False, "error": str(e)}
    finally:
        db.close()

# Create Celery task
from celery_app import app

@app.task(
    name='tasks.transcoding.process_video_transcoding',
    base=TranscodingTask,
    bind=True,
    max_retries=2,
    default_retry_delay=60,  # 1 minute
    time_limit=3600,  # 1 hour hard limit
    soft_time_limit=3300,  # 55 minutes soft limit
)
def process_video_transcoding(self, storage_object_id: int) -> dict:
    """
    Celery task for video transcoding

    Args:
        storage_object_id: ID of the storage object to transcode

    Returns:
        dict: Result with success status
    """
    try:
        return _process_video_transcoding_impl(storage_object_id)
    except Exception as exc:
        logger.error(f"Transcoding task failed for object {storage_object_id}: {exc}")
        # Retry on failure
        raise self.retry(exc=exc)
