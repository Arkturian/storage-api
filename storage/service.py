import os
import shutil
import aiofiles
import hashlib
from pathlib import Path
from datetime import datetime
import uuid
import subprocess
import json
from io import BytesIO
from pydantic import ValidationError
import httpx
try:
    import magic
except Exception:
    magic = None
from typing import Optional, Tuple, Dict, Any
from PIL import Image
from storage.pillow_plugins import ensure_heif_support

ensure_heif_support()
import piexif

from storage.external_proxy import fetch_external_file

from config import settings
from database import get_db
from tenancy.config import api_key_for_tenant

# TTS functionality is optional
try:
    from tts_models import SpeechRequest
    import tts_service
    TTS_AVAILABLE = True
except ImportError:
    TTS_AVAILABLE = False
    SpeechRequest = None
    tts_service = None

def _get_gps_from_exif(exif_data: dict) -> Optional[Tuple[float, float]]:
    """Extracts and converts GPS data from EXIF dict to decimal degrees."""
    try:
        gps = exif_data.get("GPS")
        if not gps:
            return None

        def to_deg(value):
            d, m, s = [float(v[0]) / float(v[1]) for v in value]
            return d + (m / 60.0) + (s / 3600.0)

        lat_value = to_deg(gps[piexif.GPSIFD.GPSLatitude])
        lat_ref = gps[piexif.GPSIFD.GPSLatitudeRef].decode('utf-8')
        latitude = lat_value if lat_ref == 'N' else -lat_value

        lon_value = to_deg(gps[piexif.GPSIFD.GPSLongitude])
        lon_ref = gps[piexif.GPSIFD.GPSLongitudeRef].decode('utf-8')
        longitude = lon_value if lon_ref == 'E' else -lon_value
        
        return latitude, longitude
    except (KeyError, IndexError, TypeError, ValueError):
        return None

