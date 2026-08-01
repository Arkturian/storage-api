"""
Transcoding helper for storage API

Integrates the transcoding package with storage uploads
"""

import asyncio
from pathlib import Path
from typing import Optional
from config import settings
import logging

# Import our transcoding package
try:
    from transcoding import TranscoderFactory, TranscodingConfig, TranscodingMode
    TRANSCODING_AVAILABLE = True
except ImportError:
    TRANSCODING_AVAILABLE = False
    logging.warning("Transcoding package not available")


class TranscodingHelper:
    """Helper class for video transcoding integration"""

    @staticmethod
    def is_enabled() -> bool:
        """Check if transcoding is enabled"""
        if not TRANSCODING_AVAILABLE:
            return False
        return settings.TRANSCODING_MODE.lower() != "disabled"

    @staticmethod
    def should_transcode(mime_type: Optional[str]) -> bool:
        """Check if this file should be transcoded"""
        if not TranscodingHelper.is_enabled():
            return False

        if not mime_type:
            return False

        return mime_type.startswith("video/")

    @staticmethod
    async def transcode_video(
        source_path: Path,
        output_dir: Path,
        storage_object_id: int
    ) -> bool:
        """
        Transcode a video file

        Args:
            source_path: Path to source video
            output_dir: Directory for transcoded files
            storage_object_id: Storage object ID for reference

        Returns:
            bool: True if transcoding was started/succeeded
        """
        if not TRANSCODING_AVAILABLE:
            logging.error("Transcoding requested but package not available")
            return False

        try:
            # Create transcoding config
            config = TranscodingConfig.from_env(
                mode_str=settings.TRANSCODING_MODE,
                remote_url=settings.TRANSCODING_API_URL,
                remote_key=settings.TRANSCODING_API_KEY,
                remote_response_mode=settings.TRANSCODING_RESPONSE_MODE
            )

            # Create transcoder
            transcoder = TranscoderFactory.create(config)

            # Check availability
            if not await transcoder.check_availability():
                logging.error(f"Transcoder not available (mode: {settings.TRANSCODING_MODE})")
                return False

            logging.info(f"🎬 Starting transcoding for storage object {storage_object_id}")
            logging.info(f"   Mode: {settings.TRANSCODING_MODE}")
            logging.info(f"   Source: {source_path}")
            logging.info(f"   Output: {output_dir}")

            # Create output directory
            output_dir.mkdir(parents=True, exist_ok=True)

            # Transcode (this will run async)
            result = await transcoder.transcode(source_path, output_dir)

            # Update database with transcoding result
            from database import SessionLocal
            from models import StorageObject
            import shutil

            db_session = SessionLocal()
            try:
                storage_obj = db_session.get(StorageObject, storage_object_id)
                if storage_obj:
                    if result.success:
                        # Handle response based on mode
                        response_mode = result.metadata.get("response_mode", "zip")
                        variant_count = 0

                        if response_mode == "path":
                            # Path mode: Files are in temp directory on same server, copy them
                            source_output_dir = Path(result.metadata.get("output_dir", ""))
                            variants_data = result.metadata.get("variants", [])
                            variant_count = len(variants_data)

                            if source_output_dir and source_output_dir.exists():
                                logging.info(f"📁 Copying HLS files from {source_output_dir} to {output_dir}")

                                # Copy all files from source to destination
                                for item in source_output_dir.iterdir():
                                    dest_path = output_dir / item.name
                                    if item.is_file():
                                        shutil.copy2(item, dest_path)
                                        logging.info(f"   Copied: {item.name}")

                                # Cleanup temp directory
                                try:
                                    shutil.rmtree(source_output_dir.parent)
                                    logging.info(f"🧹 Cleaned up temp directory")
                                except Exception as cleanup_err:
                                    logging.warning(f"⚠️ Failed to cleanup temp dir: {cleanup_err}")

                            logging.info(f"✅ Transcoding completed for storage object {storage_object_id}")
                            logging.info(f"   Created {variant_count} variants (path mode)")
                            for v in variants_data:
                                logging.info(f"      - {v.get('name')}: {v.get('resolution')} @ {v.get('bitrate_mbps', 0):.1f} Mbps")
                        else:
                            # ZIP mode: Files already extracted to output_dir
                            variant_count = len(result.variants)
                            logging.info(f"✅ Transcoding completed for storage object {storage_object_id}")
                            logging.info(f"   Created {variant_count} variants (zip mode)")
                            for variant in result.variants:
                                logging.info(f"      - {variant.name}: {variant.resolution} @ {variant.bitrate_mbps:.1f} Mbps")

                        if result.thumbnails:
                            logging.info(f"   Generated {len(result.thumbnails)} thumbnails")

                        # Update database status
                        storage_obj.transcoding_status = "completed"
                        storage_obj.transcoding_progress = 100
                        storage_obj.transcoding_error = None

                        # Set HLS URL (master.m3u8 in output directory)
                        hls_master = output_dir / "master.m3u8"
                        if hls_master.exists():
                            # Construct public URL for HLS master playlist using VOD_BASE_URL
                            tenant_id = getattr(storage_obj, "tenant_id", None) or (storage_obj.metadata_json or {}).get("tenant_id") or "arkturian"
                            base_key = storage_obj.object_key.rsplit(".", 1)[0]  # Remove .mp4 extension
                            vod_base = settings.VOD_BASE_URL
                            storage_obj.hls_url = f"{vod_base}/media/{tenant_id}/{base_key}_transcoded/master.m3u8"
                            logging.info(f"   HLS URL set to: {storage_obj.hls_url}")

                        db_session.commit()
                        logging.info(f"   Database updated - status: completed")
                        return True
                    else:
                        logging.error(f"❌ Transcoding failed for storage object {storage_object_id}: {result.error}")

                        # Update database with failure
                        storage_obj.transcoding_status = "failed"
                        storage_obj.transcoding_error = str(result.error)
                        db_session.commit()
                        logging.info(f"   Database updated - status: failed")
                        return False
                else:
                    logging.error(f"Storage object {storage_object_id} not found in database")
                    return False
            finally:
                db_session.close()

        except Exception as e:
            logging.error(f"❌ Transcoding error for storage object {storage_object_id}: {e}")
            import traceback
            traceback.print_exc()
            return False

    @staticmethod
    async def start_background_transcoding(
        source_path: Path,
        output_dir: Path,
        storage_object_id: int
    ):
        """
        Start transcoding - simplified synchronous ZIP mode

        Sends file to transcoding-api, receives ZIP with HLS files, extracts and updates DB.

        Args:
            source_path: Path to source video
            output_dir: Directory for transcoded files (will be created)
            storage_object_id: Storage object ID for reference
        """
        import httpx
        import zipfile
        import shutil

        try:
            logging.info(f"🎬 Starting transcoding for storage object {storage_object_id}")
            logging.info(f"   Source: {source_path}")
            logging.info(f"   Output: {output_dir}")

            # Check if transcoding is enabled
            if not TRANSCODING_AVAILABLE:
                logging.error("❌ Transcoding package not available")
                return

            if settings.TRANSCODING_MODE.lower() == "disabled":
                logging.error("❌ Transcoding is disabled")
                return

            # Upload to transcoding-api and receive ZIP
            async with httpx.AsyncClient(timeout=600.0) as client:
                with open(source_path, "rb") as f:
                    files = {"file": (source_path.name, f, "video/mp4")}

                    logging.info(f"📤 Uploading to {settings.TRANSCODING_API_URL}/transcode-sync...")

                    response = await client.post(
                        f"{settings.TRANSCODING_API_URL}/transcode-sync",
                        files=files
                    )

                    if response.status_code == 200:
                        # Received ZIP file - extract it
                        output_dir.mkdir(parents=True, exist_ok=True)

                        zip_path = output_dir.parent / f"temp_{storage_object_id}.zip"
                        with open(zip_path, "wb") as zf:
                            zf.write(response.content)

                        logging.info(f"📦 Received ZIP: {len(response.content)} bytes")

                        # Extract ZIP
                        with zipfile.ZipFile(zip_path, 'r') as zipf:
                            zipf.extractall(output_dir)

                        zip_path.unlink()  # Remove temp ZIP

                        files_created = list(output_dir.glob("*"))
                        logging.info(f"✅ Extracted {len(files_created)} files to {output_dir}")

                        # Update database
                        from database import SessionLocal
                        from models import StorageObject

                        db_session = SessionLocal()
                        try:
                            storage_obj = db_session.get(StorageObject, storage_object_id)
                            if storage_obj:
                                storage_obj.transcoding_status = "completed"
                                storage_obj.transcoding_progress = 100
                                storage_obj.transcoding_error = None

                                # Set HLS URL
                                tenant_id = getattr(storage_obj, "tenant_id", None) or (storage_obj.metadata_json or {}).get("tenant_id") or "arkturian"
                                base_key = storage_obj.object_key.rsplit(".", 1)[0]
                                vod_base = settings.VOD_BASE_URL
                                storage_obj.hls_url = f"{vod_base}/media/{tenant_id}/{base_key}_transcoded/master.m3u8"

                                db_session.commit()
                                logging.info(f"✅ Transcoding completed!")
                                logging.info(f"   HLS URL: {storage_obj.hls_url}")
                        finally:
                            db_session.close()

                    else:
                        logging.error(f"❌ Transcoding failed: {response.status_code}")

                        # Update DB with failure
                        from database import SessionLocal
                        from models import StorageObject

                        db_session = SessionLocal()
                        try:
                            storage_obj = db_session.get(StorageObject, storage_object_id)
                            if storage_obj:
                                storage_obj.transcoding_status = "failed"
                                storage_obj.transcoding_error = f"API error: {response.status_code}"
                                db_session.commit()
                        finally:
                            db_session.close()

        except Exception as e:
            logging.error(f"❌ Transcoding error: {e}")
            import traceback
            traceback.print_exc()

    @staticmethod
    def transcode_video_sync(source_path: Path, output_dir: Path, storage_object_id: int) -> bool:
        """
        Synchronous wrapper for transcode_video (for use in Celery tasks)

        Args:
            source_path: Path to source video
            output_dir: Directory for transcoded files
            storage_object_id: Storage object ID

        Returns:
            bool: True if transcoding succeeded
        """
        import asyncio

        # Run async function in event loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(
                TranscodingHelper.transcode_video(source_path, output_dir, storage_object_id)
            )
            return result
        finally:
            loop.close()


# Convenience function for easy import
async def transcode_if_needed(
    source_path: Path,
    mime_type: Optional[str],
    storage_object_id: int,
    output_dir: Optional[Path] = None
) -> bool:
    """
    Transcode video if needed

    Args:
        source_path: Path to uploaded file
        mime_type: MIME type of file
        storage_object_id: Storage object ID
        output_dir: Optional output directory (defaults to source_path parent / basename)

    Returns:
        bool: True if transcoding was triggered
    """
    if not TranscodingHelper.should_transcode(mime_type):
        return False

    if output_dir is None:
        # Default: create directory next to source file
        basename = source_path.stem
        output_dir = source_path.parent / basename

    # Start transcoding in background
    await TranscodingHelper.start_background_transcoding(
        source_path,
        output_dir,
        storage_object_id
    )

    return True