def _get_gps_from_mp4(file_path: Path) -> tuple[float, float] | None:
    try:
        probe_cmd = [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", str(file_path)
        ]
        result = subprocess.run(probe_cmd, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
        tags = data.get("format", {}).get("tags", {})
        
        location_str = tags.get("location") or tags.get("location-eng") or tags.get("com.apple.quicktime.location.ISO6709")
        
        if location_str:
            location_str = location_str.strip('/')
            if location_str.startswith('+'):
                location_str = location_str[1:]
            
            parts = location_str.replace('-', ' -').replace('+', ' ').split()
            if len(parts) >= 2:
                lat = float(parts[0])
                lon = float(parts[1])
                return lat, lon
    except Exception:
        pass
    return None

class GenericStorageService:
    def __init__(self, base_dir: Optional[str] = None) -> None:
        self.upload_root = Path(base_dir or settings.STORAGE_UPLOAD_DIR)
        # Allow disabling storage initialization when generating OpenAPI or in restricted environments
        if os.getenv("DISABLE_STORAGE_INIT", "0") == "1":
            # Still set directory attributes but avoid creating filesystem paths
            self.media_dir = self.upload_root / "media"
            self.thumbnails_dir = self.upload_root / "thumbnails"
            self.webview_dir = self.upload_root / "webview"
            return

        # Normal initialization with directory creation; fall back gracefully if parent is not writable
        try:
            self.upload_root.mkdir(parents=True, exist_ok=True)
            self.media_dir = self.upload_root / "media"
            self.thumbnails_dir = self.upload_root / "thumbnails"
            self.webview_dir = self.upload_root / "webview"
            self.media_dir.mkdir(exist_ok=True)
            self.thumbnails_dir.mkdir(exist_ok=True)
            self.webview_dir.mkdir(exist_ok=True)
        except Exception:
            # Fallback to local relative path if configured root is not creatable (e.g., read-only /mnt)
            fallback = Path("./uploads/storage")
            try:
                fallback.mkdir(parents=True, exist_ok=True)
            except Exception:
                # Best-effort: if even fallback fails, keep attributes pointing to non-created paths
                self.media_dir = self.upload_root / "media"
                self.thumbnails_dir = self.upload_root / "thumbnails"
                self.webview_dir = self.upload_root / "webview"
                return
            self.upload_root = fallback
            self.media_dir = self.upload_root / "media"
            self.thumbnails_dir = self.upload_root / "thumbnails"
            self.webview_dir = self.upload_root / "webview"
            self.media_dir.mkdir(exist_ok=True)
            self.thumbnails_dir.mkdir(exist_ok=True)
            self.webview_dir.mkdir(exist_ok=True)

    def _get_tenant_dir(self, base_dir: Path, tenant_id: str) -> Path:
        """Get or create tenant-specific subdirectory."""
        tenant_dir = base_dir / tenant_id
        tenant_dir.mkdir(exist_ok=True)
        return tenant_dir

    def _is_audio_only_webm(self, file_path: Path) -> bool:
        """Check if a WebM file contains only audio streams (no video)."""
        return _is_audio_only_webm_helper(file_path)

    def _detect_mime_type(self, data: bytes, original_name: Optional[str] = None) -> str:
        # Prefer libmagic if available
        if magic is not None:
            try:
                return magic.from_buffer(data, mime=True)
            except Exception:
                pass
        # Fallback: guess by extension if libmagic is unavailable
        if original_name:
            ext = Path(original_name).suffix.lower()
            if ext in {".jpg", ".jpeg"}: return "image/jpeg"
            if ext == ".png": return "image/png"
            if ext == ".webp": return "image/webp"
            if ext == ".txt": return "text/plain"
            if ext == ".mp4": return "video/mp4"
            if ext in {".mov", ".qt"}: return "video/quicktime"
            if ext == ".avi": return "video/x-msvideo"
            if ext in {".m4a", ".mp4a"}: return "audio/mp4"
            if ext == ".mp3": return "audio/mpeg"
            if ext == ".wav": return "audio/wav"
        return "application/octet-stream"

    def _checksum(self, data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def _create_filename(self, original_filename: str, owner_user_id: int, context: Optional[str]) -> str:
        # IMPORTANT: For TTS caching, preserve deterministic filenames that start with "tts_"
        # These are cache-friendly identifiers sent by the client
        if original_filename.startswith("tts_") and len(original_filename) > 20:
            # This is a deterministic TTS cache filename - use it as-is
            return original_filename

        # For all other files, generate a unique filename with timestamp and UUID
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        ext = Path(original_filename).suffix.lower()
        uid = str(uuid.uuid4())[:8]
        safe_ctx = (context or "").strip().replace("/", "-").replace(" ", "_")
        prefix = f"u{owner_user_id}"
        if safe_ctx:
            prefix += f"_{safe_ctx}"
        return f"{prefix}_{ts}_{uid}{ext}"

    async def _extract_metadata(self, file_path: Path, mime_type: str, tenant_id: str = "arkturian") -> dict:
        width, height, duration, bit_rate = None, None, None, None
        latitude, longitude = None, None
        thumb_path = None
        webview_path = None
        filename = file_path.name
        gps_data = None
        embedded_title = None

        def _extract_container_title(pdata: dict):
            """Read an embedded title from ffprobe output (container/stream tags).
            Tag keys are case-insensitive across containers (mp4/mov/mkv)."""
            tags = (pdata.get('format') or {}).get('tags') or {}
            t = tags.get('title') or tags.get('TITLE')
            if not t:
                for s in pdata.get('streams', []) or []:
                    st = s.get('tags') or {}
                    t = st.get('title') or st.get('TITLE')
                    if t:
                        break
            return t.strip() if isinstance(t, str) and t.strip() else None

        try:
            if mime_type.startswith("image/"):
                try:
                    print(f"--- Analyzing image for GPS: {file_path}")
                    exif_data = piexif.load(str(file_path))
                    gps_data = _get_gps_from_exif(exif_data)
                except Exception as e: # piexif can fail on images without exif
                    print(f"--- Could not load EXIF data for image {file_path}: {e}")
                    pass

                with Image.open(file_path) as img:
                    width, height = img.size
                    original_format = img.format

                    # GPS fallback via Pillow: piexif only parses JPEG/TIFF and
                    # raises InvalidImageDataError on PNG/WebP/HEIF — so GPS in
                    # a PNG eXIf chunk was silently dropped. Pillow's getexif()
                    # reads the GPS IFD format-independently.
                    if not gps_data:
                        try:
                            _pil_gps = img.getexif().get_ifd(0x8825)
                            # GPS IFD tags: 1=LatRef 2=Lat 3=LonRef 4=Lon; values
                            # are IFDRational triplets (deg, min, sec).
                            if _pil_gps.get(2) and _pil_gps.get(4):
                                def _dms(v):
                                    d, m, s = (float(x) for x in v)
                                    return d + m / 60.0 + s / 3600.0
                                _lat = _dms(_pil_gps[2]) * (-1 if str(_pil_gps.get(1, "N")).strip() == "S" else 1)
                                _lon = _dms(_pil_gps[4]) * (-1 if str(_pil_gps.get(3, "E")).strip() == "W" else 1)
                                gps_data = (_lat, _lon)
                                print(f"--- GPS extracted via Pillow fallback: {gps_data}")
                        except Exception as _pil_gps_exc:
                            print(f"--- Pillow GPS fallback failed: {_pil_gps_exc}")

                    # DEPRECATED: Thumbnails are now generated on-demand via /storage/media endpoint
                    # No longer pre-generating thumbnails at upload time
                    # Old code (kept for reference):
                    # thumb_name = f"thumb_{Path(filename).stem}.jpg"
                    # tenant_thumb_dir = self._get_tenant_dir(self.thumbnails_dir, tenant_id)
                    # thumb_path = tenant_thumb_dir / thumb_name
                    # thumb_img = img.copy()
                    # if thumb_img.mode in ("RGBA", "LA", "P"):
                    #     thumb_img = thumb_img.convert("RGB")
                    # thumb_img.thumbnail((300, 300), Image.Resampling.LANCZOS)
                    # thumb_img.save(thumb_path, "JPEG", quality=85, optimize=True)

                    # DEPRECATED: Webview variants are now generated on-demand via /storage/media endpoint
                    # No longer pre-generating web-optimized versions at upload time
                    # Old code (kept for reference):
                    # file_ext = Path(filename).suffix.lower()
                    # webview_name = f"web_{Path(filename).stem}{file_ext}"
                    # tenant_webview_dir = self._get_tenant_dir(self.webview_dir, tenant_id)
                    # webview_path = tenant_webview_dir / webview_name
                    # if max(width, height) > 1920:
                    #     webview_img = img.copy()
                    #     if width > height:
                    #         new_width = 1920
                    #         new_height = int((height * 1920) / width)
                    #     else:
                    #         new_height = 1920
                    #         new_width = int((width * 1920) / height)
                    #     webview_img = webview_img.resize((new_width, new_height), Image.Resampling.LANCZOS)
                    #     if original_format in ("PNG", "WEBP"):
                    #         webview_img.save(webview_path, original_format, quality=90, optimize=True)
                    #     else:
                    #         webview_img.save(webview_path, "JPEG", quality=90, optimize=True)
                    #     original_size = file_path.stat().st_size
                    #     webview_size = webview_path.stat().st_size
                    #     size_reduction_percent = ((original_size - webview_size) / original_size) * 100
                    #     if webview_size >= original_size * 0.85:
                    #         webview_path.unlink()
                    #         webview_path = None
            
            elif mime_type.startswith("video/"):
                print(f"--- Analyzing video for GPS: {file_path}")
                gps_data = _get_gps_from_mp4(file_path)
                probe_cmd = [
                    "ffprobe", "-v", "quiet", "-print_format", "json",
                    "-show_format", "-show_streams", str(file_path)
                ]
                result = subprocess.run(probe_cmd, capture_output=True, text=True)
                probe_data = json.loads(result.stdout)

                if 'format' in probe_data:
                    duration = float(probe_data['format'].get('duration', 0))
                    bit_rate = int(probe_data['format'].get('bit_rate', 0))

                embedded_title = _extract_container_title(probe_data)
                if embedded_title:
                    print(f"--- Embedded video title found: {embedded_title!r}")

                video_stream = next((s for s in probe_data.get('streams', []) if s.get('codec_type') == 'video'), None)
                if video_stream:
                    width = int(video_stream.get('width', 0))
                    height = int(video_stream.get('height', 0))

                    # DEPRECATED: Video thumbnails are now generated on-demand via /storage/media endpoint
                    # No longer pre-generating thumbnails at upload time using ffmpeg
                    # Old code (kept for reference):
                    # thumb_name = f"thumb_{Path(filename).stem}.jpg"
                    # tenant_thumb_dir = self._get_tenant_dir(self.thumbnails_dir, tenant_id)
                    # thumb_path = tenant_thumb_dir / thumb_name
                    # seek_time = "00:00:00.100" if duration < 2.0 else "00:00:01.000"
                    # os.system(f"ffmpeg -y -i {file_path} -ss {seek_time} -vframes 1 -vf 'scale=300:-1' {thumb_path} > /dev/null 2>&1")
            
            elif mime_type.startswith("audio/"):
                # No GPS for audio, but get other metadata
                probe_cmd = [
                    "ffprobe", "-v", "quiet", "-print_format", "json",
                    "-show_format", "-show_streams", str(file_path)
                ]
                result = subprocess.run(probe_cmd, capture_output=True, text=True)
                probe_data = json.loads(result.stdout)
                if 'format' in probe_data:
                    duration = float(probe_data['format'].get('duration', 0))
                    bit_rate = int(probe_data['format'].get('bit_rate', 0))
                embedded_title = _extract_container_title(probe_data)


            if gps_data:
                print(f"--- GPS data found and assigned: {gps_data}")
                latitude, longitude = gps_data
            else:
                print("--- No GPS data was found or extracted.")

        except Exception as e:
            print(f"Metadata extraction failed for {filename}: {e}")

        # DEPRECATED: No longer storing thumbnail_filename or webview_filename
        # Thumbnails and variants are now generated on-demand via /storage/media endpoint
        # The url_builder will NOT check for these fields - all variants are generated dynamically

        return {
            # "thumbnail_filename": None,  # DEPRECATED: Not needed anymore
            # "webview_filename": None,     # DEPRECATED: Not needed anymore
            "width": width,
            "height": height,
            "duration_seconds": duration,
            "bit_rate": bit_rate,
            "latitude": latitude,
            "longitude": longitude,
            "embedded_title": embedded_title,
        }

    async def save(self, *, data: bytes, original_filename: str, owner_user_id: int, context: Optional[str] = None, tenant_id: str = "arkturian", mime_type: Optional[str] = None) -> dict:
        if len(data) > settings.MAX_FILE_SIZE:
            raise ValueError("File too large")

        # Use provided mime_type if available, otherwise auto-detect
        # Treat application/octet-stream as unknown (curl default when type is not specified)
        mime = mime_type if mime_type and mime_type != "application/octet-stream" else self._detect_mime_type(data, original_filename)
        filename = self._create_filename(original_filename, owner_user_id, context)
        tenant_media_dir = self._get_tenant_dir(self.media_dir, tenant_id)
        file_path = tenant_media_dir / filename

        async with aiofiles.open(file_path, "wb") as f:
            await f.write(data)

        # Audio MIME reconciliation: clients (browser recorders, mobile apps)
        # often mislabel audio uploads — e.g. claim audio/mpeg for what is
        # actually WAV or WebM/Opus. ffprobe the real container and snap to
        # canonical MIME so downstream consumers route correctly.
        mime = reconcile_audio_mime(file_path, mime, original_filename)

        checksum = self._checksum(data)
        metadata = await self._extract_metadata(file_path, mime, tenant_id)

        # No longer storing URLs - they will be built dynamically by the API
        return {
            "object_key": filename,
            "original_filename": original_filename,
            "file_size_bytes": len(data),
            "mime_type": mime,
            "checksum": checksum,
            **metadata,
        }

    async def save_reference(
        self,
        *,
        data: bytes,
        original_filename: str,
        owner_user_id: int,
        context: Optional[str] = None,
        reference_path: str,
        tenant_id: str = "arkturian",
        mime_type: Optional[str] = None,
    ) -> dict:
        """
        Reference mode: Generate thumbnails/variants without copying original file.
        Original file is accessed via reference_path (local filesystem or Samba mount).

        Args:
            data: File content (for generating thumbnails)
            original_filename: Original filename
            owner_user_id: User ID
            context: Optional context string
            reference_path: Filesystem path to existing file (e.g., "/mnt/oneal/2026/Helmets/Airframe.jpg")
            mime_type: Optional explicit MIME (from caller, e.g. derived from reference_path extension).
                       Used when libmagic is unavailable / extension-less filenames.

        Returns:
            Dict with metadata (file_url points to reference_path, not storage)
        """
        if len(data) > settings.MAX_FILE_SIZE:
            raise ValueError("File too large")

        mime = mime_type if mime_type and mime_type != "application/octet-stream" else self._detect_mime_type(data, original_filename)
        checksum = self._checksum(data)

        # Create temporary file for metadata extraction and thumbnail generation
        temp_filename = self._create_filename(original_filename, owner_user_id, context)
        tenant_media_dir = self._get_tenant_dir(self.media_dir, tenant_id)
        temp_file_path = tenant_media_dir / f"temp_{temp_filename}"

        try:
            # Write temporary file
            async with aiofiles.open(temp_file_path, "wb") as f:
                await f.write(data)

            # Reconcile audio MIME against ffprobe (same as save()).
            mime = reconcile_audio_mime(temp_file_path, mime, original_filename)

            # Extract metadata and generate thumbnails/variants
            metadata = await self._extract_metadata(temp_file_path, mime, tenant_id)

            # Delete temporary file (keep only thumbnails/webview)
            temp_file_path.unlink()

        except Exception as e:
            # Clean up temp file on error
            if temp_file_path.exists():
                temp_file_path.unlink()
            raise e

        # Append cache-busting version to URLs
        def _with_version(url: Optional[str]) -> Optional[str]:
            if not url:
                return url
            return f"{url}{'&' if '?' in url else '?'}v={checksum}"

        # Use reference_path as file_url (could be converted to Samba URL later)
        # For now, just store the path - it can be converted to URL by application logic
        file_url = reference_path

        if metadata.get("thumbnail_url"):
            metadata["thumbnail_url"] = _with_version(metadata["thumbnail_url"])
        if metadata.get("webview_url"):
            metadata["webview_url"] = _with_version(metadata["webview_url"])

        # Use "ref_" prefix + UUID as object_key (no actual file in storage)
        object_key = f"ref_{str(uuid.uuid4())[:12]}_{Path(original_filename).suffix}"

        return {
            "object_key": object_key,
            "file_url": file_url,  # Points to reference_path
            "original_filename": original_filename,
            "file_size_bytes": len(data),
            "mime_type": mime,
            "checksum": checksum,
            **metadata,
        }

    def absolute_path_for_key(self, object_key: str, tenant_id: str = "arkturian") -> Path:
        """Get absolute path for object_key, supporting both old (flat) and new (tenant-based) structure."""
        # Try tenant-based path first (new structure)
        tenant_path = self.media_dir / tenant_id / object_key
        if tenant_path.exists():
            return tenant_path
        # Fallback to flat structure for backward compatibility
        flat_path = self.media_dir / object_key
        if flat_path.exists():
            return flat_path
        # Default to tenant-based path for new files
        return tenant_path

    def ensure_tenant_directories(self, tenant_id: str) -> list[str]:
        created: list[str] = []
        for base_dir in (self.media_dir, self.thumbnails_dir, self.webview_dir):
            try:
                target = base_dir / tenant_id
                target.mkdir(parents=True, exist_ok=True)
                created.append(str(target))
            except Exception:
                pass
        return created

    def delete_tenant_directories(self, tenant_id: str) -> list[str]:
        removed: list[str] = []
        for base_dir in (self.media_dir, self.thumbnails_dir, self.webview_dir):
            target = base_dir / tenant_id
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
                removed.append(str(target))
        return removed

    def _delete_physical_assets(self, path: Path, tenant_id: str) -> None:
        """Delete original file and derived assets (thumbs/webview/HLS)."""
        if path.exists():
            path.unlink()

        actual_tenant_id = tenant_id
        if path.parent.name != "media":
            actual_tenant_id = path.parent.name

        thumb = self.thumbnails_dir / actual_tenant_id / f"thumb_{path.stem}.jpg"
        if thumb.exists():
            thumb.unlink()

        web = self.webview_dir / actual_tenant_id / f"web_{path.stem}{path.suffix}"
        if web.exists():
            web.unlink()

        hls_dir = self.media_dir / actual_tenant_id / path.stem
        if hls_dir.exists() and hls_dir.is_dir():
            shutil.rmtree(hls_dir, ignore_errors=True)

    def delete(self, object_key: str, tenant_id: str = "arkturian") -> bool:
        try:
            path = self.absolute_path_for_key(object_key, tenant_id)
            self._delete_physical_assets(path, tenant_id)
            return True
        except Exception:
            pass
        return False

    async def update_file(self, object_key: str, data: bytes, tenant_id: str = "arkturian") -> dict:
        if len(data) > settings.MAX_FILE_SIZE:
            raise ValueError("File too large")

        file_path = self.absolute_path_for_key(object_key, tenant_id)
        # Remove existing assets to avoid stale cache
        if file_path.exists():
            self._delete_physical_assets(file_path, tenant_id)

        # Ensure parent dir exists before writing
        file_path.parent.mkdir(parents=True, exist_ok=True)

        mime = self._detect_mime_type(data, file_path.name)

        async with aiofiles.open(file_path, "wb") as f:
            await f.write(data)

        checksum = self._checksum(data)

        # Determine which tenant directory the file is actually in
        actual_tenant_id = tenant_id
        if file_path.parent.name != "media":
            # File is in a tenant subdirectory
            actual_tenant_id = file_path.parent.name

        metadata = await self._extract_metadata(file_path, mime, actual_tenant_id)

        # Append cache-busting version to URLs
        def _with_version(url: Optional[str]) -> Optional[str]:
            if not url:
                return url
            return f"{url}{'&' if '?' in url else '?'}v={checksum}"
        versioned_file_url = f"{settings.BASE_URL}/uploads/storage/media/{actual_tenant_id}/{object_key}?v={checksum}"
        if metadata.get("thumbnail_url"):
            metadata["thumbnail_url"] = _with_version(metadata["thumbnail_url"])
        if metadata.get("webview_url"):
            metadata["webview_url"] = _with_version(metadata["webview_url"])

        return {
            "file_url": versioned_file_url,
            "file_size_bytes": len(data),
            "mime_type": mime,
            "checksum": checksum,
            **metadata,
        }

generic_storage = GenericStorageService()


async def _fetch_media_variant_via_http(
    storage_obj,
    tenant_id: str,
    max_edge: int,
    target_format: str,
    quality: int,
) -> Optional[Tuple[bytes, str, Dict[str, Any]]]:
    """Attempt to fetch an optimized media variant via the public storage API."""

    object_id = getattr(storage_obj, "id", None)
    if not object_id:
        return None

    base_url = settings.BASE_URL.rstrip("/")
    media_url = f"{base_url}/storage/media/{object_id}"

    params = {}
    if max_edge:
        params["width"] = max_edge
    if target_format:
        params["format"] = target_format
    if quality:
        params["quality"] = quality

    headers = {}
    tenant_key = api_key_for_tenant(tenant_id)
    if tenant_key:
        headers["X-API-KEY"] = tenant_key
    else:
        default_key = getattr(settings, "API_KEY", None)
        if default_key:
            headers["X-API-KEY"] = default_key

    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            response = await client.get(media_url, params=params, headers=headers)
            response.raise_for_status()
            content_type = response.headers.get(
                "content-type",
                f"image/{'jpeg' if target_format in {'jpg', 'jpeg'} else target_format}"
            )
            return response.content, content_type, {}
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None
        raise
    except Exception:
        return None

def _compute_trim_bounds(img: Image.Image) -> Optional[Tuple[int, int, int, int]]:
    width, height = img.size
    if width == 0 or height == 0:
        return None

    if "A" in img.getbands():
        alpha = img.getchannel("A")
        bbox = alpha.getbbox()
        if bbox:
            return bbox

    gray = img.convert("L")
    # Threshold: treat near-white as background
    mask = gray.point(lambda p: 255 if p < 250 else 0)
    bbox = mask.getbbox()
    if bbox:
        return bbox
    return None


async def load_image_bytes_for_analysis(
    storage_obj,
    tenant_id: Optional[str] = None,
    *,
    max_edge: int = 1300,
    target_format: str = "webp",
    quality: int = 75,
    trim_config: Optional[Dict[str, Any]] = None,
) -> Tuple[bytes, str, Dict[str, Any]]:
    """Load an optimized image variant suitable for AI analysis.

    Falls back to fetching external references when the local media file is missing.
    Returns image bytes and the associated MIME type. Raises FileNotFoundError if
    the source cannot be resolved.
    """

    target_format = (target_format or "webp").lower()
    if target_format not in {"jpg", "jpeg", "png", "webp"}:
        target_format = "webp"

    mime_type = getattr(storage_obj, "mime_type", "") or ""
    if not mime_type.lower().startswith("image/"):
        raise ValueError("Storage object is not an image")

    tenant = tenant_id or getattr(storage_obj, "tenant_id", None)
    if not tenant:
        metadata = getattr(storage_obj, "metadata_json", None) or {}
        tenant = metadata.get("tenant_id", "arkturian")

    object_key = getattr(storage_obj, "object_key", None)
    src_path: Optional[Path] = None
    if object_key:
        candidate = generic_storage.absolute_path_for_key(object_key, tenant)
        if candidate.exists():
            src_path = candidate

    image_source: Optional[Any] = None
    if src_path and src_path.exists():
        image_source = src_path
    else:
        external_uri = getattr(storage_obj, "external_uri", None)
        if external_uri:
            data, _metadata = await fetch_external_file(external_uri, use_cache=True)
            image_source = BytesIO(data)
        else:
            fallback = await _fetch_media_variant_via_http(
                storage_obj,
                tenant,
                max_edge,
                target_format,
                quality,
            )
            if fallback:
                data_bytes, _, _ = fallback
                image_source = BytesIO(data_bytes)
            else:
                raise FileNotFoundError(f"Image not found for storage object {getattr(storage_obj, 'id', 'unknown')}")

    apply_trim = bool(trim_config and trim_config.get("enabled"))
    trim_meta: Dict[str, Any] = {}

    with Image.open(image_source) as img:
        original_width, original_height = img.size
        img_format = target_format

        if apply_trim:
            bbox = _compute_trim_bounds(img)
            if bbox:
                x1, y1, x2, y2 = bbox
                x1 = max(0, min(x1, original_width))
                y1 = max(0, min(y1, original_height))
                x2 = max(0, min(x2, original_width))
                y2 = max(0, min(y2, original_height))
                if x2 > x1 and y2 > y1 and (x1 != 0 or y1 != 0 or x2 != original_width or y2 != original_height):
                    img = img.crop((x1, y1, x2, y2))
                    trim_meta.update({
                        "applied": True,
                        "x1": x1,
                        "y1": y1,
                        "x2": x2,
                        "y2": y2,
                        "trim_width": x2 - x1,
                        "trim_height": y2 - y1,
                        "normalized": [
                            x1 / original_width if original_width else 0,
                            y1 / original_height if original_height else 0,
                            x2 / original_width if original_width else 0,
                            y2 / original_height if original_height else 0,
                        ],
                    })

        if max_edge:
            w, h = img.size
            if w > 0 and h > 0:
                if w >= h:
                    target_w = min(max_edge, w)
                    target_h = int(h * (target_w / float(w)))
                else:
                    target_h = min(max_edge, h)
                    target_w = int(w * (target_h / float(h)))
                if target_w > 0 and target_h > 0 and (target_w != w or target_h != h):
                    img = img.resize((target_w, target_h), Image.Resampling.LANCZOS)

        if img_format in {"jpg", "jpeg"} and img.mode in {"RGBA", "LA", "P"}:
            img = img.convert("RGB")

        out_buffer = BytesIO()
        save_kwargs = {}
        if img_format in {"jpg", "jpeg"}:
            save_kwargs = {"quality": quality, "optimize": True}
            mime_out = "image/jpeg"
            img.save(out_buffer, format="JPEG", **save_kwargs)
        elif img_format == "png":
            mime_out = "image/png"
            img.save(out_buffer, format="PNG")
        else:  # webp default
            save_kwargs = {"quality": quality, "method": 6}
            mime_out = "image/webp"
            img.save(out_buffer, format="WEBP", **save_kwargs)

        trim_meta.setdefault("width", original_width)
        trim_meta.setdefault("height", original_height)
        if "applied" not in trim_meta:
            trim_meta["applied"] = False
        if "normalized" not in trim_meta and original_width and original_height:
            trim_meta["normalized"] = [0.0, 0.0, 1.0, 1.0]
        if "trim_width" not in trim_meta:
            trim_meta["trim_width"] = trim_meta.get("width")
        if "trim_height" not in trim_meta:
            trim_meta["trim_height"] = trim_meta.get("height")

        return out_buffer.getvalue(), mime_out, trim_meta


def extract_thumbnails_for_ai(video_path: Path, output_dir: Path, n_frames: int = 5) -> int:
    """
    Extract `n_frames` thumbnails uniformly spaced across the FULL video duration
    for AI safety analysis. The previous implementation used `-vf fps=2 -vframes 5`
    which only sampled the first ~2.5 seconds — a 10-minute video with unsafe
    content in the middle was effectively invisible to the safety check.

    Strategy:
      1. ffprobe the duration.
      2. Sample at timestamps `D * (i + 0.5) / n` for i in 0..n-1 → frames at
         10%, 30%, 50%, 70%, 90% of the video (for n=5). Avoids cold-opens and
         end-credits while covering the body of the video.
      3. Each frame extracted with `-ss <ts> -frames:v 1`, scaled to 1280x720,
         saved as JPEG.

    Falls back to the old "first frames" strategy if ffprobe fails (very short
    or malformed videos).

    Returns 0 on success (at least one frame written), non-zero on total failure.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    n_frames = max(1, n_frames)

    # Probe duration
    duration_s = 0.0
    try:
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ],
            capture_output=True, text=True, timeout=10,
        )
        if probe.returncode == 0 and probe.stdout.strip():
            duration_s = float(probe.stdout.strip())
    except Exception:
        duration_s = 0.0

    # Very short / unknown duration → fall back to first-frames sampling
    if duration_s < 1.0:
        cmd = [
            "ffmpeg", "-y", "-i", str(video_path),
            "-vf", "fps=2",
            "-vframes", str(n_frames),
            "-s", "1280x720",
            str(output_dir / "thumb_%02d.jpg"),
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=120)
        return result.returncode

    # Uniform sampling across full duration
    timestamps = [duration_s * (i + 0.5) / n_frames for i in range(n_frames)]
    written = 0
    for idx, ts in enumerate(timestamps, start=1):
        out_path = output_dir / f"thumb_{idx:02d}.jpg"
        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{ts:.3f}",
            "-i", str(video_path),
            "-frames:v", "1",
            "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2",
            "-q:v", "2",
            str(out_path),
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=30)
            if r.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0:
                written += 1
        except subprocess.TimeoutExpired:
            continue
    return 0 if written > 0 else 1


def validate_and_extract_hls_zip(zip_path: Path, extract_dir: Path) -> dict:
    """
    Validates and extracts a pre-transcoded HLS video zip file.
    
    Args:
        zip_path: Path to the zip file
        extract_dir: Directory to extract contents to
        
    Returns:
        dict with validation results and metadata
    """
    import zipfile
    
    try:
        extract_dir.mkdir(parents=True, exist_ok=True)
        
        with zipfile.ZipFile(zip_path, 'r') as zip_file:
            # Extract all files
            zip_file.extractall(extract_dir)
            
            # Check for required HLS files
            master_m3u8 = extract_dir / "master.m3u8"
            if not master_m3u8.exists():
                return {"valid": False, "error": "Missing master.m3u8 file"}
            
            # Parse master.m3u8 to find quality streams and extract metadata
            quality_streams = []
            ts_files = []
            max_width = 0
            max_height = 0
            max_bitrate = 0
            duration_seconds = 0.0
            
            with open(master_m3u8, 'r') as f:
                lines = f.readlines()
                for i, line in enumerate(lines):
                    if line.startswith('#EXT-X-STREAM-INF:'):
                        # Extract resolution and bandwidth from stream info
                        if 'RESOLUTION=' in line:
                            res_part = line.split('RESOLUTION=')[1].split(',')[0] if ',' in line.split('RESOLUTION=')[1] else line.split('RESOLUTION=')[1]
                            try:
                                w, h = map(int, res_part.strip().split('x'))
                                if w * h > max_width * max_height:
                                    max_width, max_height = w, h
                            except:
                                pass
                        if 'BANDWIDTH=' in line:
                            try:
                                bandwidth = int(line.split('BANDWIDTH=')[1].split(',')[0])
                                max_bitrate = max(max_bitrate, bandwidth)
                            except:
                                pass
                        # Next line should contain the quality m3u8 file
                        if i + 1 < len(lines):
                            quality_file = lines[i + 1].strip()
                            quality_path = extract_dir / quality_file
                            if quality_path.exists():
                                quality_streams.append(quality_file)
                                
                                # Parse individual quality m3u8 to find .ts files and extract duration
                                with open(quality_path, 'r') as qf:
                                    qlines = qf.readlines()
                                    for qline in qlines:
                                        if qline.strip().endswith('.ts'):
                                            ts_file = qline.strip()
                                            ts_path = extract_dir / ts_file
                                            if ts_path.exists():
                                                ts_files.append(ts_file)
                                            else:
                                                return {"valid": False, "error": f"Missing .ts file: {ts_file}"}
                                        elif qline.startswith('#EXTINF:') and duration_seconds == 0.0:
                                            # Extract duration from first segment of highest quality stream
                                            try:
                                                duration_seconds = float(qline.split('#EXTINF:')[1].split(',')[0])
                                            except:
                                                pass
            
            if not quality_streams:
                return {"valid": False, "error": "No valid quality streams found in master.m3u8"}
            
            # Calculate total HLS file size
            total_size = sum(f.stat().st_size for f in extract_dir.glob('*') if f.is_file())
            
            # Write size info
            (extract_dir / "hls_size.txt").write_text(str(total_size))
            
            return {
                "valid": True,
                "quality_streams": quality_streams,
                "ts_files": ts_files,
                "total_size": total_size,
                "file_count": len(list(extract_dir.glob('*'))),
                "width": max_width if max_width > 0 else None,
                "height": max_height if max_height > 0 else None,
                "duration_seconds": duration_seconds if duration_seconds > 0 else None,
                "bit_rate": max_bitrate if max_bitrate > 0 else None,
            }
            
    except Exception as e:
        return {"valid": False, "error": f"Zip extraction failed: {str(e)}"}


def _is_audio_only_webm_helper(file_path: Path) -> bool:
    """Check if a WebM file contains only audio streams (no video)."""
    try:
        result = subprocess.run([
            "ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
            "-of", "csv=p=0", str(file_path)
        ], capture_output=True, text=True, timeout=10)

        if result.returncode == 0:
            streams = result.stdout.strip().split('\n')
            # Check if all streams are audio and none are video
            has_audio = False
            for stream in streams:
                if stream.strip() == "video":
                    return False  # Has video, not audio-only
                elif stream.strip() == "audio":
                    has_audio = True
            return has_audio  # Only audio streams found
        return False
    except Exception as e:
        print(f"!!! WARNING: Failed to check WebM stream types: {e}")
        return False


# Map (ffprobe format_name, first-audio-codec) to canonical MIME.
# format_name comes back comma-separated when the container is ambiguous
# (e.g. "mov,mp4,m4a,3gp,3g2,mj2") — we treat the set as fingerprint.
def _canonical_audio_mime(format_name: str, audio_codec: Optional[str]) -> Optional[str]:
    fmt = {p.strip() for p in (format_name or "").split(",") if p.strip()}
    codec = (audio_codec or "").lower()

    if "mp3" in fmt:
        return "audio/mpeg"
    if "wav" in fmt:
        return "audio/wav"
    if "flac" in fmt:
        return "audio/flac"
    if "aac" in fmt:
        return "audio/aac"
    if fmt & {"matroska", "webm"}:
        return "audio/webm"
    if "ogg" in fmt:
        if codec == "opus":
            return "audio/opus"
        return "audio/ogg"
    if fmt & {"mp4", "m4a", "mov", "3gp", "3g2", "mj2"}:
        # MP4 family; codec disambiguates
        if codec == "alac":
            return "audio/mp4"
        return "audio/mp4"  # AAC inside m4a/mp4 is audio/mp4 per RFC 4337
    return None


def _detect_true_audio_mime(file_path: Path) -> Optional[str]:
    """
    Run ffprobe and return the canonical audio MIME based on container +
    audio codec. Returns None if the file is not audio-only (has video
    streams) or if ffprobe fails — caller keeps the claimed MIME.

    Catches the case where a client mislabels an upload (e.g. claims
    audio/mpeg but the bytes are actually WAV or WebM-Opus from a browser
    recorder). Auto-correction at upload time keeps downstream consumers
    (transcoder, AI analyzer, MIME-based routing) consistent.
    """
    try:
        result = subprocess.run([
            "ffprobe", "-v", "error",
            "-print_format", "json",
            "-show_format", "-show_streams",
            str(file_path),
        ], capture_output=True, text=True, timeout=15)
        if result.returncode != 0 or not result.stdout:
            return None
        data = json.loads(result.stdout)
    except Exception as e:
        print(f"⚠️  ffprobe failed for audio MIME detection on {file_path}: {e}")
        return None

    streams = data.get("streams") or []
    # Reject anything with a video stream — that's a video file, not audio
    if any(s.get("codec_type") == "video" for s in streams):
        return None
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    if not audio_streams:
        return None
    first_codec = audio_streams[0].get("codec_name")
    format_name = (data.get("format") or {}).get("format_name") or ""
    return _canonical_audio_mime(format_name, first_codec)


# Extensions / declared MIMEs that should be cross-checked against the
# actual file bytes via ffprobe. Anything outside this set is left
# untouched (we don't want to ffprobe every image / 3D / doc upload).
_AUDIO_EXTENSIONS = {
    ".mp3", ".m4a", ".mp4a", ".aac", ".wav", ".wave",
    ".ogg", ".opus", ".oga", ".flac", ".webm",
}


def _claims_audio(claimed_mime: Optional[str], original_filename: Optional[str]) -> bool:
    if claimed_mime and claimed_mime.lower().startswith("audio/"):
        return True
    if original_filename:
        ext = Path(original_filename).suffix.lower()
        if ext in _AUDIO_EXTENSIONS:
            return True
    return False


def reconcile_audio_mime(
    file_path: Path,
    claimed_mime: Optional[str],
    original_filename: Optional[str],
) -> str:
    """
    If the upload looks like audio (caller mime is audio/* or extension is
    a known audio type), verify against ffprobe and return the canonical
    MIME. Falls back to claimed_mime when ffprobe is inconclusive or the
    file is not audio.

    Use this in upload paths AFTER the file is written to disk.
    """
    fallback = claimed_mime or "application/octet-stream"
    if not _claims_audio(claimed_mime, original_filename):
        return fallback
    detected = _detect_true_audio_mime(file_path)
    if detected and detected != claimed_mime:
        print(
            f"🎵 audio MIME corrected: claimed={claimed_mime!r} "
            f"detected={detected!r} file={file_path.name}"
        )
        return detected
    return fallback


def bulk_delete_objects(
    db,
    name: Optional[str] = None,
    collection_like: Optional[str] = None,
    context_like: Optional[str] = None,
    tenant_id: Optional[str] = None,
    current_user = None
) -> int:
    """
    Finds and deletes storage objects matching filter criteria, including their physical files
    and any other files that share a link_id with the found items.
    """
    from models import StorageObject, User
    import shutil

    # Step 1: Find initial objects based on filters
    q = db.query(StorageObject)
    if collection_like:
        q = q.filter(StorageObject.collection_id.ilike(f"%{collection_like}%"))
    if name:
        q = q.filter(StorageObject.original_filename.ilike(f"%{name}%"))
    if context_like:
        q = q.filter(StorageObject.context.ilike(f"%{context_like}%"))

    if tenant_id:
        q = q.filter(StorageObject.tenant_id == tenant_id)

    if current_user and current_user.trust_level != "admin" and not tenant_id:
        q = q.filter(StorageObject.owner_user_id == current_user.id)

    initial_objects = q.all()
    if not initial_objects:
        return 0

    # Step 2: Collect all unique link_ids from the initial set
    link_ids_to_find = {obj.link_id for obj in initial_objects if obj.link_id}

    # Step 3: Find all objects that share those link_ids
    all_objects_to_delete_map = {obj.id: obj for obj in initial_objects}
    if link_ids_to_find:
        linked_objects = db.query(StorageObject).filter(StorageObject.link_id.in_(link_ids_to_find)).all()
        for obj in linked_objects:
            all_objects_to_delete_map[obj.id] = obj # Use map to ensure uniqueness

    final_objects_to_delete = list(all_objects_to_delete_map.values())
    
    # Step 4: Perform deletion
    deleted_count = 0
    object_ids_to_delete = []

    for obj in final_objects_to_delete:
        try:
            # Extract tenant_id from obj or default to arkturian
            tenant_id = getattr(obj, 'tenant_id', 'arkturian')
            if not tenant_id and hasattr(obj, 'metadata_json') and obj.metadata_json:
                tenant_id = getattr(obj, 'tenant_id', None) or (obj.metadata_json or {}).get('tenant_id') or 'arkturian'
            if not tenant_id:
                tenant_id = 'arkturian'

            generic_storage.delete(obj.object_key, tenant_id)
            object_ids_to_delete.append(obj.id)
            deleted_count += 1
        except Exception as e:
            print(f"Error deleting file for object {obj.id} ({obj.object_key}): {e}")

    if object_ids_to_delete:
        # Delete embeddings from Knowledge Graph for all objects
        try:
            from knowledge_graph.vector_store import vector_store
            for obj_id in object_ids_to_delete:
                try:
                    vector_store.delete_embedding(obj_id)
                except Exception as e:
                    print(f"Warning: Could not delete embedding for object {obj_id}: {e}")
            print(f"🗑️  Deleted {len(object_ids_to_delete)} embeddings from Knowledge Graph")
        except Exception as e:
            print(f"Warning: Could not clean up embeddings: {e}")

        db.query(StorageObject).filter(StorageObject.id.in_(object_ids_to_delete)).delete(synchronize_session=False)
        db.commit()

    return deleted_count


async def _process_tts_request_from_storage(storage_obj, tts_request):
    """
    Internal function to process a TTS request from a storage file.
    This avoids calling the public API endpoint and prevents recursion.
    """
    if not TTS_AVAILABLE:
        print(f"--- TTS Hook SKIPPED: TTS functionality not available (tts_models/tts_service not found)")
        return

    audio_bytes = None
    db_session = next(get_db())
    try:
        from storage.domain import save_file_and_record
        print(f"--- TTS Hook: Processing TTS request ID {tts_request.id} from Storage Object {storage_obj.id}")
        
        # Re-use the logic from the main API endpoint
        if tts_request.config.provider == "openai":
            config = tts_request.config.openai or tts_service.OpenAITTSConfig()
            config.voice = tts_request.content.voice
            config.speed = tts_request.content.speed
            config.output_format = tts_request.config.output_format
            audio_bytes = await tts_service.generate_openai_tts(tts_request.content.text, config)

        elif tts_request.config.provider == "gemini":
            from google.cloud import texttospeech
            client = texttospeech.TextToSpeechAsyncClient()
            synthesis_input = texttospeech.SynthesisInput(text=tts_request.content.text)
            voice_params = texttospeech.VoiceSelectionParams(language_code=tts_request.content.language, name=tts_request.content.voice)
            audio_config = texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.MP3,
                speaking_rate=tts_request.content.speed,
                pitch=tts_request.content.pitch
            )
            response = await client.synthesize_speech(input=synthesis_input, voice=voice_params, audio_config=audio_config)
            audio_bytes = response.audio_content

        elif tts_request.config.provider == "elevenlabs":
            config = tts_request.config.elevenlabs or tts_service.ElevenLabsTTSConfig()
            config.voice_id = tts_request.content.voice
            if tts_request.content.stability is not None:
                config.stability = tts_request.content.stability
            if tts_request.content.clarity is not None:
                config.clarity = tts_request.content.clarity
            audio_bytes = await tts_service.generate_elevenlabs_tts(tts_request.content.text, config)

        if not audio_bytes:
            print(f"--- TTS Hook ERROR: TTS generation failed for request {tts_request.id}, no audio data produced.")
            return

        # --- Save the generated audio directly to storage and DB ---
        filename = f"tts_{tts_request.id}_{tts_request.config.provider}.{tts_request.config.output_format}"
        
        saved_audio_obj = await save_file_and_record(
            db=db_session,
            owner_user_id=storage_obj.owner_user_id,
            data=audio_bytes,
            original_filename=filename,
            context="tts-generation-hook",
            is_public=storage_obj.is_public,
            collection_id=storage_obj.collection_id,
            link_id=storage_obj.link_id or tts_request.id # Use original link_id or request id
        )
        
        print(f"--- TTS Hook SUCCESS: Saved audio to Storage Object ID {saved_audio_obj.id} (linked to Text ID {storage_obj.id})")

        # --- (Optional) Generate a title image ---
        if tts_request.config.generate_title_image:
            print(f"--- Image Gen Hook: Requesting title image prompt for TTS request {tts_request.id}")
            try:
                from main import generate_image_endpoint, ImageGenRequest

                # Generate the title-image prompt via api-ai's CLI-backed text
                # endpoint — NOT the google-generativeai SDK. Per project policy
                # Gemini may only run via CLI subprocess, never the billable
                # Google API/SDK. Reuses the same /ai/chatgpt path as the safety
                # pipeline (subscription-backed, no per-call billing).
                _ai_api_base = os.getenv("AI_API_BASE_URL", "http://127.0.0.1:8000")
                _ai_api_key = os.getenv("AI_INTERNAL_API_KEY", "Inetpass1")
                image_prompt_request = (
                    "Based on the following dialog, create a short, visually descriptive prompt for a text-to-image AI. "
                    "The prompt should capture the essence and mood of the conversation in a single scene. "
                    "Describe the scene, characters, and atmosphere. Return only the prompt text."
                    f"\n\nDIALOG:\n{tts_request.content.text}"
                )
                async with httpx.AsyncClient(timeout=60.0) as _prompt_client:
                    _prompt_resp = await _prompt_client.post(
                        f"{_ai_api_base}/ai/chatgpt",
                        json={"prompt": image_prompt_request},
                        headers={"X-API-KEY": _ai_api_key, "Content-Type": "application/json"},
                    )
                    _prompt_resp.raise_for_status()
                    _prompt_body = _prompt_resp.json()
                image_prompt = (_prompt_body.get("response") or _prompt_body.get("message") or "").strip()
                
                print(f"--- Image Gen Hook: Received prompt: '{image_prompt}'")
                
                # Call the image generation endpoint internally
                await generate_image_endpoint(
                    ImageGenRequest(
                        prompt=image_prompt, 
                        link_id=storage_obj.link_id or tts_request.id, 
                        owner_user_id=storage_obj.owner_user_id
                    ),
                    "Inetpass1", # Internal API Key
                    db_session
                )
            except Exception as img_e:
                print(f"--- Image Gen Hook CRITICAL ERROR: Failed to generate image for TTS request {tts_request.id}. Error: {img_e}")

    except Exception as e:
        print(f"--- TTS Hook CRITICAL ERROR: Failed processing TTS request {tts_request.id}. Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        db_session.close()


async def enqueue_ai_safety_and_transcoding(storage_obj, db=None, skip_ai_safety: bool = False, ai_mode: str = "full") -> None:
    """
    Centralized function to handle AI safety analysis and transcoding
    for all media types. Used by all upload endpoints.

    Args:
        storage_obj: StorageObject instance with mime_type, id, and object_key
        db: SQLAlchemy session to use for database operations
        skip_ai_safety: Legacy parameter, use ai_mode="none" instead
        ai_mode: AI analysis mode (none, safety, vision, full)
    """
    # Pre-processing scratch dir: NOT in /tmp — the nightly cleanup_tmp.sh
    # cron wipes /tmp at midnight and would corrupt in-flight Celery tasks
    # whose retries can stretch over hours. Keep alongside other storage data.
    _AI_SCRATCH_ROOT = Path(os.getenv("AI_SCRATCH_DIR", "/var/lib/storage-api/tmp-ai"))
    try:
        _AI_SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    except Exception:
        # Fallback to /tmp if we can't create the persistent dir (e.g. read-only fs in tests)
        _AI_SCRATCH_ROOT = Path("/tmp")
    import logging
    logger = logging.getLogger(__name__)
    logger.error(f"🔥 enqueue_ai_safety_and_transcoding CALLED for storage object {storage_obj.id}")
    logger.error(f"   mime_type: {storage_obj.mime_type}")
    logger.error(f"   skip_ai_safety: {skip_ai_safety}")
    logger.error(f"   ai_mode: {ai_mode}")

    # Convert legacy skip_ai_safety to ai_mode
    if skip_ai_safety:
        ai_mode = "none"

    # Skip AI analysis for markup/source-code contexts that are clearly not
    # user-uploaded media (3DPresenter Markdown, JSON config, etc.). The
    # safety pipeline is built around image/video moderation; gating these
    # text payloads gives no signal and stalls legitimate non-media flows.
    SAFETY_BYPASS_CONTEXTS = {"presentation", "config", "template", "schema"}
    SAFETY_BYPASS_MIMES = {
        "text/markdown", "text/x-markdown", "text/csv", "text/plain",
        "application/json", "application/xml", "text/xml", "text/yaml",
        "application/x-yaml",
    }
    ctx = (storage_obj.context or "").lower()
    mime_lc = (storage_obj.mime_type or "").lower()
    fname_lc = (storage_obj.original_filename or "").lower()
    is_markup = (
        ctx in SAFETY_BYPASS_CONTEXTS
        or mime_lc in SAFETY_BYPASS_MIMES
        or fname_lc.endswith((".md", ".markdown", ".json", ".yaml", ".yml", ".xml", ".toml", ".csv"))
    )
    if is_markup and ai_mode != "none":
        # Mark as completed/safe so the quarantine gate lets the asset
        # through. We don't run an actual safety check — the content shape
        # (markup, no embedded media) makes the risk negligible.
        logger.info(
            f"⏭️  Bypassing safety analysis for markup/config "
            f"(ctx={storage_obj.context!r} mime={storage_obj.mime_type!r}) "
            f"on object {storage_obj.id}"
        )
        try:
            from database import SessionLocal as _SL
            from models import StorageObject as _SO
            _bp_db = _SL()
            try:
                _bp_obj = _bp_db.query(_SO).filter(_SO.id == storage_obj.id).first()
                if _bp_obj is not None:
                    _bp_obj.ai_safety_status = "completed"
                    _bp_obj.ai_safety_rating = "safe"
                    _bp_obj.ai_danger_potential = 1
                    _bp_obj.safety_info = {
                        "isSafe": True,
                        "confidence": 1.0,
                        "reasoning": "Markup/config bypass — not subject to media safety pipeline.",
                        "flags": [],
                        "version": "v2",
                        "bypass_reason": "markup_context",
                    }
                    _bp_db.commit()
            finally:
                _bp_db.close()
        except Exception as _bp_exc:
            print(f"⚠️ Markup safety-bypass write failed: {_bp_exc}")
        return

    # Skip AI analysis for 3D model files (binary formats that can't be analyzed by vision AI)
    is_3d_model = False
    if storage_obj.mime_type:
        # Check MIME type
        if storage_obj.mime_type.startswith("model/") or \
           storage_obj.mime_type in ("application/octet-stream", "application/x-3ds"):
            is_3d_model = True

    # Also check file extension as fallback (some servers don't set correct MIME for 3D files)
    if storage_obj.original_filename:
        filename_lower = storage_obj.original_filename.lower()
        if filename_lower.endswith(('.glb', '.gltf', '.obj', '.fbx', '.dae', '.3ds', '.stl', '.blend', '.usdz')):
            is_3d_model = True

    if is_3d_model:
        logger.error(f"⏭️  Skipping AI analysis for 3D model file: {storage_obj.original_filename} (mime: {storage_obj.mime_type})")
        print(f"⏭️  3D model detected - skipping AI analysis for {storage_obj.original_filename}")
        return

    try:
        import shutil
        import httpx
        # `Path` already imported at module level; the previous local re-import
        # caused Python to treat Path as a function-local variable, which made
        # the earlier `_AI_SCRATCH_ROOT = Path(...)` line raise UnboundLocalError
        # on every upload.
        from config import settings

        # Extract tenant_id from storage_obj or default to arkturian
        tenant_id = getattr(storage_obj, 'tenant_id', 'arkturian')
        if not tenant_id and storage_obj.metadata_json:
            tenant_id = getattr(storage_obj, 'tenant_id', None) or (storage_obj.metadata_json or {}).get('tenant_id') or 'arkturian'
        if not tenant_id:
            tenant_id = 'arkturian'

        # Handle external storage mode: download file temporarily for AI analysis
        temp_file_for_external = None
        storage_mode = getattr(storage_obj, 'storage_mode', 'copy')

        if storage_mode == "external":
            external_uri = getattr(storage_obj, 'external_uri', None)
            if not external_uri:
                print(f"⚠️  External storage object {storage_obj.id} has no external_uri, skipping AI analysis")
                return

            print(f"🌐 [EXTERNAL MODE] Downloading file for AI analysis from: {external_uri}")
            try:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    response = await client.get(external_uri)
                    response.raise_for_status()

                    # Save to temporary file
                    temp_file_for_external = Path(f"/tmp/external_ai_{storage_obj.id}_{storage_obj.original_filename}")
                    temp_file_for_external.parent.mkdir(parents=True, exist_ok=True)
                    temp_file_for_external.write_bytes(response.content)
                    file_path = temp_file_for_external
                    print(f"✅ [EXTERNAL MODE] Downloaded {len(response.content)} bytes to {file_path}")
                    print(f"✅ [EXTERNAL MODE] file_path is now set to: {file_path}")
            except Exception as e:
                print(f"❌ [EXTERNAL MODE] Failed to download external file for AI analysis: {e}")
                import traceback
                traceback.print_exc()
                return
        else:
            # Normal mode: use local file
            file_path = generic_storage.absolute_path_for_key(storage_obj.object_key, tenant_id)
            print(f"📁 [NORMAL MODE] Using local file: {file_path}")

        configured_queue_path = Path(settings.AI_ANALYSIS_QUEUE_PATH)
        fallback_queue_path = Path("/tmp/ai_analysis_queue.txt")

        def _prepare_queue_path(candidate: Path) -> Optional[Path]:
            try:
                candidate.parent.mkdir(parents=True, exist_ok=True)
                # Touch the file to validate write permissions.
                with open(candidate, "a"):
                    pass
                return candidate
            except PermissionError as exc:
                print(f"⚠️  Cannot write AI analysis queue file at {candidate}: {exc}")
                return None
            except Exception as exc:
                print(f"⚠️  Failed to initialize AI analysis queue path {candidate}: {exc}")
                return None

        # === CELERY TASK QUEUE (v3 - Production Ready) ===
        # Replaced file-based queue with Redis + Celery
        # Mark the storage object as ai_safety_status="pending" the moment we
        # enqueue a task. This closes the upload→AI race: the /storage/media
        # quarantine gate sees "pending" for public objects and returns 451
        # until the Celery worker writes "completed". Without this step the
        # asset would be downloadable for the (potentially minutes-long)
        # window between upload-200 and AI verdict.
        if ai_mode != "none":
            try:
                from database import SessionLocal as _SL
                from models import StorageObject as _SO
                _pending_db = _SL()
                try:
                    _pending_obj = _pending_db.query(_SO).filter(_SO.id == storage_obj.id).first()
                    if _pending_obj is not None and _pending_obj.ai_safety_status != "completed":
                        _pending_obj.ai_safety_status = "pending"
                        _pending_db.commit()
                finally:
                    _pending_db.close()
            except Exception as _pending_exc:
                print(f"⚠️ Could not mark ai_safety_status=pending: {_pending_exc}")

        def _enqueue_ai_task(object_id: int, task_type: str, content_path: str, filename: str, ai_mode: str = "full") -> None:
            """
            Enqueue AI analysis task to Celery

            Args:
                object_id: Storage object ID
                task_type: Type of content (image, video, text, audio)
                content_path: Path to content file or directory
                filename: Original filename
                ai_mode: AI analysis mode (none, safety, vision, full)
            """
            try:
                from tasks.ai_analysis import (
                    process_safety_check_only,
                    process_vision_analysis_only,
                    process_image_analysis,
                    process_video_analysis,
                    process_text_analysis,
                )

                if task_type == "image":
                    # Route to appropriate task based on ai_mode
                    if ai_mode == "safety":
                        process_safety_check_only.delay(object_id, content_path, filename)
                        print(f"✅ Celery: Safety check queued for object {object_id}")
                    elif ai_mode == "vision":
                        process_vision_analysis_only.delay(object_id, content_path, filename)
                        print(f"✅ Celery: Vision analysis queued for object {object_id}")
                    elif ai_mode == "full":
                        process_image_analysis.delay(object_id, content_path, filename)
                        print(f"✅ Celery: Full analysis queued for object {object_id}")
                    else:
                        print(f"⚠️  AI mode '{ai_mode}' = no analysis")

                elif task_type == "video":
                    process_video_analysis.delay(object_id, content_path, filename)
                    print(f"✅ Celery: Video analysis task queued for object {object_id}")

                elif task_type == "text" or task_type == "audio":
                    process_text_analysis.delay(object_id, content_path, filename)
                    print(f"✅ Celery: Text analysis task queued for object {object_id}")

                else:
                    print(f"⚠️  Unknown task type: {task_type}")

            except Exception as exc:
                print(f"❌ Failed to enqueue Celery task: {exc}")
                # Don't fail the upload - AI analysis can be retried manually
        
        # Check if this is a pre-transcoded video zip file (HLS result)
        is_hls_result = (storage_obj.metadata_json and storage_obj.metadata_json.get('is_hls_result', False))
        if (storage_obj.mime_type and 
            (storage_obj.mime_type == "application/zip" or storage_obj.mime_type == "application/x-zip-compressed") and 
            storage_obj.original_filename and storage_obj.original_filename.lower().endswith('.zip') and
            is_hls_result):
            
            # Handle pre-transcoded video zip files
            print(f"--- Processing pre-transcoded video zip: {file_path}")
            hls_extract_dir = file_path.parent / file_path.stem
            
            # Validate and extract HLS content
            validation_result = validate_and_extract_hls_zip(file_path, hls_extract_dir)
            
            if validation_result["valid"]:
                print(f"--- SUCCESS: Pre-transcoded video validated - {validation_result['file_count']} files, {validation_result['total_size']} bytes")
                
                # Update target storage object with extracted metadata and HLS URL
                try:
                    from database import get_db
                    from models import StorageObject
                    db = next(get_db())
                    
                    # Check if this is for an original video (new architecture)
                    original_video_id = storage_obj.metadata_json.get('original_video_id') if storage_obj.metadata_json else None
                    
                    if original_video_id:
                        # New architecture: Update original video with HLS data
                        print(f"📦 NEW ARCHITECTURE: Updating original video {original_video_id} with HLS data")
                        target_obj = db.query(StorageObject).filter(StorageObject.id == original_video_id).first()

                        hls_url = f"https://api.arkturian.com/uploads/storage/media/{tenant_id}/{hls_extract_dir.name}/master.m3u8"
                        
                        if target_obj:
                            target_obj.hls_url = hls_url
                            if validation_result.get("width"):
                                target_obj.width = validation_result["width"]
                            if validation_result.get("height"):
                                target_obj.height = validation_result["height"]
                            if validation_result.get("duration_seconds"):
                                target_obj.duration_seconds = validation_result["duration_seconds"]
                            if validation_result.get("bit_rate"):
                                target_obj.bit_rate = validation_result["bit_rate"]
                            db.commit()
                            print(f"📦 SUCCESS: Original video {original_video_id} updated with HLS URL: {hls_url}")
                        else:
                            print(f"📦 ERROR: Original video {original_video_id} not found")
                    else:
                        # Old architecture: Update current storage object
                        storage_obj_db = db.query(StorageObject).filter(StorageObject.id == storage_obj.id).first()
                        if storage_obj_db:
                            if validation_result.get("width"):
                                storage_obj_db.width = validation_result["width"]
                            if validation_result.get("height"):
                                storage_obj_db.height = validation_result["height"]
                            if validation_result.get("duration_seconds"):
                                storage_obj_db.duration_seconds = validation_result["duration_seconds"]
                            if validation_result.get("bit_rate"):
                                storage_obj_db.bit_rate = validation_result["bit_rate"]
                            db.commit()
                            print(f"--- SUCCESS: Metadata updated - {validation_result.get('width', 'N/A')}x{validation_result.get('height', 'N/A')}, {validation_result.get('duration_seconds', 'N/A')}s, {validation_result.get('bit_rate', 'N/A')} bps")
                
                except Exception as e:
                    print(f"!!! WARNING: Failed to update metadata: {e}")
                    # Don't raise exception - allow processing to continue
                
                # Optionally queue AI analysis for HLS files
                if not skip_ai_safety:
                    ts_files = validation_result["ts_files"]
                    if ts_files:
                        # Create a temporary video from first .ts segment for thumbnail extraction
                        first_ts = hls_extract_dir / ts_files[0]
                        ai_thumb_dir = _AI_SCRATCH_ROOT / f"ai_thumbs_{storage_obj.id}"
                        result_code = extract_thumbnails_for_ai(first_ts, ai_thumb_dir)
                        
                        if result_code != 0:
                            print(f"!!! WARNING: Thumbnail extraction from pre-transcoded video failed, skipping AI analysis")
                        else:
                            print(f"--- SUCCESS: Thumbnails extracted from pre-transcoded video")
                            _enqueue_ai_task(storage_obj.id, "video", str(ai_thumb_dir), storage_obj.original_filename, ai_mode)
                
                # Delete the original ZIP file since we have extracted the content
                try:
                    file_path.unlink()
                    print(f"--- SUCCESS: Original ZIP file deleted after extraction")
                except Exception as e:
                    print(f"!!! WARNING: Could not delete original ZIP file: {e}")
                
                # No need to enqueue for transcoding - it's already transcoded!
                print(f"--- SUCCESS: Pre-transcoded video ready for streaming")
            else:
                print(f"!!! CRITICAL: Invalid pre-transcoded video zip: {validation_result['error']}")
                
        elif storage_obj.mime_type and storage_obj.mime_type.startswith("video/"):
            # Check if this is actually an audio-only WebM file
            if storage_obj.mime_type == "video/webm" and _is_audio_only_webm_helper(file_path):
                # Process as audio instead of video
                _enqueue_ai_task(storage_obj.id, "audio", str(file_path), storage_obj.original_filename, ai_mode)
                print(f"--- SUCCESS: Audio-only WebM queued for AI transcription: {file_path}")
            else:
                # Regular video processing: optionally enqueue AI analysis, then handle transcoding
                if ai_mode != "none":
                    ai_thumb_dir = _AI_SCRATCH_ROOT / f"ai_thumbs_{storage_obj.id}"
                    result_code = extract_thumbnails_for_ai(file_path, ai_thumb_dir)
                    if result_code != 0:
                        print(f"!!! WARNING: ffmpeg thumbnail extraction failed with code {result_code} for {file_path}")
                    else:
                        print(f"--- SUCCESS: Thumbnail extraction complete for {file_path}")
                        _enqueue_ai_task(storage_obj.id, "video", str(ai_thumb_dir), storage_obj.original_filename, ai_mode)

                # Trigger video transcoding using TranscodingHelper
                file_size = file_path.stat().st_size
                size_mb = file_size / (1024 * 1024)

                logger.error(f"🎬 Video file ({size_mb:.1f}MB), checking for transcoding for object {storage_obj.id}")
                try:
                    from storage.transcoding_helper import TranscodingHelper

                    is_enabled = TranscodingHelper.is_enabled()
                    should_transcode = TranscodingHelper.should_transcode(storage_obj.mime_type)
                    logger.error(f"   TranscodingHelper.is_enabled() = {is_enabled}")
                    logger.error(f"   TranscodingHelper.should_transcode('{storage_obj.mime_type}') = {should_transcode}")

                    # Check if transcoding is enabled and should be done
                    if is_enabled and should_transcode:
                        logger.error(f"✅ Transcoding enabled, queueing Celery task for storage object {storage_obj.id}")

                        # Mark in database that transcoding is queued
                        from datetime import datetime, timezone
                        from database import SessionLocal
                        from models import StorageObject

                        db_session = SessionLocal()
                        try:
                            storage_obj_refresh = db_session.get(StorageObject, storage_obj.id)
                            if storage_obj_refresh:
                                storage_obj_refresh.transcoding_status = "queued"
                                if not storage_obj_refresh.metadata_json:
                                    storage_obj_refresh.metadata_json = {}
                                storage_obj_refresh.metadata_json['transcoding_queued_at'] = datetime.now(timezone.utc).isoformat()
                                db_session.commit()
                                print(f"✅ Database updated - transcoding status: queued")
                        finally:
                            db_session.close()

                        # Queue Celery task for video transcoding
                        from tasks.transcoding import process_video_transcoding
                        process_video_transcoding.delay(storage_obj.id)

                        print(f"✅ Transcoding task queued for storage object {storage_obj.id}")
                    else:
                        print(f"ℹ️  Transcoding disabled or not needed for this file type")

                except Exception as transcoding_error:
                    print(f"⚠️  Transcoding failed to start: {transcoding_error}")
                    import traceback
                    traceback.print_exc()
                    # Don't fail the upload if transcoding fails to start
                        
        elif storage_obj.mime_type and storage_obj.mime_type.startswith("image/"):
            # Image processing - resize and optimize for AI safety analysis
            if ai_mode != "none":
                ai_image_dir = _AI_SCRATCH_ROOT / f"ai_images_{storage_obj.id}"
                ai_image_dir.mkdir(parents=True, exist_ok=True)
                
                # Resize image to max 1280x1280 for AI analysis (saves bandwidth/costs)
                try:
                    with Image.open(file_path) as img:
                        # Convert to RGB if needed (handles RGBA, P mode, etc.)
                        if img.mode in ("RGBA", "LA", "P"):
                            img = img.convert("RGB")
                        
                        # Resize maintaining aspect ratio, max 1280x1280
                        img.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
                        
                        # Save as optimized JPG for AI analysis
                        ai_image_path = ai_image_dir / "image.jpg"
                        img.save(ai_image_path, "JPEG", quality=85, optimize=True)
                        
                    print(f"--- SUCCESS: Image resized for AI analysis: {file_path} -> {ai_image_path}")
                except Exception as e:
                    print(f"!!! WARNING: Image resize failed, using original: {e}")
                    # Fallback to original file if resize fails
                    shutil.copy2(file_path, ai_image_dir / "image.jpg")

                _enqueue_ai_task(storage_obj.id, "image", str(ai_image_dir), storage_obj.original_filename, ai_mode)
                print(f"--- SUCCESS: Image queued for AI analysis: {file_path}")
            
        elif storage_obj.mime_type and storage_obj.mime_type.startswith("audio/"):
            # Audio processing - queue for AI transcription and analysis
            if ai_mode != "none":
                _enqueue_ai_task(storage_obj.id, "audio", str(file_path), storage_obj.original_filename, ai_mode)
                print(f"--- SUCCESS: Audio queued for AI analysis: {file_path}")
            
        elif (storage_obj.mime_type and (storage_obj.mime_type.startswith("text/") or
                                          storage_obj.mime_type in ("application/csv", "application/json"))) or \
             (storage_obj.original_filename and (storage_obj.original_filename.lower().endswith('.json') or
                                                  storage_obj.original_filename.lower().endswith('.txt') or
                                                  storage_obj.original_filename.lower().endswith('.csv'))):
            # Text processing - check for TTS request first, then queue for general analysis
            if ai_mode != "none":
                # Only try TTS processing if TTS modules are available
                if TTS_AVAILABLE:
                    try:
                        # Read the content of the text file
                        async with aiofiles.open(file_path, "r") as f:
                            content = await f.read()

                        # Try to parse as JSON and validate against our TTS model
                        json_content = json.loads(content)
                        tts_request = SpeechRequest(**json_content)

                        # If validation is successful, it's a TTS request. Process it.
                        print(f"--- TTS Hook DETECTED: Storage Object {storage_obj.id} is a valid TTS request.")
                        await _process_tts_request_from_storage(storage_obj, tts_request)

                        # Since we processed it as TTS, we can skip the generic text analysis for now.
                        return

                    except (json.JSONDecodeError, ValidationError):
                        # This is not a valid TTS request, so proceed with standard text analysis.
                        print(f"--- INFO: Storage Object {storage_obj.id} is not a TTS request, queuing for standard text analysis.")
                        pass # Fall through to the generic text analysis queue below
                else:
                    # TTS not available, skip TTS processing
                    try:
                        # Read the content of the text file
                        async with aiofiles.open(file_path, "r") as f:
                            content = await f.read()

                        # Try to parse as JSON to see if it looks like TTS (but we can't validate)
                        try:
                            json_content = json.loads(content)
                            # Just log that we skipped TTS processing
                            print(f"--- INFO: Storage Object {storage_obj.id} might be TTS request, but TTS modules unavailable. Queuing for standard text analysis.")
                        except json.JSONDecodeError:
                            pass # Not JSON, continue with text analysis
                    except Exception:
                        pass # Fall through to generic text analysis

                # Generic text analysis (fallback or non-TTS text files)
                _enqueue_ai_task(storage_obj.id, "text", str(file_path), storage_obj.original_filename, ai_mode)
                print(f"--- SUCCESS: Text queued for AI analysis: {file_path}")
            
    except Exception as e:
        print(f"!!! CRITICAL: Failed to enqueue AI analysis for {storage_obj.object_key}. Error: {e}")
        # Don't raise exception here - allow file upload to succeed even if AI queueing fails
    finally:
        # Cleanup temporary file for external storage mode
        if temp_file_for_external and temp_file_for_external.exists():
            try:
                temp_file_for_external.unlink()
                print(f"🧹 Cleaned up temporary file: {temp_file_for_external}")
            except Exception as cleanup_error:
                print(f"⚠️  Failed to cleanup temporary file: {cleanup_error}")
