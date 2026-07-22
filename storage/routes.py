from fastapi import BackgroundTasks
from fastapi import APIRouter, UploadFile, File, Form, Depends, HTTPException, Query, Request, Body, Header
from fastapi.responses import FileResponse
from fastapi.responses import Response
from sqlalchemy.orm import Session
from sqlalchemy import func, or_
from typing import Optional, List, Any, Dict, Tuple
import json
import os
import shutil
import hashlib
import base64
import subprocess
import threading
from pathlib import Path
from io import BytesIO
from datetime import datetime

from PIL import Image
from storage.pillow_plugins import ensure_heif_support

ensure_heif_support()

# Bound concurrent ffmpeg video-frame extractions. get_media_variant is a sync
# endpoint that FastAPI runs in a ~40-thread pool, so a burst of first-load
# thumbnail requests (e.g. a video grid) could spawn dozens of parallel ffmpeg
# processes (~0.5 GB each) and OOM-killed arkserver on 2026-06-04. Cached frames
# bypass ffmpeg entirely; only cache-misses contend for this semaphore.
_FFMPEG_FRAME_SEM = threading.BoundedSemaphore(int(os.getenv("MEDIA_FFMPEG_CONCURRENCY", "3")))
_FFMPEG_FRAME_WAIT = float(os.getenv("MEDIA_FFMPEG_WAIT", "45"))

# --- Audio transcode support (ffmpeg-backed, mirrors the video-frame path) ---
# The media endpoint can re-encode any audio object — or the audio track of a
# video — to a requested container via ?format=<mp3|m4a|aac|ogg|opus|wav|flac>,
# analogous to image ?format=jpg and video ?format=jpg frame extraction. Bounded
# by the same ffmpeg concurrency semaphore as frame extraction and cache-keyed so
# repeat requests never re-run ffmpeg. Without an audio format param, audio is
# served raw (backwards compatible). Codec is the only universal knob; optional
# ?bitrate / ?sample_rate / ?channels tune the output.
#   format -> (ffmpeg codec, file extension, mime type, is_lossy)
_AUDIO_FORMAT_SPEC = {
    "mp3":  ("libmp3lame", "mp3",  "audio/mpeg", True),
    "m4a":  ("aac",        "m4a",  "audio/mp4",  True),
    "aac":  ("aac",        "m4a",  "audio/mp4",  True),
    "ogg":  ("libvorbis",  "ogg",  "audio/ogg",  True),
    "oga":  ("libvorbis",  "ogg",  "audio/ogg",  True),
    "opus": ("libopus",    "opus", "audio/opus", True),
    "wav":  ("pcm_s16le",  "wav",  "audio/wav",  False),
    "flac": ("flac",       "flac", "audio/flac", False),
}


def _transcode_audio(src_path, spec, *, bitrate=None, sample_rate=None, channels=None, cache_path):
    """Re-encode audio (or a video's audio track) to the target codec via ffmpeg.

    Bounded by ``_FFMPEG_FRAME_SEM``. Writes the derivative to ``cache_path``.

    Args:
        src_path: Source media path (audio or video).
        spec: One value from ``_AUDIO_FORMAT_SPEC`` — (codec, ext, mime, is_lossy).
        bitrate: Optional target bitrate for lossy codecs, e.g. ``"192k"``.
        sample_rate: Optional output sample rate in Hz.
        channels: Optional output channel count (1=mono, 2=stereo).
        cache_path: Destination ``Path`` for the encoded file.

    Returns:
        True on success.

    Raises:
        RuntimeError: Concurrency cap hit (caller may fall back to raw), or the
            ffmpeg encode failed (caller should surface it — not silent).
    """
    codec, _ext, _mime_out, is_lossy = spec
    acquired = _FFMPEG_FRAME_SEM.acquire(timeout=_FFMPEG_FRAME_WAIT)
    if not acquired:
        raise RuntimeError("ffmpeg audio-transcode concurrency limit reached")
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        # -vn strips any video stream (so video sources demux to a pure audio file);
        # -threads 1 keeps a burst from saturating the box (same guard as frames).
        cmd = ["ffmpeg", "-nostdin", "-threads", "1", "-y", "-i", str(src_path), "-vn", "-c:a", codec]
        if is_lossy and bitrate:
            cmd += ["-b:a", bitrate]
        if sample_rate:
            cmd += ["-ar", str(sample_rate)]
        if channels:
            cmd += ["-ac", str(channels)]
        cmd.append(str(cache_path))
        result = subprocess.run(cmd, capture_output=True, timeout=300)
        if result.returncode != 0 or not cache_path.exists():
            cache_path.unlink(missing_ok=True)
            err = (result.stderr or b"")[-400:].decode("utf-8", "replace")
            raise RuntimeError(f"ffmpeg audio transcode failed (rc={result.returncode}): {err}")
        return True
    finally:
        _FFMPEG_FRAME_SEM.release()


def _audio_cache_path_for(obj, audio_fmt, *, bitrate=None, sample_rate=None, channels=None):
    """Resolve the cache path + output mime for an audio transcode derivative.

    Single source of truth for the audio cache key, shared by the GET media
    handler and the HEAD-intercept middleware in main.py — so a HEAD reports
    the exact file a GET would serve (no "HEAD lies" drift, issue #61 for audio).

    Args:
        obj: The StorageObject (uses ``id`` and ``tenant_id``).
        audio_fmt: Requested target format (e.g. ``"mp3"``).
        bitrate: Normalized bitrate string (lowercased, e.g. ``"192k"``) or None.
        sample_rate: Output sample rate in Hz or None.
        channels: Output channel count or None.

    Returns:
        ``(cache_path, mime_out, ext)`` or ``None`` if ``audio_fmt`` is not a
        known audio target.
    """
    spec = _AUDIO_FORMAT_SPEC.get((audio_fmt or "").lower())
    if not spec:
        return None
    _codec, ext, mime_out, _is_lossy = spec
    br_key = bitrate or "src"
    cache_name = (
        f"aud_{obj.id}_{audio_fmt.lower()}_{br_key}_"
        f"{sample_rate or 'src'}_{channels or 'src'}.{ext}"
    )
    cache_path = generic_storage.webview_dir / (obj.tenant_id or "arkturian") / cache_name
    return cache_path, mime_out, ext

try:
    import fitz  # type: ignore[import]

    PDF_RENDER_AVAILABLE = True
except Exception:  # pragma: no cover - optional dependency
    fitz = None  # type: ignore[assignment]
    PDF_RENDER_AVAILABLE = False

try:
    from cairosvg import svg2png

    CAIROSVG_AVAILABLE = True
except Exception:  # pragma: no cover - optional dependency
    svg2png = None  # type: ignore[assignment]
    CAIROSVG_AVAILABLE = False

DEFAULT_PDF_RENDER_SCALE = 2.0

try:
    import cv2
    import numpy as np
    OPENCV_AVAILABLE = True
except Exception:
    cv2 = None  # type: ignore[assignment]
    np = None  # type: ignore[assignment]
    OPENCV_AVAILABLE = False


def _parse_aspect_ratio_value(raw_value: Optional[str]) -> Optional[float]:
    """
    Convert an aspect ratio string (e.g. "1:1" or "1.0") into a float width/height ratio.
    Returns None when the parameter is empty.
    """
    if not raw_value:
        return None
    value = raw_value.strip()
    if not value:
        return None
    try:
        if ":" in value:
            left, right = value.split(":", 1)
            numerator = float(left)
            denominator = float(right)
        else:
            numerator = float(value)
            denominator = 1.0
    except (TypeError, ValueError) as exc:  # pragma: no cover - validation guard
        raise ValueError("expected aspect_ratio like '1:1' or '1.0'") from exc
    if numerator <= 0 or denominator <= 0:
        raise ValueError("aspect_ratio values must be positive")
    return numerator / denominator


def _apply_letterbox_aspect_ratio(image: Image.Image, desired_ratio: Optional[float], target_format: str) -> Image.Image:
    """
    Pad an image with transparent (PNG/WebP) or white (JPEG) background
    to achieve the requested aspect ratio without stretching.
    """
    if not desired_ratio:
        return image

    width, height = image.size
    if width <= 0 or height <= 0:
        return image

    current_ratio = width / height
    if abs(current_ratio - desired_ratio) < 1e-3:
        return image

    if current_ratio > desired_ratio:
        final_width = width
        final_height = max(int(round(width / desired_ratio)), 1)
    else:
        final_height = height
        final_width = max(int(round(height * desired_ratio)), 1)

    supports_alpha = target_format not in {"jpg", "jpeg"}
    canvas_mode = "RGBA" if supports_alpha else "RGB"
    background_color = (255, 255, 255, 0) if supports_alpha else (255, 255, 255)
    canvas = Image.new(canvas_mode, (final_width, final_height), background_color)

    paste_img = image
    if paste_img.mode != canvas_mode:
        paste_img = paste_img.convert(canvas_mode)

    offset_x = (final_width - width) // 2
    offset_y = (final_height - height) // 2
    if supports_alpha and paste_img.mode in {"RGBA", "LA"}:
        canvas.paste(paste_img, (offset_x, offset_y), paste_img)
    else:
        canvas.paste(paste_img, (offset_x, offset_y))
    return canvas


def _evict_share_proxy_cache(cache_dir: Path) -> None:
    """Keep /tmp/share_proxy_cache (a RAM-backed tmpfs) bounded: delete oldest
    entries once total size exceeds the cap. Without this the cache grew to
    multiple GB of RAM during a bulk ingest (2026-07-10). Best-effort / never
    raises. Cap via SHARE_PROXY_CACHE_MAX_MB (default 1024)."""
    try:
        cap = int(os.getenv("SHARE_PROXY_CACHE_MAX_MB", "1024")) * 1024 * 1024
        files = [p for p in cache_dir.glob("obj_*") if p.is_file()]
        files.sort(key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in files)
        for p in files:
            if total <= cap:
                break
            try:
                sz = p.stat().st_size
                p.unlink(missing_ok=True)
                total -= sz
            except Exception:
                pass
    except Exception:
        pass


def _resolve_storage_object_path(obj: Any, refresh: bool = False) -> Path:
    """
    Resolve file path for a storage object, handling both local and external URIs.

    Args:
        obj: StorageObject instance
        refresh: If True, clears cache for external URIs

    Returns:
        Path to the file (local or cached external)

    Raises:
        HTTPException: If file cannot be accessed
    """
    # Try local file first
    src_path: Optional[Path] = None
    if obj.object_key:
        src_path = generic_storage.absolute_path_for_key(obj.object_key, obj.tenant_id)

    # Handle external URIs if local file doesn't exist
    if (not src_path or not src_path.exists()) and obj.external_uri:
        cache_dir = Path("/tmp/share_proxy_cache")
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"obj_{obj.id}"

        if refresh:
            cache_file.unlink(missing_ok=True)
            meta_candidate = cache_dir / f"obj_{obj.id}.meta"
            meta_candidate.unlink(missing_ok=True)

        # Download if not cached — STREAM to the cache file with a hard size guard.
        # The old client.get().content pulled the whole body into RAM in one shot,
        # and /tmp/share_proxy_cache is a RAM-backed tmpfs, so a 12 GB ZIP
        # mislabeled as image/zip ballooned RSS and OOM-killed uvicorn (2026-07-10).
        # Reject via Content-Length up front, hard-stop the stream if a server
        # omits it, write straight to disk (no full-body copy in RAM), and keep the
        # tmpfs cache bounded.
        if not cache_file.exists():
            _max_bytes = int(os.getenv("EXTERNAL_MAX_FETCH_MB", "100")) * 1024 * 1024
            _evict_share_proxy_cache(cache_dir)
            tmp_file = cache_dir / f"obj_{obj.id}.part"
            try:
                with httpx.Client(timeout=30.0, follow_redirects=True) as client:
                    with client.stream("GET", obj.external_uri) as response:
                        if response.status_code != 200:
                            raise HTTPException(
                                status_code=502,
                                detail=f"Failed to fetch external URI: HTTP {response.status_code}"
                            )
                        clen = response.headers.get("content-length")
                        if clen and clen.isdigit() and int(clen) > _max_bytes:
                            raise HTTPException(
                                status_code=413,
                                detail=f"External file too large ({int(clen)} bytes > {_max_bytes})"
                            )
                        total = 0
                        with open(tmp_file, "wb") as fh:
                            for chunk in response.iter_bytes():
                                total += len(chunk)
                                if total > _max_bytes:
                                    raise HTTPException(
                                        status_code=413,
                                        detail=f"External file exceeded {_max_bytes} bytes"
                                    )
                                fh.write(chunk)
                        tmp_file.replace(cache_file)
                        src_path = cache_file
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(
                    status_code=502,
                    detail=f"Failed to fetch external URI: {str(e)}"
                )
            finally:
                try:
                    tmp_file.unlink(missing_ok=True)
                except Exception:
                    pass
        else:
            src_path = cache_file

    # Fallback: reference_path (e.g., SMB mount)
    if (not src_path or not src_path.exists()) and getattr(obj, 'reference_path', None):
        ref = Path(obj.reference_path)
        if ref.exists():
            src_path = ref

    if not src_path or not src_path.exists():
        raise HTTPException(status_code=404, detail="File not accessible")

    return src_path


def _generate_contour_polygon_from_image(image_path: Path, simplify_factor: float = 0.002) -> Dict[str, Any]:
    """
    Compute contour polygon for an image using OpenCV.

    Args:
        image_path: Path to the image file
        simplify_factor: Douglas-Peucker simplification factor (0.001-0.05, lower = more detail)

    Returns:
        Dictionary with polygon data including points and normalized coordinates
    """
    if not OPENCV_AVAILABLE:
        raise ImportError("OpenCV (cv2) is required for polygon extraction")

    # Load image
    img = cv2.imread(str(image_path), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise ValueError(f"Failed to load image: {image_path}")

    height, width = img.shape[:2]

    # Handle transparency/alpha channel
    if img.shape[2] == 4:  # Has alpha channel
        # Use alpha channel as mask
        alpha = img[:, :, 3]
        # Threshold alpha to binary mask (non-transparent = foreground)
        _, mask = cv2.threshold(alpha, 1, 255, cv2.THRESH_BINARY)
    else:
        # No alpha - try to detect background by edges
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        # Use Canny edge detection
        edges = cv2.Canny(gray, 50, 150)
        # Dilate edges to close gaps
        kernel = np.ones((5, 5), np.uint8)
        mask = cv2.dilate(edges, kernel, iterations=2)

    # Find contours
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    if not contours:
        # No contours found - return full image rect as polygon
        points = [[0, 0], [width, 0], [width, height], [0, height]]
        normalized_points = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]
        return {
            "type": "polygon",
            "width": width,
            "height": height,
            "points": points,
            "normalized_points": normalized_points,
            "point_count": 4,
        }

    # Get largest contour (assume it's the main object)
    largest_contour = max(contours, key=cv2.contourArea)

    # Simplify polygon using Douglas-Peucker algorithm
    epsilon = simplify_factor * cv2.arcLength(largest_contour, True)
    simplified_contour = cv2.approxPolyDP(largest_contour, epsilon, True)

    # Convert to list of [x, y] points
    points = [[int(pt[0][0]), int(pt[0][1])] for pt in simplified_contour]

    # Normalize to 0-1 range
    normalized_points = [
        [pt[0] / width if width > 0 else 0.0, pt[1] / height if height > 0 else 0.0]
        for pt in points
    ]

    return {
        "type": "polygon",
        "width": width,
        "height": height,
        "points": points,
        "normalized_points": normalized_points,
        "point_count": len(points),
    }


def _generate_trim_metadata_from_image(image_path: Path) -> Dict[str, Any]:
    """Compute trim bounds for a given image file."""
    with Image.open(image_path) as img:
        # Convert palette images to RGBA to access alpha channel
        if img.mode in ('P', 'PA'):
            img = img.convert('RGBA')
        width, height = img.size

        def _compute_bbox(image: Image.Image) -> Optional[Tuple[int, int, int, int]]:
            if width == 0 or height == 0:
                return None
            if "A" in image.getbands():
                alpha_bbox = image.getchannel("A").getbbox()
                if alpha_bbox:
                    return alpha_bbox
            gray = image.convert("L")
            mask = gray.point(lambda p: 255 if p < 250 else 0)
            return mask.getbbox()

        bbox = _compute_bbox(img)
        trim_meta: Dict[str, Any] = {
            "width": width,
            "height": height,
            "trim_width": width,
            "trim_height": height,
            "normalized": [0.0, 0.0, 1.0, 1.0],
            "applied": False,
        }

        if bbox:
            x1, y1, x2, y2 = bbox
            x1 = max(0, min(x1, width))
            y1 = max(0, min(y1, height))
            x2 = max(0, min(x2, width))
            y2 = max(0, min(y2, height))
            if x2 > x1 and y2 > y1 and (x1 != 0 or y1 != 0 or x2 != width or y2 != height):
                trim_meta.update({
                    "applied": True,
                    "x1": x1,
                    "y1": y1,
                    "x2": x2,
                    "y2": y2,
                    "trim_width": x2 - x1,
                    "trim_height": y2 - y1,
                    "normalized": [
                        x1 / width if width else 0.0,
                        y1 / height if height else 0.0,
                        x2 / width if width else 1.0,
                        y2 / height if height else 1.0,
                    ],
                })

        # Ensure default coordinates exist even if not applied
        trim_meta.setdefault("x1", 0)
        trim_meta.setdefault("y1", 0)
        trim_meta.setdefault("x2", width)
        trim_meta.setdefault("y2", height)
        return trim_meta


def _render_pdf_page_to_image(pdf_path: Path, output_path: Path, width: Optional[int], height: Optional[int]) -> None:
    if not PDF_RENDER_AVAILABLE:
        raise RuntimeError("PDF rendering support is not available (PyMuPDF missing)")

    with fitz.open(pdf_path) as doc:
        if doc.page_count == 0:
            raise ValueError("PDF contains no pages")

        page = doc.load_page(0)
        rect = page.rect

        zoom_x = DEFAULT_PDF_RENDER_SCALE
        zoom_y = DEFAULT_PDF_RENDER_SCALE

        if width and height:
            zoom_x = max(width / max(rect.width, 1), 0.5)
            zoom_y = max(height / max(rect.height, 1), 0.5)
        elif width:
            zoom_x = zoom_y = max(width / max(rect.width, 1), 0.5)
        elif height:
            zoom_x = zoom_y = max(height / max(rect.height, 1), 0.5)

        pix = page.get_pixmap(matrix=fitz.Matrix(zoom_x, zoom_y), alpha=False)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        pix.save(str(output_path))


def _ensure_pdf_preview(
    obj: "StorageObject",
    pdf_path: Path,
    *,
    width: Optional[int],
    height: Optional[int],
    refresh: bool,
) -> Path:
    tenant_id = getattr(obj, "tenant_id", None) or "arkturian"
    generic_storage.ensure_tenant_directories(tenant_id)
    preview_dir = generic_storage.webview_dir / tenant_id
    base_name = Path(getattr(obj, "object_key", "") or f"obj_{getattr(obj, 'id', 'unknown')}").stem
    size_tag = f"{width or 'auto'}x{height or 'auto'}"
    preview_path = preview_dir / f"pdfpreview_{base_name}_{size_tag}.png"

    if refresh and preview_path.exists():
        preview_path.unlink(missing_ok=True)

    if not preview_path.exists():
        _render_pdf_page_to_image(pdf_path, preview_path, width, height)

    return preview_path

from database import get_db
from auth import get_current_user, get_current_user_optional, generate_api_key
from models import StorageObject, StorageObjectResponse, StorageListResponse, User, AsyncTask
from ai_analysis import analyze_content
from config import settings
from pydantic import BaseModel
import httpx
from urllib.parse import urlparse, urlunparse
from fastapi import Security
from fastapi.security.api_key import APIKeyHeader as _APIKeyHeader

from storage.domain import save_file_and_record, update_file_and_record
from storage.service import generic_storage, bulk_delete_objects, GenericStorageService
from storage.external_proxy import fetch_external_file, external_cache
from storage.url_builder import build_storage_urls, get_base_url_from_request
from admin import routes as admin_routes
from tenancy.config import tenant_id_for_api_key, get_tenant_id, get_tenant_id_optional


def build_hls_url(tenant_id: str, basename: str) -> str:
    """
    Build HLS URL using VOD_BASE_URL from settings.
    Auto-derives vod domain from api-storage domain (e.g., api-storage.arkserver.arkturian.com -> vod.arkserver.arkturian.com)
    """
    from config import settings
    vod_base = settings.VOD_BASE_URL
    return f"{vod_base}/media/{tenant_id}/{basename}/master.m3u8"


router = APIRouter()
admin_router = APIRouter(prefix="/admin")


class BulkDeleteRequest(BaseModel):
    name: Optional[str] = None
    collection_like: Optional[str] = None
    context_like: Optional[str] = None


class TenantDeleteRequest(BaseModel):
    tenant_id: str
    batch_size: Optional[int] = 100
    dry_run: bool = False


class SimilarityResponse(BaseModel):
    """Response for similarity search"""
    query_object_id: int
    similar_objects: List[StorageObjectResponse]
    distances: List[float]
    total_embeddings: int


class SearchResultChunk(BaseModel):
    """A single embedding/chunk match from search"""
    # The matched content
    content: str  # The actual text that matched
    embedding_type: Optional[str] = None  # e.g., "row", "page", "segment"
    embedding_index: Optional[int] = None  # Row number, page number, etc.

    # Chunk metadata
    metadata: Optional[dict] = None  # row_hash, timestamps, etc.

    # Source file context (omitted by default — see include_source_file on /kg/search;
    # a search result only needs content+metadata+distance, and the full object is
    # ~30k chars each which blows response/token budgets for consumers)
    source_file: Optional[StorageObjectResponse] = None

    # Search relevance
    distance: float
    similarity_score: float  # 0-100, higher is better


class KGSearchResponse(BaseModel):
    """Response for text semantic search - returns matching chunks/embeddings"""
    query: str
    results: List[SearchResultChunk]  # Changed from 'items' to 'results' for clarity
    total_embeddings: int


class MediaDerivativeStatus(BaseModel):
    width: int
    quality: int
    format: str
    exists: bool
    size_bytes: Optional[int] = None
    path: Optional[str] = None
    mtime: Optional[str] = None


class MediaCacheStatus(BaseModel):
    object_id: int
    tenant_id: Optional[str] = None
    original_exists: bool
    original_path: Optional[str] = None
    derivatives: List[MediaDerivativeStatus]
    message: Optional[str] = None


class CacheClearResult(BaseModel):
    object_id: int
    success: bool
    files_deleted: int = 0
    message: Optional[str] = None


class CacheClearResponse(BaseModel):
    total_objects: int
    successful: int
    failed: int
    results: List[CacheClearResult]


def _resolve_quality_for_width(width: int) -> int:
    return 85 if width >= 1000 else 75


def _build_derivative_name(base_name: str, width: int, quality: int, fmt: str, trim: bool = False) -> str:
    suffix = fmt.lower()
    if suffix == "jpeg":
        suffix = "jpg"
    if suffix not in {"jpg", "png", "webp"}:
        suffix = "webp"
    edge = max(width, 1)
    trim_token = "_trim" if trim else ""
    return f"web_{base_name}_{edge}e{trim_token}_q{quality}.{suffix}"


@router.get("/cache-status", response_model=List[MediaCacheStatus])
def get_media_cache_status(
    object_ids: List[int] = Query(..., alias="object_id", description="List of storage object IDs"),
    widths: Optional[List[int]] = Query(None, alias="width", description="Derivative widths to inspect"),
    qualities: Optional[List[int]] = Query(None, alias="quality", description="Optional quality overrides"),
    image_format: str = Query("webp", description="Derivative format to inspect"),
    include_original: bool = Query(True, description="Include original file status"),
    reuse_existing: bool = Form(False),
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
    tenant_id: Optional[str] = Depends(get_tenant_id_optional),
):
    unique_ids = list(dict.fromkeys(int(oid) for oid in object_ids if int(oid) > 0))
    if not unique_ids:
        raise HTTPException(status_code=400, detail="No valid object IDs provided")

    width_values = [int(w) for w in (widths or [130, 1300]) if int(w) > 0]
    if not width_values:
        raise HTTPException(status_code=400, detail="No valid widths provided")

    quality_overrides: List[int] = []
    if qualities:
        try:
            quality_overrides = [int(q) for q in qualities if int(q) > 0]
        except ValueError as exc:  # pragma: no cover - defensive
            raise HTTPException(status_code=400, detail=f"Invalid quality value: {exc}")

    def _quality_for_index(idx: int, width: int) -> int:
        if quality_overrides:
            if len(quality_overrides) == 1:
                return quality_overrides[0]
            if idx < len(quality_overrides):
                return quality_overrides[idx]
        return _resolve_quality_for_width(width)

    response: List[MediaCacheStatus] = []

    for object_id in unique_ids:
        record = db.query(StorageObject).filter(StorageObject.id == object_id).first()

        if not record:
            response.append(
                MediaCacheStatus(
                    object_id=object_id,
                    tenant_id=None,
                    original_exists=False,
                    original_path=None,
                    derivatives=[],
                    message="Object not found",
                )
            )
            continue

        if tenant_id and record.tenant_id and record.tenant_id != tenant_id:
            response.append(
                MediaCacheStatus(
                    object_id=object_id,
                    tenant_id=record.tenant_id,
                    original_exists=False,
                    original_path=None,
                    derivatives=[],
                    message="Object belongs to different tenant",
                )
            )
            continue

        derivatives: List[MediaDerivativeStatus] = []
        base_name = Path(record.object_key).stem if record.object_key else f"ext_{object_id}"
        tenant_slug = record.tenant_id or tenant_id or ""

        for idx, width in enumerate(width_values):
            quality_value = _quality_for_index(idx, width)
            derivative_name = _build_derivative_name(base_name, width, quality_value, image_format)

            candidate_paths = [generic_storage.webview_dir / derivative_name]
            if tenant_slug:
                candidate_paths.append(generic_storage.webview_dir / tenant_slug / derivative_name)

            exists = False
            size_bytes = None
            mtime_iso = None
            resolved_path = None

            for candidate in candidate_paths:
                if candidate.exists():
                    exists = True
                    resolved_path = str(candidate)
                    try:
                        stats = candidate.stat()
                        size_bytes = stats.st_size
                        mtime_iso = datetime.fromtimestamp(stats.st_mtime).isoformat()
                    except Exception:
                        pass
                    break

            derivatives.append(
                MediaDerivativeStatus(
                    width=width,
                    quality=quality_value,
                    format=image_format.lower(),
                    exists=exists,
                    size_bytes=size_bytes,
                    path=resolved_path,
                    mtime=mtime_iso,
                )
            )

        original_exists = False
        original_path = None
        message = None

        if include_original:
            if record.object_key:
                original_path = str(generic_storage.absolute_path_for_key(record.object_key, record.tenant_id or tenant_slug or "arkturian"))
                original_exists = Path(original_path).exists()
            elif record.external_uri:
                original_path = record.external_uri
                original_exists = False
                message = "Object uses external URI"

        response.append(
            MediaCacheStatus(
                object_id=object_id,
                tenant_id=record.tenant_id,
                original_exists=original_exists,
                original_path=original_path,
                derivatives=derivatives,
                message=message,
            )
        )

    return response


@router.post("/media/clear-cache", response_model=CacheClearResponse)
def clear_media_cache(
    object_ids: List[int] = Query(..., alias="object_id", description="List of storage object IDs to clear cache for"),
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
    tenant_id: Optional[str] = Depends(get_tenant_id_optional),
):
    """
    Clear cached derivatives for one or more storage objects.

    This endpoint removes all cached derivative images (thumbnails, web-optimized versions, etc.)
    for the specified storage object IDs. Original files are not affected.

    Args:
        object_ids: List of storage object IDs to clear cache for

    Returns:
        CacheClearResponse with detailed results for each object
    """
    unique_ids = list(dict.fromkeys(int(oid) for oid in object_ids if int(oid) > 0))
    if not unique_ids:
        raise HTTPException(status_code=400, detail="No valid object IDs provided")

    results: List[CacheClearResult] = []
    successful = 0
    failed = 0

    for object_id in unique_ids:
        files_deleted = 0
        message = None
        success = True

        try:
            # Get the storage object
            record = db.query(StorageObject).filter(StorageObject.id == object_id).first()

            if not record:
                results.append(
                    CacheClearResult(
                        object_id=object_id,
                        success=False,
                        files_deleted=0,
                        message="Object not found",
                    )
                )
                failed += 1
                continue

            # Check tenant access
            if tenant_id and record.tenant_id and record.tenant_id != tenant_id:
                results.append(
                    CacheClearResult(
                        object_id=object_id,
                        success=False,
                        files_deleted=0,
                        message="Object belongs to different tenant",
                    )
                )
                failed += 1
                continue

            # Build derivative name pattern
            base_name = Path(record.object_key).stem if record.object_key else f"ext_{object_id}"
            tenant_slug = record.tenant_id or tenant_id or ""

            # Search patterns for derivative files
            # Format: web_{basename}_{width}e_q{quality}.{format}
            patterns = [
                f"web_{base_name}_*",
            ]

            # Check in webview directory and tenant subdirectory
            search_paths = [generic_storage.webview_dir]
            if tenant_slug:
                search_paths.append(generic_storage.webview_dir / tenant_slug)

            for search_path in search_paths:
                if not search_path.exists():
                    continue

                for pattern in patterns:
                    for file_path in search_path.glob(pattern):
                        try:
                            if file_path.is_file():
                                file_path.unlink()
                                files_deleted += 1
                        except Exception as e:
                            # Log but continue with other files
                            pass

            if files_deleted > 0:
                message = f"Cleared {files_deleted} cached derivative(s)"
            else:
                message = "No cached derivatives found"

            results.append(
                CacheClearResult(
                    object_id=object_id,
                    success=True,
                    files_deleted=files_deleted,
                    message=message,
                )
            )
            successful += 1

        except Exception as e:
            results.append(
                CacheClearResult(
                    object_id=object_id,
                    success=False,
                    files_deleted=0,
                    message=f"Error: {str(e)}",
                )
            )
            failed += 1

    return CacheClearResponse(
        total_objects=len(unique_ids),
        successful=successful,
        failed=failed,
        results=results,
    )


@router.get("/similar/{object_id}", response_model=SimilarityResponse)
async def find_similar_objects(
    object_id: int,
    limit: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    api_key_header: str = Security(_APIKeyHeader(name="X-API-KEY", auto_error=True)),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Find semantically similar storage objects.

    Uses vector embeddings to find objects with similar semantic content.
    Requires that the object has been analyzed with AI (analyze=true during upload).

    Args:
        object_id: ID of the storage object to find similar items for
        limit: Maximum number of similar objects to return (1-50)

    Returns:
        SimilarityResponse with similar objects and their similarity scores
    """
    # Verify object exists and user has access
    obj = db.query(StorageObject).filter(
        StorageObject.id == object_id,
        StorageObject.tenant_id == tenant_id
    ).first()
    if not obj:
        raise HTTPException(status_code=404, detail="Storage object not found")

    # Check access (owner or public)
    if obj.owner_user_id != current_user.id and not obj.is_public:
        if current_user.trust_level != "admin":
            raise HTTPException(status_code=403, detail="Access denied")

    try:
        from knowledge_graph.pipeline import kg_pipeline

        # Build tenant-aware where filter for vector search
        where = None
        try:
            if current_user.trust_level == "admin":
                where = None
            else:
                # Prefer tenant from API key mapping; fallback to email domain
                tenant_from_key = tenant_id_for_api_key(api_key_header)
                tenant_email_domain = None
                if getattr(current_user, "email", None) and "@" in current_user.email:
                    tenant_email_domain = current_user.email.split("@", 1)[1].strip().lower()

                tenant_scope = tenant_from_key or tenant_email_domain
                if tenant_scope:
                    where = {"tenant_id": tenant_scope}
                else:
                    where = {"owner_user_id": current_user.id}
        except Exception:
            where = None

        # Find similar objects using KG with where filter (tenant-specific collection)
        similar_results = await kg_pipeline.find_similar_objects(object_id, limit, where=where, tenant_id=tenant_id)

        if not similar_results:
            return SimilarityResponse(
                query_object_id=object_id,
                similar_objects=[],
                distances=[],
                total_embeddings=kg_pipeline.get_stats(tenant_id=tenant_id)["total_embeddings"]
            )

        # Fetch full storage objects for the similar results (with owner email)
        similar_ids = [r["object_id"] for r in similar_results]
        base_q = db.query(StorageObject, User.email.label('owner_email')).outerjoin(User, StorageObject.owner_user_id == User.id).filter(
            StorageObject.id.in_(similar_ids),
            StorageObject.tenant_id == tenant_id
        )

        # Enforce access control: owner, public, or same-tenant (API key tenant or email domain); admin bypass
        if current_user.trust_level != "admin":
            tenant_domain = None
            try:
                if getattr(current_user, "email", None) and "@" in current_user.email:
                    tenant_domain = current_user.email.split("@", 1)[1].strip().lower()
            except Exception:
                tenant_domain = None
            tenant_from_key = tenant_id_for_api_key(api_key_header)
            effective_tenant = tenant_from_key or tenant_domain

            filters = [
                (StorageObject.owner_user_id == current_user.id),
                (StorageObject.is_public == True),  # noqa: E712
            ]
            if effective_tenant:
                filters.append(func.lower(User.email).like(f"%@{effective_tenant}"))

            base_q = base_q.filter(or_(*filters))

        rows = base_q.all()

        # Create a map for quick lookup (rows are tuples of (StorageObject, owner_email))
        objects_map = {row[0].id: row[0] for row in rows}

        # Order objects according to similarity results
        ordered_objects = []
        distances = []
        for result in similar_results:
            obj_id = result["object_id"]
            if obj_id in objects_map:
                ordered_objects.append(objects_map[obj_id])
                distances.append(result["distance"])

        return SimilarityResponse(
            query_object_id=object_id,
            similar_objects=[StorageObjectResponse.from_orm(obj) for obj in ordered_objects],
            distances=distances,
            total_embeddings=kg_pipeline.get_stats(tenant_id=tenant_id)["total_embeddings"]
        )

    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        print(f"❌ Similarity search error: {e}")
        raise HTTPException(status_code=500, detail=f"Similarity search failed: {str(e)}")


@router.get("/kg/stats")
async def get_kg_stats(
    current_user: User = Depends(get_current_user),
    api_key_header: str = Security(_APIKeyHeader(name="X-API-KEY", auto_error=True)),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Get knowledge graph statistics for the current tenant.

    Uses tenant-specific collection based on API key mapping.
    Returns statistics about the knowledge graph including total embeddings
    and collection name for the authenticated tenant.

    Returns:
        Statistics about the knowledge graph:
        - status: "ok" if successful
        - total_embeddings: Number of embeddings in tenant's collection
        - collection: Name of the tenant's ChromaDB collection
    """
    try:
        from knowledge_graph.pipeline import kg_pipeline
        stats = kg_pipeline.get_stats(tenant_id=tenant_id)

        return {
            "status": "ok",
            "total_embeddings": stats["total_embeddings"],
            "collection": stats["collection"]
        }
    except Exception as e:
        print(f"❌ KG stats error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get KG stats: {str(e)}")


@router.get("/kg/health")
async def kg_health_check(
    current_user: User = Depends(get_current_user),
    api_key_header: str = Security(_APIKeyHeader(name="X-API-KEY", auto_error=True)),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Health check for knowledge graph system for the current tenant.

    Uses tenant-specific collection based on API key mapping.
    Returns health status including OpenAI configuration, ChromaDB connectivity,
    and statistics for the authenticated tenant's collection.
    """
    try:
        from knowledge_graph.pipeline import kg_pipeline
        import os

        stats = kg_pipeline.get_stats(tenant_id=tenant_id)
        openai_configured = bool(os.getenv("OPENAI_API_KEY"))
        from knowledge_graph.embedding_service import embedding_service as _emb

        return {
            "status": "healthy",
            "total_embeddings": stats["total_embeddings"],
            "vector_store": stats["collection"],
            "openai_configured": openai_configured,
            "embedding_model": _emb.model,
            "vector_dimensions": _emb.dimensions
        }
    except Exception as e:
        print(f"❌ KG health check error: {e}")
        return {
            "status": "unhealthy",
            "error": str(e)
        }


@router.get("/kg/search", response_model=KGSearchResponse)
async def kg_text_search(
    query: str = Query(..., description="Text to search by"),
    limit: int = Query(10, ge=1, le=50),
    collection_like: Optional[str] = Query(None, description="Optional filter: collection id contains"),
    mine: bool = False,
    include_source_file: bool = Query(False, description="Include the full source StorageObject per result. Default off = slim response (content+metadata+distance only); the full object is ~30k chars each and blows token budgets. Fetch /objects/{id} if you need it."),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    api_key_header: str = Security(_APIKeyHeader(name="X-API-KEY", auto_error=True)),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Semantic search across assets by text query using the Knowledge Graph vector store.

    Scope default is tenant-wide (`mine=false`) — the vector collection is already
    tenant-isolated (tenant_{id}_knowledge), so owner-scoping (`mine=true`) is opt-in
    for "only my own objects". A shared catalog KG must be searchable across the
    tenant regardless of which key ingested the objects.

    Scoping:
    - Admin: global unless mine=true, then owner-only
    - Non-admin: tenant-scoped by API key (preferred), else owner email domain; if mine=true, owner-only
    """
    try:
        from knowledge_graph.embedding_service import embedding_service
        from knowledge_graph.vector_store import get_vector_store
        from knowledge_graph.pipeline import kg_pipeline

        # Get tenant-specific vector store
        tenant_vector_store = get_vector_store(tenant_id=tenant_id)

        # Build metadata filter for vector store
        where: Optional[dict] = None
        if current_user.trust_level == "admin":
            if mine:
                where = {"owner_user_id": current_user.id}
            else:
                where = None
        else:
            # Prefer tenant from API key mapping; fallback to email domain
            tenant_from_key = tenant_id_for_api_key(api_key_header)
            tenant_email_domain = None
            try:
                if getattr(current_user, "email", None) and "@" in current_user.email:
                    tenant_email_domain = current_user.email.split("@", 1)[1].strip().lower()
            except Exception:
                tenant_email_domain = None

            if mine:
                where = {"owner_user_id": current_user.id}
            else:
                tenant_scope = tenant_from_key or tenant_email_domain
                where = {"tenant_id": tenant_scope} if tenant_scope else {"owner_user_id": current_user.id}

        # Optional collection filter: add to metadata filter if provided
        if collection_like:
            # Chroma where doesn't support ilike contains directly; skip in vector query and post-filter by DB below
            pass

        # Generate query embedding and search in tenant-specific collection
        vector = await embedding_service.generate_embedding(query)
        vs_results = tenant_vector_store.search_by_text(query_text=query, query_embedding=vector, limit=limit, filters=where)

        if not vs_results:
            return KGSearchResponse(query=query, results=[], total_embeddings=kg_pipeline.get_stats(tenant_id=tenant_id)["total_embeddings"])  # type: ignore

        # Get unique object IDs to fetch source files
        unique_object_ids = list(set(r["object_id"] for r in vs_results))

        # Fetch storage objects and enforce access
        q = db.query(StorageObject, User.email.label('owner_email')).outerjoin(User, StorageObject.owner_user_id == User.id).filter(
            StorageObject.id.in_(unique_object_ids),
            StorageObject.tenant_id == tenant_id
        )
        if collection_like:
            like = f"%{collection_like}%"
            q = q.filter(StorageObject.collection_id.ilike(like))

        rows = q.all()
        allowed_ids = set()
        objects_map = {}
        for so, owner_email in rows:
            # Access control: allow owner, public, or admin
            try:
                if not (getattr(so, "is_public", False) or so.owner_user_id == current_user.id or current_user.trust_level == "admin"):
                    continue
            except Exception:
                pass
            allowed_ids.add(so.id)
            # Only serialize the (heavy, ~30k-char) full object when explicitly requested.
            if include_source_file:
                resp = StorageObjectResponse.from_orm(so)
                resp.owner_email = owner_email
                objects_map[so.id] = resp

        # Build chunk-level results
        results: List[SearchResultChunk] = []
        for r in vs_results:
            object_id = r["object_id"]
            if object_id not in allowed_ids:
                continue  # Skip if no access

            # Extract chunk data
            content = r.get("document", "")
            metadata = r.get("metadata", {})
            distance = r["distance"]

            # Calculate similarity score (0-100, higher is better)
            # Distance of 0 = 100% similar, distance of 2 = 0% similar
            similarity_score = max(0, min(100, (1 - distance / 2) * 100))

            # Build result chunk
            chunk = SearchResultChunk(
                content=content,
                embedding_type=metadata.get("embedding_type"),
                embedding_index=metadata.get("embedding_index"),
                metadata=metadata,
                source_file=(objects_map.get(object_id) if include_source_file else None),
                distance=distance,
                similarity_score=round(similarity_score, 1)
            )
            results.append(chunk)

        return KGSearchResponse(query=query, results=results, total_embeddings=kg_pipeline.get_stats(tenant_id=tenant_id)["total_embeddings"])  # type: ignore

    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ KG text search error: {e}")
        raise HTTPException(status_code=500, detail=f"KG search failed: {str(e)}")


@router.get("/kg/vibe-search", response_model=KGSearchResponse)
async def kg_vibe_search(
    query: str = Query(..., description="Vibe/style/mood query (e.g., 'aggressive racing style')"),
    limit: int = Query(10, ge=1, le=50),
    collection_like: Optional[str] = Query(None),
    mine: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    api_key_header: str = Security(_APIKeyHeader(name="X-API-KEY", auto_error=True)),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Advanced vibe/style-based semantic search with GPT-4 re-ranking.

    This endpoint combines:
    1. Vector search for candidate products (top 30)
    2. GPT-4 analysis of AI metadata for intelligent re-ranking
    3. Returns products that best match the vibe/style/mood query

    Perfect for queries like:
    - "aggressive racing style"
    - "minimal professional setup"
    - "wild downhill aesthetic"
    - "dark skull theme"
    """
    try:
        from knowledge_graph.embedding_service import embedding_service
        from knowledge_graph.vector_store import get_vector_store
        from knowledge_graph.pipeline import kg_pipeline
        from openai import AsyncOpenAI
        import os

        # STEP 1: Vector search for candidates (cast wider net)
        candidate_limit = min(30, limit * 3)
        tenant_vector_store = get_vector_store(tenant_id=tenant_id)

        # Build metadata filter
        where: Optional[dict] = None
        if current_user.trust_level == "admin":
            if mine:
                where = {"owner_user_id": current_user.id}
        else:
            tenant_from_key = tenant_id_for_api_key(api_key_header)
            tenant_email_domain = None
            try:
                if getattr(current_user, "email", None) and "@" in current_user.email:
                    tenant_email_domain = current_user.email.split("@", 1)[1].strip().lower()
            except Exception:
                tenant_email_domain = None

            if mine:
                where = {"owner_user_id": current_user.id}
            else:
                tenant_scope = tenant_from_key or tenant_email_domain
                where = {"tenant_id": tenant_scope} if tenant_scope else {"owner_user_id": current_user.id}

        # Vector search
        vector = await embedding_service.generate_embedding(query)
        vs_results = tenant_vector_store.search_by_text(query_text=query, query_embedding=vector, limit=candidate_limit, filters=where)

        if not vs_results or len(vs_results) < 2:
            # Not enough candidates for re-ranking, return regular results
            return KGSearchResponse(query=query, results=[], total_embeddings=kg_pipeline.get_stats(tenant_id=tenant_id)["total_embeddings"])  # type: ignore

        # Get unique object IDs
        unique_object_ids = list(set(r["object_id"] for r in vs_results))

        # STEP 2: Fetch storage objects with AI metadata
        q = db.query(StorageObject, User.email.label('owner_email')).outerjoin(User, StorageObject.owner_user_id == User.id).filter(
            StorageObject.id.in_(unique_object_ids),
            StorageObject.tenant_id == tenant_id
        )
        if collection_like:
            like = f"%{collection_like}%"
            q = q.filter(StorageObject.collection_id.ilike(like))

        rows = q.all()

        # Build objects map with access control
        objects_map = {}
        candidates_for_gpt = []

        for so, owner_email in rows:
            # Access control
            try:
                if not (getattr(so, "is_public", False) or so.owner_user_id == current_user.id or current_user.trust_level == "admin"):
                    continue
            except Exception:
                pass

            objects_map[so.id] = (so, owner_email)

            # Extract AI metadata for GPT-4
            metadata = so.ai_context_metadata or {}
            product_info = {
                "object_id": so.id,
                "title": so.ai_title or so.title or "Unknown",
                "style": metadata.get("product_analysis", {}).get("style"),
                "mood": metadata.get("visual_analysis", {}).get("aesthetics", {}).get("mood"),
                "aesthetic": metadata.get("visual_analysis", {}).get("aesthetics", {}).get("aestheticStyle"),
                "visual_harmony": metadata.get("layout_intelligence", {}).get("visualHarmonyTags", []),
                "emotional_appeal": metadata.get("semantic_properties", {}).get("emotionalAppeal", []),
                "brand_perception": metadata.get("semantic_properties", {}).get("brandPerception"),
                "keywords": metadata.get("semantic_properties", {}).get("keywords", [])[:15],  # Limit keywords
            }
            candidates_for_gpt.append(product_info)

        if not candidates_for_gpt:
            return KGSearchResponse(query=query, results=[], total_embeddings=kg_pipeline.get_stats(tenant_id=tenant_id)["total_embeddings"])  # type: ignore

        # STEP 3: GPT-4 Re-Ranking
        openai_client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

        gpt_prompt = f"""You are an expert at matching products to style/vibe/mood queries.

User query: "{query}"

Analyze these products and rank them by how well they match the query's vibe/style/mood.
Consider: style attributes, mood, aesthetics, emotional appeal, visual themes.

Products:
{json.dumps(candidates_for_gpt, indent=2, ensure_ascii=False)}

Return ONLY a JSON array of object_ids in ranked order (best matches first), limited to top {limit}.
Format: {{"ranked_ids": [123, 456, 789, ...]}}
"""

        gpt_response = await openai_client.chat.completions.create(
            model="gpt-4o-mini",  # Fast and cheap for re-ranking
            messages=[
                {"role": "system", "content": "You are a product ranking expert. Return only valid JSON."},
                {"role": "user", "content": gpt_prompt}
            ],
            temperature=0.3,
            max_tokens=500
        )

        # Parse GPT-4 response
        try:
            gpt_result = json.loads(gpt_response.choices[0].message.content)
            ranked_ids = gpt_result.get("ranked_ids", [])
        except Exception as e:
            print(f"⚠️ GPT-4 response parsing failed: {e}")
            # Fallback to vector search order
            ranked_ids = unique_object_ids[:limit]

        # STEP 4: Build final results in ranked order
        results: List[SearchResultChunk] = []
        for idx, object_id in enumerate(ranked_ids):
            if object_id not in objects_map:
                continue

            so, owner_email = objects_map[object_id]

            # Find original vector search result for distance
            vs_result = next((r for r in vs_results if r["object_id"] == object_id), None)
            distance = vs_result["distance"] if vs_result else 1.0
            similarity_score = max(0, min(100, (1 - distance / 2) * 100))

            # Boost similarity score based on GPT-4 ranking
            rank_boost = (len(ranked_ids) - idx) / len(ranked_ids) * 20  # Up to +20%
            adjusted_similarity = min(100, similarity_score + rank_boost)

            resp = StorageObjectResponse.from_orm(so)
            resp.owner_email = owner_email

            # Get embedding text from vector result
            content = vs_result.get("document", "") if vs_result else ""
            metadata_dict = vs_result.get("metadata", {}) if vs_result else {}

            chunk = SearchResultChunk(
                content=content,
                embedding_type=metadata_dict.get("embedding_type"),
                embedding_index=metadata_dict.get("embedding_index"),
                metadata=metadata_dict,
                source_file=resp,
                distance=distance,
                similarity_score=round(adjusted_similarity, 1)
            )
            results.append(chunk)

        return KGSearchResponse(query=query, results=results, total_embeddings=kg_pipeline.get_stats(tenant_id=tenant_id)["total_embeddings"])  # type: ignore

    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Vibe search error: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Vibe search failed: {str(e)}")


@router.post("/kg/embed/{object_id}")
async def regenerate_embedding(
    object_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Regenerate embedding for a single storage object.

    This endpoint:
    1. Fetches the storage object with AI metadata
    2. Generates new embedding text using create_embedding_text()
    3. Creates new 3072-dim vector using OpenAI
    4. Updates ChromaDB with new embedding

    Use this after updating the create_embedding_text() function to regenerate
    embeddings with richer semantic data.
    """
    # Fetch storage object
    storage_obj = db.query(StorageObject).filter(
        StorageObject.id == object_id,
        StorageObject.tenant_id == tenant_id
    ).first()

    if not storage_obj:
        raise HTTPException(status_code=404, detail="Storage object not found")

    # Access control - only owner or admin
    if storage_obj.owner_user_id != current_user.id and current_user.trust_level != "admin":
        raise HTTPException(status_code=403, detail="Access denied")

    try:
        # Get tenant-specific vector store
        tenant_vector_store = get_vector_store(tenant_id=tenant_id)

        # Create new embedding text using updated create_embedding_text()
        embedding_text = embedding_service.create_embedding_text(storage_obj)

        if not embedding_text or len(embedding_text.strip()) < 10:
            raise HTTPException(
                status_code=400,
                detail="Cannot generate embedding: insufficient metadata"
            )

        # Generate new embedding vector
        embedding_vector_list = await embedding_service.generate_embedding(embedding_text)

        # Create EmbeddingVector object
        from knowledge_graph.models import EmbeddingVector
        embedding_vector = EmbeddingVector(
            object_id=storage_obj.id,
            vector=embedding_vector_list,
            embedding_text=embedding_text,
            metadata={
                "title": storage_obj.ai_title or storage_obj.title,
                "ai_category": storage_obj.ai_category,
                "tenant_id": tenant_id,
                "updated_at": str(storage_obj.updated_at)
            }
        )

        # Update or create embedding in ChromaDB
        tenant_vector_store.upsert_embedding(embedding_vector)

        return {
            "status": "success",
            "object_id": storage_obj.id,
            "embedding_text_length": len(embedding_text),
            "embedding_preview": embedding_text[:200] + "..." if len(embedding_text) > 200 else embedding_text,
            "vector_dimensions": len(embedding_vector),
            "message": "Embedding regenerated successfully"
        }

    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Embedding regeneration error for object {object_id}: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Embedding regeneration failed: {str(e)}")


@router.get("/proxy/{object_id}")
async def proxy_external_file(
    object_id: int,
    no_cache: bool = Query(False, description="Bypass cache and fetch fresh from source"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Transparent proxy for external storage objects.

    Fetches file from external URI (storage_mode="external") with LRU caching.
    Returns the file data with appropriate headers.

    Access control:
    - Owner can always access
    - Public files accessible to all
    - Admin can access all

    Cache configuration (via environment):
    - EXTERNAL_CACHE_DIR: Cache directory
    - EXTERNAL_CACHE_MAX_SIZE_MB: Max cache size (default: 500MB)
    - EXTERNAL_CACHE_MAX_FILES: Max cached files (default: 1000)
    - EXTERNAL_CACHE_TTL_HOURS: Cache TTL (default: 24h)
    """
    # Fetch storage object
    obj = db.query(StorageObject).filter(
        StorageObject.id == object_id,
        StorageObject.tenant_id == tenant_id
    ).first()
    if not obj:
        raise HTTPException(status_code=404, detail="Storage object not found")

    # Safety quarantine — same policy as /storage/media
    _check_quarantine(obj, current_user)

    # Access control
    if not obj.is_public:
        if obj.owner_user_id != current_user.id and current_user.trust_level != "admin":
            raise HTTPException(status_code=403, detail="Access denied")

    # Check if it's an external object
    if obj.storage_mode != "external" or not obj.external_uri:
        raise HTTPException(
            status_code=400,
            detail=f"Object {object_id} is not an external storage object (mode: {obj.storage_mode})"
        )

    try:
        # Fetch file with caching
        file_data, metadata = await fetch_external_file(obj.external_uri, use_cache=not no_cache)

        # Increment download counter
        obj.download_count += 1
        db.commit()

        # Return file with proper headers
        headers = {
            'Content-Type': metadata.get('content-type', obj.mime_type or 'application/octet-stream'),
            'Content-Length': str(len(file_data)),
            'X-Storage-Mode': 'external-proxy',
            'X-Cache': 'HIT' if not no_cache else 'BYPASS',
            'X-External-URI': obj.external_uri[:100],  # Truncated for security
        }

        # Add cache headers if available
        if metadata.get('etag'):
            headers['ETag'] = metadata['etag']
        if metadata.get('last-modified'):
            headers['Last-Modified'] = metadata['last-modified']

        return Response(
            content=file_data,
            media_type=headers['Content-Type'],
            headers=headers
        )

    except httpx.HTTPError as e:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to fetch external file: {str(e)}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Proxy error: {str(e)}"
        )


@router.get("/proxy/stats")
def get_proxy_cache_stats(
    current_user: User = Depends(get_current_user),
):
    """
    Get external proxy cache statistics.

    Admin-only endpoint to monitor cache performance.
    """
    if current_user.trust_level != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    return external_cache.get_stats()


@router.post("/bulk-delete")
def bulk_delete_filtered_objects(
    payload: BulkDeleteRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Deletes all storage objects matching the provided filters."""
    try:
        deleted_count = bulk_delete_objects(
            db,
            name=payload.name,
            collection_like=payload.collection_like,
            context_like=payload.context_like,
            current_user=current_user
        )
        return {"deleted_count": deleted_count, "message": f"{deleted_count} items deleted successfully."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"An unexpected error occurred: {e}")


@router.post("/admin/clean-tenant")
def clean_tenant_objects_endpoint(
    payload: TenantDeleteRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.trust_level != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    result = _clean_tenant_objects(
        db,
        payload.tenant_id,
        current_user,
        batch_size=payload.batch_size or 100,
        dry_run=payload.dry_run,
    )
    result.setdefault(
        "message",
        "Dry run complete. No data deleted." if payload.dry_run else "Tenant cleaned successfully.",
    )
    return result


@router.post("/admin/delete-tenant")
def delete_tenant_objects(
    payload: TenantDeleteRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Deprecated: use /storage/admin/clean-tenant."""
    if current_user.trust_level != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    result = _clean_tenant_objects(
        db,
        payload.tenant_id,
        current_user,
        batch_size=payload.batch_size or 100,
        dry_run=payload.dry_run,
    )
    result["message"] = "Deprecated endpoint. Use /storage/admin/clean-tenant."
    return result


# --- Admin Cleanup Models ---
class CleanupByAgeRequest(BaseModel):
    days: int

class CleanupByUserRequest(BaseModel):
    email: str

class CleanupByCollectionRequest(BaseModel):
    collection_id: str

class CleanupResponse(BaseModel):
    deleted_count: int
    message: str

class UserWithCollections(BaseModel):
    email: str
    display_name: str
    collection_count: int

class CollectionInfo(BaseModel):
    id: str
    name: Optional[str]
    item_count: int
    owner_email: Optional[str] = None

class RenameCollectionRequest(BaseModel):
    old_id: str
    new_id: str
    owner_email: Optional[str] = None

class RenameCollectionResponse(BaseModel):
    updated_count: int
    message: str


# --- Admin Cleanup Endpoints ---

@admin_router.post("/cleanup/by-age", response_model=CleanupResponse)
def admin_cleanup_by_age(
    payload: CleanupByAgeRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Deletes all storage objects older than a specified number of days."""
    if current_user.trust_level != "admin":
        raise HTTPException(status_code=403, detail="Forbidden: Admin access required.")
    try:
        result = admin_routes.cleanup_objects_older_than(db, payload.days)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"An unexpected error occurred: {e}")


@admin_router.post("/cleanup/by-user", response_model=CleanupResponse)
def admin_cleanup_by_user(
    payload: CleanupByUserRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Purges all storage objects owned by a specific user by their email."""
    if current_user.trust_level != "admin":
        raise HTTPException(status_code=403, detail="Forbidden: Admin access required.")
    try:
        result = admin_routes.purge_objects_by_user_email(db, payload.email)
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) # 404 for user not found
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"An unexpected error occurred: {e}")


@admin_router.post("/cleanup/by-collection", response_model=CleanupResponse)
def admin_cleanup_by_collection(
    payload: CleanupByCollectionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Deletes all storage objects within a specific collection."""
    if current_user.trust_level != "admin":
        raise HTTPException(status_code=403, detail="Forbidden: Admin access required.")
    try:
        result = admin_routes.purge_objects_by_collection(db, payload.collection_id)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"An unexpected error occurred: {e}")

@admin_router.get("/users-with-collections", response_model=List[UserWithCollections])
def get_users_with_collections(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get all users who have collections in storage"""
    if current_user.trust_level != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    
    # Query users who have storage objects (with or without collection_id)
    users_with_collections = db.query(
        User.email,
        User.display_name,
        func.count(func.distinct(
            func.coalesce(StorageObject.collection_id, 'null')
        )).label('collection_count')
    ).join(
        StorageObject, User.id == StorageObject.owner_user_id
    ).group_by(
        User.id, User.email, User.display_name
    ).having(
        func.count(StorageObject.id) > 0
    ).order_by(
        User.email
    ).all()
    
    return [
        UserWithCollections(
            email=user.email,
            display_name=user.display_name,
            collection_count=user.collection_count
        )
        for user in users_with_collections
    ]

@admin_router.get("/find-by-mac-job/{job_id}")
def admin_find_storage_by_mac_job(
    job_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant_id: str = Depends(get_tenant_id),
):
    """Find storage object by mac_job_id in metadata_json"""
    if current_user.trust_level != "admin":
        raise HTTPException(status_code=403, detail="Forbidden: Admin access required.")

    try:
        from sqlalchemy import text
        storage_obj = db.query(StorageObject).filter(
            text("JSON_EXTRACT(metadata_json, '$.mac_job_id') = :job_id")
        ).filter(StorageObject.tenant_id == tenant_id).params(job_id=job_id).first()
        
        if not storage_obj:
            raise HTTPException(status_code=404, detail=f"Storage object with mac_job_id {job_id} not found")
            
        return {"id": storage_obj.id, "original_filename": storage_obj.original_filename}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database lookup failed: {str(e)}")

@admin_router.get("/collections", response_model=List[CollectionInfo])
def get_collections_for_user(
    user_email: Optional[str] = Query(None),
    public_only: bool = Query(False),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get collections for a specific user or public collections"""
    if current_user.trust_level != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    
    if public_only:
        # Get collections with no owner (public)
        collections_query = db.query(
            StorageObject.collection_id,
            func.count(StorageObject.id).label('item_count')
        ).filter(
            StorageObject.collection_id.isnot(None),
            StorageObject.collection_id != "",
            StorageObject.owner_user_id.is_(None)
        ).group_by(
            StorageObject.collection_id
        )
    elif user_email:
        # Find the user
        user = db.query(User).filter(User.email == user_email).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        # Get collections for this user (including uncategorized files as 'null' collection)
        collections_query = db.query(
            func.coalesce(StorageObject.collection_id, 'null').label('collection_id'),
            func.count(StorageObject.id).label('item_count')
        ).filter(
            StorageObject.owner_user_id == user.id
        ).group_by(
            func.coalesce(StorageObject.collection_id, 'null')
        )
    else:
        raise HTTPException(status_code=400, detail="Either user_email or public_only must be specified")
    
    collections = collections_query.order_by(StorageObject.collection_id).all()
    
    return [
        CollectionInfo(
            id=collection.collection_id,
            name="Uncategorized Files" if collection.collection_id == 'null' else collection.collection_id,
            item_count=collection.item_count,
            owner_email=user_email if not public_only else None
        )
        for collection in collections
    ]

@admin_router.post("/collections/rename", response_model=RenameCollectionResponse)
def rename_collection(
    payload: RenameCollectionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant_id: str = Depends(get_tenant_id),
):
    """Bulk rename a collection_id across all storage objects.
    If owner_email is provided, only objects belonging to that user are updated.
    """
    if current_user.trust_level != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    try:
        if not payload.old_id or not payload.new_id:
            raise HTTPException(status_code=400, detail="Both old_id and new_id are required")

        # Resolve owner scope if provided
        owner_id = None
        if payload.owner_email:
            user = db.query(User).filter(User.email == payload.owner_email).first()
            if not user:
                raise HTTPException(status_code=404, detail="Owner user not found")
            owner_id = user.id

        q = db.query(StorageObject).filter(
            StorageObject.collection_id == payload.old_id,
            StorageObject.tenant_id == tenant_id
        )
        if owner_id is not None:
            q = q.filter(StorageObject.owner_user_id == owner_id)

        updated_count = 0
        # Perform bulk update
        try:
            updated_count = q.update({StorageObject.collection_id: payload.new_id}, synchronize_session=False)
            db.commit()
        except Exception as e:
            db.rollback()
            raise HTTPException(status_code=500, detail=f"Bulk update failed: {e}")

        return RenameCollectionResponse(updated_count=updated_count, message="Collection renamed successfully")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {e}")






def _hydrate_storage_urls(response_obj, storage_obj, request) -> None:
    """Fill computed file_url/thumbnail_url/webview_url on a response model the same
    way the GET/list handlers do. Write endpoints (upload/fetch/analyze/replace/
    update) return a raw `from_orm` row whose file_url column is empty — the URLs
    are built per-request from the host, not stored — so without this the caller
    gets file_url="" until a follow-up GET (reported by AiApi 2026-06-11). The
    request-derived base_url is required: a config fallback would hand out
    cross-host URLs that 404 against the wrong instance's DB."""
    try:
        base_url = get_base_url_from_request(request)
        urls = build_storage_urls(
            object_id=storage_obj.id,
            tenant_id=storage_obj.tenant_id,
            checksum=storage_obj.checksum,
            metadata_json=storage_obj.metadata_json,
            base_url=base_url,
            storage_mode=storage_obj.storage_mode,
            stored_file_url=storage_obj.file_url if storage_obj.file_url else None,
            mime_type=storage_obj.mime_type,
        )
        response_obj.file_url = urls["file_url"]
        response_obj.thumbnail_url = urls["thumbnail_url"]
        response_obj.webview_url = urls["webview_url"]
    except Exception as _e:
        logging.getLogger("gunicorn.error").warning(
            f"_hydrate_storage_urls failed for obj {getattr(storage_obj, 'id', None)}: {_e}"
        )


@router.post("/upload", response_model=StorageObjectResponse)
async def upload_file(
    request: Request,
    file: Optional[UploadFile] = File(None),
    original_filename: Optional[str] = Form(None),  # Required when file is omitted (path-only register mode)
    context: Optional[str] = Form(None),
    is_public: bool = Form(False),
    owner_email: Optional[str] = Form(None),
    collection_id: Optional[str] = Form(None),
    link_id: Optional[str] = Form(None),  # For linking related files
    analyze: bool = Form(False),  # LEGACY opt-in flag (prefer ai_mode). DEFAULT FALSE — no AI unless explicitly requested
    ai_mode: str = Form("none"),  # AI analysis mode. DEFAULT "none" — caller opts in explicitly: "safety", "vision", "full"
    reference_id: Optional[str] = Form(None),  # For Mac transcoding - reference to existing storage object
    hls_result: bool = Form(False),  # Flag to indicate this ZIP contains HLS transcoding result
    skip_ai_safety: bool = Form(False),  # Allow skipping AI safety check (LEGACY - use ai_mode="none")
    storage_mode: str = Form("copy"),  # "copy" (default), "reference", or "external"
    reference_path: Optional[str] = Form(None),  # Filesystem path when using reference mode
    external_uri: Optional[str] = Form(None),  # External URL when using external mode
    ai_file_path: Optional[str] = Form(None),  # Original file path for AI context (e.g., "/OnEal/2026/Helmets/file.jpg")
    ai_metadata: Optional[str] = Form(None),  # JSON string with semantic metadata for AI (brand, year, category, etc.)
    ai_context_text: Optional[str] = Form(None),  # Unstructured text for AI to parse and extract semantic tags
    ai_tasks: Optional[str] = Form(None),  # CSV/JSON list of steps: safety,vision,product,embedding,kg,notify
    ai_vision_mode: Optional[str] = Form(None),  # auto|generic|product
    ai_context_role: Optional[str] = Form(None),  # product|lifestyle|doc|other
    reuse_existing: bool = Form(True),  # Auto-detect duplicate uploads by filename+tenant+owner
    ttl_hours: Optional[int] = Form(None),  # Auto-delete after N hours (None = permanent)
    transcribe_audio: bool = Form(False),  # Video: demux audio + transcribe via api-ai → audio_transcript (needs an analysis ai_mode)
    x_compute_focal: Optional[str] = Header(None),  # Opt-in: "true"/"1" → compute face focal point at upload (images only, AI-cost-free, default off)
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
    api_key_header: Optional[str] = Security(_APIKeyHeader(name="X-API-KEY", auto_error=False)),
    tenant_id: Optional[str] = Depends(get_tenant_id),
):
    import logging
    glogger = logging.getLogger("gunicorn.error")

    # === Path-only register mode (no multipart bytes) ===
    # When storage_mode=reference AND reference_path is set AND file is omitted,
    # read the file directly from the local filesystem. Saves bandwidth for
    # same-host sync scripts uploading large GLB/PLY files.
    from pathlib import Path as _Path  # local alias — Path is locally rebound later in this function
    if file is None:
        if storage_mode != "reference" or not reference_path:
            raise HTTPException(
                status_code=400,
                detail="file is required unless storage_mode=reference AND reference_path is provided",
            )
        ref_p = _Path(reference_path)
        if not ref_p.is_absolute():
            raise HTTPException(status_code=400, detail="reference_path must be absolute")
        try:
            if not ref_p.exists() or not ref_p.is_file():
                raise HTTPException(status_code=404, detail=f"reference_path not found: {reference_path}")
            data = ref_p.read_bytes()
        except PermissionError:
            raise HTTPException(status_code=403, detail=f"storage-api cannot read {reference_path}")
        derived_filename = original_filename or ref_p.name
        # Derive MIME from real file extension (reference_path) — caller's original_filename
        # may be a relpath-token without extension; we want a usable Content-Type.
        import mimetypes as _mimetypes
        _guessed, _ = _mimetypes.guess_type(str(ref_p))
        # Build a minimal pseudo-UploadFile shim so downstream code can use file.filename / file.content_type
        class _PathOnlyFile:
            filename = derived_filename
            content_type = _guessed  # None falls back to libmagic / suffix detection downstream
        file = _PathOnlyFile()  # type: ignore[assignment]
        glogger.error(f"🚀 UPLOAD_FILE [path-only]: size={len(data)}, ref={reference_path}")
    else:
        # Normal multipart upload
        with open("/tmp/upload_debug.log", "a") as f_log:
            import datetime
            f_log.write(f"{datetime.datetime.now()} - UPLOAD ENTRY: filename={file.filename}\n")
            f_log.flush()
        glogger.error(f"🚀🚀🚀 UPLOAD_FILE ENTRY POINT: filename={file.filename}")
        data = await file.read()
        glogger.error(f"🚀 UPLOAD_FILE: after read, size={len(data)}, context={context}")

    try:
        # Special handling for HLS results: get tenant/owner from original video
        if reference_id and hls_result:
            print(f"📦 HLS Result Upload: reference_id={reference_id}, getting tenant from original video")
            try:
                reference_id_int = int(reference_id)
            except (ValueError, TypeError):
                raise HTTPException(status_code=400, detail=f"Invalid reference_id format: {reference_id}")

            # Find original video to get tenant and owner
            original_video = db.query(StorageObject).filter(StorageObject.id == reference_id_int).first()
            if not original_video:
                raise HTTPException(status_code=404, detail=f"Original video {reference_id} not found")

            # Use tenant and owner from original video
            tenant_id = original_video.tenant_id
            target_owner_id = original_video.owner_user_id
            print(f"📦 Using tenant '{tenant_id}' and owner {target_owner_id} from original video {reference_id_int}")
        else:
            # Normal upload: require authentication
            if not current_user:
                raise HTTPException(status_code=401, detail="Authentication required")
            if not tenant_id:
                raise HTTPException(status_code=400, detail="Tenant ID required")

            # Resolve target owner
            target_owner_id = current_user.id

        if owner_email and not (reference_id and hls_result):
            if current_user.trust_level != "admin":
                raise HTTPException(status_code=403, detail="Only admin may set owner_email")
            owner = db.query(User).filter(User.email == owner_email).first()
            if not owner:
                owner = User(
                    email=owner_email,
                    display_name=owner_email.split("@")[0],
                    password_hash="",
                    api_key=generate_api_key(),
                    trust_level=settings.NEW_USER_TRUST_LEVEL,
                    device_ids=[],
                )
                db.add(owner)
                db.commit()
                db.refresh(owner)
            target_owner_id = owner.id

        # Auto-resolve original video if HLS result arrives without explicit reference_id
        if not reference_id and hls_result:
            try:
                q = db.query(StorageObject).filter(
                    StorageObject.owner_user_id == target_owner_id,
                    StorageObject.tenant_id == tenant_id
                )
                if link_id:
                    # Support semicolon-separated link_ids (use literal + for SQLite)
                    from sqlalchemy import literal
                    pattern = literal(';') + StorageObject.link_id + literal(';')
                    q = q.filter(pattern.like(f'%;{link_id};%'))
                q = q.filter(StorageObject.mime_type.isnot(None)).filter(StorageObject.mime_type.like("video/%"))
                candidate = q.order_by(StorageObject.created_at.desc()).first()
                if candidate:
                    reference_id = str(candidate.id)
                    print(f"📦 Auto-resolved original video by {'link_id' if link_id else 'owner'}: {reference_id}")
            except Exception as _e:
                print(f"📦 WARN: Auto-resolve original video failed: {_e}")

        # Handle Mac transcoding completion: process HLS ZIP and add to original video
        print(f"🔍 DEBUG: reference_id={reference_id}, context={context}, hls_result={hls_result}")
        if reference_id and hls_result:
            # original_video was already loaded above when getting tenant/owner
            print(f"📦 Found original video: {original_video.original_filename}")
            print(f"📦 Processing HLS ZIP: {file.filename}")
            
            # Create temporary ZIP entry for processing
            temp_zip = await save_file_and_record(
                db,
                owner_user_id=target_owner_id,
                data=data,
                original_filename=file.filename,
                context="Temporary HLS ZIP for processing",
                is_public=False,  # Keep it private
                collection_id=None,
                link_id=None,
                tenant_id=tenant_id,
            )
            
            # Mark as HLS result for automatic processing
            if hls_result:
                if not temp_zip.metadata_json:
                    temp_zip.metadata_json = {}
                temp_zip.metadata_json['is_hls_result'] = True
                temp_zip.metadata_json['original_video_id'] = reference_id_int
                print(f"📦 Created temporary HLS ZIP: {temp_zip.id} -> will extract to original video {reference_id_int}")
            
            db.commit()
            db.refresh(temp_zip)
            saved_obj = temp_zip
            
            # Process the HLS ZIP directly (avoid transcoding loop)
            try:
                from storage.service import validate_and_extract_hls_zip
                from pathlib import Path
                
                print(f"📦 DIRECT HLS processing for {temp_zip.id} - avoiding transcoding loop")

                # Get file path and extract HLS into ORIGINAL video's basename directory
                file_path = generic_storage.absolute_path_for_key(temp_zip.object_key, temp_zip.tenant_id)
                original_basename = Path(original_video.object_key).stem
                hls_extract_dir = file_path.parent / original_basename
                
                validation_result = validate_and_extract_hls_zip(file_path, hls_extract_dir)
                
                if validation_result["valid"]:
                    # Update original video with HLS URL (prefer VOD domain for playback)
                    hls_url = build_hls_url(tenant_id, hls_extract_dir.name)
                    original_video.hls_url = hls_url
                    
                    if validation_result.get("width"):
                        original_video.width = validation_result["width"]
                    if validation_result.get("height"):
                        original_video.height = validation_result["height"]
                    if validation_result.get("duration_seconds"):
                        original_video.duration_seconds = validation_result["duration_seconds"]
                    if validation_result.get("bit_rate"):
                        original_video.bit_rate = validation_result["bit_rate"]
                    # Persist HLS metadata for admin UI (variant count, quality list)
                    try:
                        if original_video.metadata_json is None:
                            original_video.metadata_json = {}
                        quality_list = validation_result.get("quality_streams") or []
                        original_video.metadata_json["hls_variant_count"] = len(quality_list)
                        original_video.metadata_json["hls_quality_streams"] = quality_list
                        # Optionally read Mac status.json if provided in the ZIP
                        status_json_path = hls_extract_dir / "status.json"
                        if status_json_path.exists():
                            try:
                                with open(status_json_path, "r") as sf:
                                    status_data = json.load(sf)
                                # Map a few useful fields
                                if isinstance(status_data, dict):
                                    hw = status_data.get("hardwareAcceleration") or status_data.get("hw_accel")
                                    if hw:
                                        original_video.metadata_json["mac_hw_accel"] = hw
                                    br_mode = status_data.get("bitrateMode") or status_data.get("rate_control")
                                    if br_mode:
                                        original_video.metadata_json["mac_bitrate_mode"] = br_mode
                                    codec_profile = status_data.get("codecProfile") or status_data.get("profile")
                                    if codec_profile:
                                        original_video.metadata_json["mac_codec_profile"] = codec_profile
                            except Exception:
                                pass
                    except Exception:
                        pass
                    
                    # Mark transcoding as completed
                    original_video.transcoding_status = "completed"
                    
                    db.commit()
                    print(f"📦 SUCCESS: Original video {original_video.id} updated with HLS URL: {hls_url}")
                    print(f"📦 Transcoding status set to: completed")
                    
                    # Delete ZIP file and temp entry
                    try:
                        file_path.unlink()
                        print(f"📦 ZIP file deleted: {file_path}")
                    except:
                        pass
                        
                    db.delete(temp_zip)
                    db.commit()
                    print(f"📦 Temporary ZIP entry deleted: {temp_zip.id}")
                    
                    saved_obj = original_video
                else:
                    print(f"📦 ERROR: HLS ZIP validation failed: {validation_result}")
                    saved_obj = temp_zip  # Return temp ZIP if processing failed
                    
            except Exception as e:
                print(f"📦 ERROR processing HLS ZIP: {e}")
                saved_obj = temp_zip  # Return temp ZIP if processing failed
                
            # Return immediately after HLS processing (successful or failed) - avoid normal upload pipeline
            _resp = StorageObjectResponse.from_orm(saved_obj)
            _hydrate_storage_urls(_resp, saved_obj, request)
            return _resp
        else:
            # Normal upload logic
            existing = None
            if reuse_existing:
                # Check for existing upload by URI or filename (not checksum)
                existing_q = db.query(StorageObject).filter(
                    StorageObject.owner_user_id == target_owner_id,
                    StorageObject.tenant_id == tenant_id,
                )

                # Match by external_uri if provided, otherwise by filename
                if external_uri:
                    existing_q = existing_q.filter(StorageObject.external_uri == external_uri)
                    existing = existing_q.order_by(StorageObject.created_at.desc()).first()
                    if existing:
                        print(f"♻️  Reusing existing storage object {existing.id} (external_uri match: {external_uri})")
                elif file.filename:
                    existing_q = existing_q.filter(StorageObject.original_filename == file.filename)
                    existing = existing_q.order_by(StorageObject.created_at.desc()).first()
                    if existing:
                        print(f"♻️  Reusing existing storage object {existing.id} (filename match: {file.filename})")

            # Build AI context metadata from parameters
            ai_context_metadata = {}
            if ai_file_path:
                ai_context_metadata["file_path"] = ai_file_path
            if collection_id:
                ai_context_metadata["collection_id"] = collection_id
            if ai_metadata:
                try:
                    ai_context_metadata["metadata"] = json.loads(ai_metadata)
                except json.JSONDecodeError:
                    pass  # Ignore invalid JSON
            if ai_context_text:
                ai_context_metadata["context_text"] = ai_context_text

            if existing:
                saved_obj = await update_file_and_record(
                    db, storage_obj=existing, data=data, context=context
                )
            else:
                saved_obj = await save_file_and_record(
                    db,
                    owner_user_id=target_owner_id,
                    data=data,
                    original_filename=file.filename,
                    context=context,
                    is_public=is_public,
                    collection_id=collection_id,
                    link_id=link_id,
                    storage_mode=storage_mode,
                    reference_path=reference_path,
                    external_uri=external_uri,
                    ai_context_metadata=ai_context_metadata if ai_context_metadata else None,
                    tenant_id=tenant_id,
                    mime_type=file.content_type,
                    ttl_hours=ttl_hours,
                )

            # Attach tenant_id derived from API key for downstream KG processing
            try:
                tenant_from_key = tenant_id_for_api_key(api_key_header)
                if tenant_from_key:
                    if not saved_obj.metadata_json or not isinstance(saved_obj.metadata_json, dict):
                        saved_obj.metadata_json = {}
                    saved_obj.metadata_json["tenant_id"] = tenant_from_key
                    db.commit()
                    db.refresh(saved_obj)
            except Exception:
                pass

        # --- AI Analysis handled by async worker system (check_safety_ai.py) ---
        # The async worker will perform comprehensive analysis with unified Gemini prompt

        # --- Safety-First Workflow (AI safety checks + transcoding for all supported media) ---
        # SKIP transcoding for temporary HLS processing files to avoid infinite loops
        is_temp_hls = (saved_obj.context == "Temporary HLS ZIP for processing")
        is_mac_hls_result = (saved_obj.context == "Mac HLS Transcoding Result")

        glogger.error(f"🔍 TRANSCODING CHECK: saved_obj.id={saved_obj.id}, context='{saved_obj.context}', mime_type='{saved_obj.mime_type}', is_temp_hls={is_temp_hls}, is_mac_hls_result={is_mac_hls_result}")

        if not is_temp_hls and not is_mac_hls_result:
            glogger.error(f"📤 TRIGGERING transcoding for storage object {saved_obj.id}")
            from storage.service import enqueue_ai_safety_and_transcoding

            # Opt-in audio transcription: persist the flag so the async video-analysis
            # task demuxes + transcribes (only fires when an analysis ai_mode runs).
            if transcribe_audio:
                _mj = dict(saved_obj.metadata_json or {})
                _mj["transcribe_audio"] = True
                saved_obj.metadata_json = _mj
                try:
                    db.commit()
                except Exception:
                    db.rollback()

            # DEFAULT IS NO AI PROCESSING. Callers opt in explicitly per upload —
            # storage is a generic media store; each app (Wanderlaut etc.) requests
            # exactly the checks it wants. Saves time/cost on the common path.
            # Opt-in via modern ai_mode ("safety"/"vision"/"full") OR legacy analyze=True.
            effective_ai_mode = ai_mode
            if ai_mode in ("safety", "vision", "full"):
                pass  # explicit modern opt-in
            elif analyze:
                # legacy opt-in (analyze=True, no modern ai_mode) → safety
                effective_ai_mode = "safety"
            else:
                effective_ai_mode = "none"
            if skip_ai_safety and ai_mode not in ("safety", "vision", "full"):
                # legacy hard skip wins unless a modern ai_mode was explicitly set
                effective_ai_mode = "none"

            await enqueue_ai_safety_and_transcoding(
                saved_obj,
                db=db if 'db' in locals() else None,
                skip_ai_safety=skip_ai_safety,
                ai_mode=effective_ai_mode
            )
            glogger.error(f"✅ enqueue_ai_safety_and_transcoding COMPLETED for {saved_obj.id} (ai_mode={effective_ai_mode})")
        else:
            glogger.error(f"📦 SKIPPING transcoding for HLS result/processing file: {saved_obj.id} (context: {saved_obj.context})")
        

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")

    # Opt-in face focal point at upload (X-Compute-Focal: true). AI-cost-free,
    # images only, and strictly best-effort — a detection failure must never
    # break the upload itself.
    if str(x_compute_focal or "").strip().lower() in ("1", "true", "yes") \
            and (saved_obj.mime_type or "").lower().startswith("image/"):
        try:
            _compute_and_store_focal(saved_obj, db, saved_obj.id)
        except Exception as _focal_exc:  # noqa: BLE001
            glogger.error(f"⚠️ X-Compute-Focal failed for {saved_obj.id} (upload unaffected): {_focal_exc}")

    _resp = StorageObjectResponse.from_orm(saved_obj)
    _hydrate_storage_urls(_resp, saved_obj, request)
    return _resp


class RemoteFetchRequest(BaseModel):
    url: str
    context: Optional[str] = None
    is_public: bool = False
    owner_email: Optional[str] = None
    collection_id: Optional[str] = None
    link_id: Optional[str] = None
    filename: Optional[str] = None
    analyze: bool = False


@router.post("/fetch", response_model=StorageObjectResponse)
async def fetch_and_store_remote(
    payload: RemoteFetchRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    api_key_header: str = Security(_APIKeyHeader(name="X-API-KEY", auto_error=True)),
    tenant_id: str = Depends(get_tenant_id),
):
    # Resolve target owner
    target_owner_id = current_user.id
    if payload.owner_email:
        if current_user.trust_level != "admin":
            raise HTTPException(status_code=403, detail="Only admin may set owner_email")
        owner = db.query(User).filter(User.email == payload.owner_email).first()
        if not owner:
            owner = User(
                email=payload.owner_email,
                display_name=payload.owner_email.split("@")[0],
                password_hash="",
                api_key=generate_api_key(),
                trust_level=settings.NEW_USER_TRUST_LEVEL,
                device_ids=[],
            )
            db.add(owner)
            db.commit()
            db.refresh(owner)
        target_owner_id = owner.id

    # Download the remote file
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20.0) as client:
            # Many CDNs (e.g., Wikimedia) require a proper User-Agent and may return errors for missing headers
            request_headers = {
                "User-Agent": "3DPresenter/1.0 (+https://arkturian.com; contact: support@arkturian.com)",
                "Accept": "image/*,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }

            target_url = payload.url
            resp = await client.get(target_url, headers=request_headers)
            
            # Wikimedia 'thumb' fallback: convert thumbnail URL to original file URL if the CDN refuses the thumb
            if resp.status_code >= 400:
                parsed = urlparse(target_url)
                if parsed.netloc.endswith("upload.wikimedia.org") and "/thumb/" in parsed.path:
                    # Convert /wikipedia/commons/thumb/A/AB/Name.jpg/1280px-Name.jpg -> /wikipedia/commons/A/AB/Name.jpg
                    try:
                        path = parsed.path
                        path = path.replace("/wikipedia/commons/thumb/", "/wikipedia/commons/")
                        # Drop the last segment (e.g., 1280px-Name.jpg)
                        if "/" in path:
                            path = path.rsplit("/", 1)[0]
                        fallback_url = urlunparse(parsed._replace(path=path, query=""))
                        resp = await client.get(fallback_url, headers=request_headers)
                        target_url = fallback_url
                    except Exception:
                        pass

            if resp.status_code >= 400:
                # Wikimedia final fallback via Special:FilePath
                parsed2 = urlparse(target_url)
                if parsed2.netloc.endswith("upload.wikimedia.org"):
                    try:
                        file_name_guess = None
                        path_parts = parsed2.path.strip('/').split('/')
                        # Expect formats:
                        # - wikipedia/commons/thumb/<h1>/<h2>/<filename>/<size_px-filename>
                        # - wikipedia/commons/<h1>/<h2>/<filename>
                        if 'thumb' in path_parts:
                            # filename is after the two hash parts
                            # e.g. ['wikipedia','commons','thumb','4','4f','Red_Bull.jpg','1280px-Red_Bull.jpg']
                            try:
                                idx = path_parts.index('thumb')
                                file_name_guess = path_parts[idx + 3]
                            except Exception:
                                pass
                        if not file_name_guess and len(path_parts) >= 5 and path_parts[0] == 'wikipedia' and path_parts[1] == 'commons':
                            file_name_guess = path_parts[4]

                        if file_name_guess:
                            special_url = f"https://commons.wikimedia.org/wiki/Special:FilePath/{file_name_guess}"
                            resp = await client.get(special_url, headers=request_headers)
                            target_url = special_url
                    except Exception:
                        pass

            if resp.status_code >= 400:
                raise HTTPException(status_code=resp.status_code, detail=f"Fetch failed: {resp.text[:200]}")
            data = resp.content
            if len(data) == 0:
                raise HTTPException(status_code=422, detail="Fetched empty response body")
            # Limit size
            if len(data) > settings.MAX_FILE_SIZE:
                raise HTTPException(status_code=413, detail="Remote file too large")
            content_type = resp.headers.get("content-type", "application/octet-stream").split(";")[0].strip()

        # Derive filename
        filename = payload.filename
        if not filename:
            parsed = urlparse(target_url)
            path_name = (parsed.path or "/").rstrip("/").split("/")[-1]
            filename = path_name or "downloaded"
            # Ensure an extension for common image types if missing
            if "." not in filename:
                if content_type == "image/jpeg":
                    filename += ".jpg"
                elif content_type == "image/png":
                    filename += ".png"
                elif content_type == "image/webp":
                    filename += ".webp"

        saved_obj = await save_file_and_record(
            db,
            owner_user_id=target_owner_id,
            data=data,
            original_filename=filename,
            context=payload.context,
            is_public=payload.is_public,
            collection_id=payload.collection_id,
            link_id=payload.link_id,
            tenant_id=tenant_id,
        )

        # Attach tenant_id derived from API key for downstream KG processing
        try:
            tenant_from_key = tenant_id_for_api_key(api_key_header)
            if tenant_from_key:
                if not saved_obj.metadata_json or not isinstance(saved_obj.metadata_json, dict):
                    saved_obj.metadata_json = {}
                saved_obj.metadata_json["tenant_id"] = tenant_from_key
                db.commit()
                db.refresh(saved_obj)
        except Exception:
            pass

        # Optional analysis and safety pipeline
        if payload.analyze:
            try:
                # Build AI context from upload parameters
                ai_context = {}
                if ai_file_path:
                    ai_context["file_path"] = ai_file_path
                if ai_metadata:
                    try:
                        ai_context["metadata"] = json.loads(ai_metadata)
                    except:
                        pass
                if ai_context_text:
                    ai_context["context_text"] = ai_context_text

                analysis_result = await analyze_content(
                    data,
                    saved_obj.mime_type,
                    context=ai_context if ai_context else None
                )

                # Save comprehensive AI analysis results
                saved_obj.ai_category = analysis_result.get("category")
                saved_obj.ai_danger_potential = analysis_result.get("danger_potential")

                # Safety information (detailed)
                if "safety_info" in analysis_result:
                    saved_obj.safety_info = analysis_result.get("safety_info")
                    # Also set legacy field for backward compatibility
                    safety_info = analysis_result.get("safety_info", {})
                    saved_obj.ai_safety_rating = "safe" if safety_info.get("isSafe", True) else "unsafe"

                # Media analysis results
                saved_obj.ai_title = analysis_result.get("ai_title")
                saved_obj.ai_subtitle = analysis_result.get("ai_subtitle")
                saved_obj.ai_tags = analysis_result.get("ai_tags", [])  # Simple tags array for display
                saved_obj.ai_collections = analysis_result.get("ai_collections", [])

                # Store extracted tags and embedding info for Knowledge Graph
                if "extracted_tags" in analysis_result or "embedding_info" in analysis_result:
                    # Create new dict to ensure SQLAlchemy detects the change
                    context_meta = saved_obj.ai_context_metadata.copy() if saved_obj.ai_context_metadata else {}
                    context_meta["extracted_tags"] = analysis_result.get("extracted_tags", {})
                    context_meta["embedding_info"] = analysis_result.get("embedding_info", {})
                    context_meta["mode"] = analysis_result.get("mode", "")
                    context_meta["prompt"] = analysis_result.get("prompt", "")
                    context_meta["response"] = analysis_result.get("ai_response", "")
                    saved_obj.ai_context_metadata = context_meta  # Reassign to trigger SQLAlchemy change detection

                db.commit()
                db.refresh(saved_obj)
            except Exception as e:
                print(f"❌ AI Analysis failed: {e}")
                import traceback
                traceback.print_exc()
                pass

        from storage.service import enqueue_ai_safety_and_transcoding
        try:
            await enqueue_ai_safety_and_transcoding(saved_obj)
        except Exception:
            pass

        _resp = StorageObjectResponse.from_orm(saved_obj)
        _hydrate_storage_urls(_resp, saved_obj, request)
        return _resp

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Fetch-and-store failed: {str(e)}")

@router.post("/objects/{object_id}/analyze", response_model=StorageObjectResponse)
async def analyze_existing_object(
    object_id: int,
    request: Request,
    ai_context_text: Optional[str] = Query(None, description="Optional context hint for AI"),
    ai_tasks: Optional[str] = Query(None, description="CSV/JSON list: safety,vision,product,embedding,kg,notify"),
    ai_vision_mode: Optional[str] = Query(None, description="auto|generic|product"),
    ai_context_role: Optional[str] = Query(None, description="product|lifestyle|doc|other"),
    ai_metadata: Optional[str] = Query(None, description="JSON with domain metadata (e.g., brand, model, features)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant_id: str = Depends(get_tenant_id),
):
    obj = db.query(StorageObject).filter(
        StorageObject.id == object_id,
        StorageObject.tenant_id == tenant_id
    ).first()
    if not obj:
        raise HTTPException(status_code=404, detail="Not found")
    if obj.owner_user_id != current_user.id and current_user.trust_level != "admin":
        raise HTTPException(status_code=403, detail="Forbidden")

    try:
        file_path = generic_storage.absolute_path_for_key(obj.object_key, obj.tenant_id)
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="File missing from storage")

        with open(file_path, "rb") as f:
            data = f.read()

        # Build AI context if provided
        ai_context = None
        if ai_context_text:
            ai_context = {"context_text": ai_context_text}
        if ai_metadata:
            try:
                import json as _json
                meta = _json.loads(ai_metadata)
                if ai_context is None:
                    ai_context = {}
                ai_context["metadata"] = meta
            except Exception:
                pass

        analysis_result = await analyze_content(
            data,
            obj.mime_type,
            context=ai_context,
            vision_mode=ai_vision_mode or "auto",
            ai_tasks_str=ai_tasks,
            context_role=ai_context_role,
        )

        # Save comprehensive AI analysis results
        obj.ai_category = analysis_result.get("category")
        obj.ai_danger_potential = analysis_result.get("danger_potential")

        # Safety information (detailed)
        if "safety_info" in analysis_result:
            obj.safety_info = analysis_result.get("safety_info")
            # Also set legacy field for backward compatibility
            safety_info = analysis_result.get("safety_info", {})
            obj.ai_safety_rating = "safe" if safety_info.get("isSafe", True) else "unsafe"

        # Media analysis results
        obj.ai_title = analysis_result.get("ai_title")
        obj.ai_subtitle = analysis_result.get("ai_subtitle")
        obj.ai_tags = analysis_result.get("ai_tags", [])  # Simple tags array for display
        obj.ai_collections = analysis_result.get("ai_collections", [])

        # Store extracted tags and embedding info for Knowledge Graph
        if "extracted_tags" in analysis_result or "embedding_info" in analysis_result:
            # Create new dict to ensure SQLAlchemy detects the change
            context_meta = obj.ai_context_metadata.copy() if obj.ai_context_metadata else {}
            context_meta["extracted_tags"] = analysis_result.get("extracted_tags", {})
            context_meta["embedding_info"] = analysis_result.get("embedding_info", {})
            context_meta["mode"] = analysis_result.get("mode", "")
            context_meta["prompt"] = analysis_result.get("prompt", "")
            context_meta["response"] = analysis_result.get("ai_response", "")
            obj.ai_context_metadata = context_meta  # Reassign to trigger SQLAlchemy change detection

        db.commit()
        db.refresh(obj)

        # CRITICAL: Trigger Knowledge Graph Pipeline after AI analysis
        # This creates embeddings and processes image URIs from the AI analysis
        try:
            import sys
            from knowledge_graph.pipeline import kg_pipeline

            # Log to file for debugging
            with open("/tmp/kg_pipeline_debug.log", "a") as log:
                log.write(f"\n{'='*80}\n")
                log.write(f"[{__import__('datetime').datetime.now()}] Triggering KG Pipeline for object {obj.id}\n")
                log.flush()

            print(f"🎯 Triggering Knowledge Graph Pipeline for object {obj.id}", file=sys.stderr, flush=True)
            kg_entry = await kg_pipeline.process_storage_object(obj, db)

            with open("/tmp/kg_pipeline_debug.log", "a") as log:
                log.write(f"KG Pipeline returned: {kg_entry}\n")
                log.flush()

            if kg_entry:
                print(f"✅ Knowledge Graph Pipeline completed for object {obj.id}", file=sys.stderr, flush=True)
            else:
                print(f"⚠️ Knowledge Graph Pipeline returned None for object {obj.id}", file=sys.stderr, flush=True)
        except Exception as kg_error:
            # Don't fail the entire request if KG processing fails
            # Just log the error and continue
            with open("/tmp/kg_pipeline_debug.log", "a") as log:
                log.write(f"ERROR: {kg_error}\n")
                import traceback as tb
                tb.print_exc(file=log)
                log.flush()

            print(f"❌ Knowledge Graph Pipeline failed for object {obj.id}: {kg_error}", file=sys.stderr, flush=True)
            import traceback
            traceback.print_exc()

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Analysis failed: {str(e)}")

    _resp = StorageObjectResponse.from_orm(obj)
    _hydrate_storage_urls(_resp, obj, request)
    return _resp


@router.get("/asset-refs")
def get_asset_variant_references(
    link_id: Optional[str] = Query(None, description="Group assets by link; bypasses owner scope"),
    collection_id: Optional[str] = Query(None, description="Filter by collection_id if link_id not provided"),
    object_id: Optional[int] = Query(None, description="Pin to a specific storage object id"),
    role: Optional[str] = Query(None, description="Filter by metadata_json.role (e.g., hero|detail|lifestyle)"),
    mine: bool = True,
    limit: int = Query(100, ge=1, le=5000),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant_id: str = Depends(get_tenant_id),
):
    """Return simplified image/video variant URLs for use in design/print (e.g., Figma).

    Mapping rules:
    - Images: thumb -> thumbnail_url, preview -> webview_url or thumbnail_url, print -> original file_url
    - Videos: hls -> detected master.m3u8, posterThumb -> thumbnail_url, posterPreview -> webview_url or thumbnail_url, print -> original file_url
    Access scope mirrors /storage/list: if link_id is provided we do not restrict by owner; otherwise owner-only unless admin with mine=false.
    """
    # Base query similar to /list
    q = db.query(StorageObject)

    # Filter by tenant_id first for performance
    q = q.filter(StorageObject.tenant_id == tenant_id)

    if object_id is not None:
        q = q.filter(StorageObject.id == object_id)
    elif link_id:
        # Support semicolon-separated link_ids (use literal + for SQLite)
        from sqlalchemy import literal
        pattern = literal(';') + StorageObject.link_id + literal(';')
        q = q.filter(pattern.like(f'%;{link_id};%'))
    elif collection_id:
        q = q.filter(StorageObject.collection_id == collection_id)
    elif not mine and current_user.trust_level == "admin":
        pass
    else:
        q = q.filter(StorageObject.owner_user_id == current_user.id)

    results = q.order_by(StorageObject.created_at.desc()).limit(limit).all()

    items: List[dict] = []

    for obj in results:
        # Enforce access: allow owner, public, or admin
        try:
            if not (getattr(obj, "is_public", False) or obj.owner_user_id == current_user.id or current_user.trust_level == "admin"):
                continue
        except Exception:
            pass

        # Optional role filter from metadata_json
        obj_role = None
        try:
            if obj.metadata_json and isinstance(obj.metadata_json, dict):
                obj_role = obj.metadata_json.get("role")
        except Exception:
            obj_role = None
        if role and (obj_role or "").lower() != role.lower():
            continue

        mime = (obj.mime_type or "").lower()
        mime_major = mime.split("/")[0] if "/" in mime else mime

        # Construct original file URL with checksum for cache-busting
        try:
            from config import settings as _settings
            base = _settings.BASE_URL.rstrip("/")
        except Exception:
            base = "https://api-storage.arkturian.com"
        file_url = f"{base}/uploads/storage/media/{obj.object_key}"
        if getattr(obj, "checksum", None):
            sep = "&" if "?" in file_url else "?"
            file_url = f"{file_url}{sep}v={obj.checksum}"

        # Detect HLS for videos (mirrors logic used in listing)
        hls_url = None
        try:
            path = generic_storage.absolute_path_for_key(obj.object_key, obj.tenant_id)
            basename = Path(obj.object_key).stem
            hls_dir_path = path.parent / basename
            master = hls_dir_path / "master.m3u8"
            if master.exists():
                hls_url = build_hls_url(tenant_id, basename)
        except Exception:
            pass

        if mime_major == "image":
            item = {
                "id": obj.id,
                "type": "image",
                "role": obj_role,
                "variants": {
                    "thumb": getattr(obj, "thumbnail_url", None),
                    "preview": getattr(obj, "webview_url", None) or getattr(obj, "thumbnail_url", None),
                    "print": file_url,
                },
                "width": getattr(obj, "width", None),
                "height": getattr(obj, "height", None),
                "link_id": getattr(obj, "link_id", None),
                "collection_id": getattr(obj, "collection_id", None),
            }
            items.append(item)
        elif mime_major == "video":
            item = {
                "id": obj.id,
                "type": "video",
                "role": obj_role,
                "video": {
                    "hls": hls_url,
                    "posterThumb": getattr(obj, "thumbnail_url", None),
                    "posterPreview": getattr(obj, "webview_url", None) or getattr(obj, "thumbnail_url", None),
                    "print": file_url,
                },
                "width": getattr(obj, "width", None),
                "height": getattr(obj, "height", None),
                "duration": getattr(obj, "duration_seconds", None),
                "link_id": getattr(obj, "link_id", None),
                "collection_id": getattr(obj, "collection_id", None),
            }
            items.append(item)
        else:
            # For other mime types, still expose original as print and thumbnail if available
            item = {
                "id": obj.id,
                "type": mime_major or "other",
                "role": obj_role,
                "variants": {
                    "thumb": getattr(obj, "thumbnail_url", None),
                    "preview": getattr(obj, "webview_url", None) or getattr(obj, "thumbnail_url", None),
                    "print": file_url,
                },
                "link_id": getattr(obj, "link_id", None),
                "collection_id": getattr(obj, "collection_id", None),
            }
            items.append(item)

    return {"count": len(items), "results": items}


class BatchAssetQuery(BaseModel):
    link_id: str
    role: Optional[str] = None


class BatchAssetRequest(BaseModel):
    queries: List[BatchAssetQuery]


@router.post("/asset-refs/batch")
def get_asset_variant_references_batch(
    payload: BatchAssetRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant_id: str = Depends(get_tenant_id),
):
    """Batch resolve asset variants for multiple link_ids.

    This endpoint is optimized for performance when resolving many assets at once.
    Uses a single SQL query with IN clause instead of multiple individual queries.

    Returns: Dict mapping link_id to asset data (same format as /asset-refs endpoint)
    """
    if not payload.queries:
        return {"results": {}}

    # Extract all link_ids from queries
    link_ids = [q.link_id for q in payload.queries]

    # Single SQL query with IN clause for all link_ids
    q = db.query(StorageObject).filter(
        StorageObject.link_id.in_(link_ids),
        StorageObject.tenant_id == tenant_id
    )

    # Apply role filtering if any query specifies it (for simplicity, use first role filter)
    # In production, you might want to handle per-query role filtering differently
    role_filters = [q.role for q in payload.queries if q.role]
    if role_filters:
        # For now, if role is specified, filter to that role for all results
        # This is a simplification; you could implement more complex logic
        pass  # Role filtering happens per-item below

    # Execute query and fetch all results
    results = q.order_by(StorageObject.created_at.desc()).all()

    # Group results by link_id
    results_map: Dict[str, List[dict]] = {}

    for obj in results:
        # Enforce access: allow owner, public, or admin
        try:
            if not (getattr(obj, "is_public", False) or obj.owner_user_id == current_user.id or current_user.trust_level == "admin"):
                continue
        except Exception:
            pass

        obj_link_id = getattr(obj, "link_id", None)
        if not obj_link_id:
            continue

        # Optional role filter from metadata_json
        obj_role = None
        try:
            if obj.metadata_json and isinstance(obj.metadata_json, dict):
                obj_role = obj.metadata_json.get("role")
        except Exception:
            obj_role = None

        # Apply role filtering for this specific link_id query
        query_for_link = next((q for q in payload.queries if q.link_id == obj_link_id), None)
        if query_for_link and query_for_link.role:
            if (obj_role or "").lower() != query_for_link.role.lower():
                continue

        mime = (obj.mime_type or "").lower()
        mime_major = mime.split("/")[0] if "/" in mime else mime

        # Construct original file URL with checksum
        try:
            from config import settings as _settings
            base = _settings.BASE_URL.rstrip("/")
        except Exception:
            base = "https://api-storage.arkturian.com"
        file_url = f"{base}/uploads/storage/media/{obj.object_key}"
        if getattr(obj, "checksum", None):
            sep = "&" if "?" in file_url else "?"
            file_url = f"{file_url}{sep}v={obj.checksum}"

        # Detect HLS for videos
        hls_url = None
        try:
            path = generic_storage.absolute_path_for_key(obj.object_key, obj.tenant_id)
            basename = Path(obj.object_key).stem
            hls_dir_path = path.parent / basename
            master = hls_dir_path / "master.m3u8"
            if master.exists():
                hls_url = build_hls_url(tenant_id, basename)
        except Exception:
            pass

        # Build item based on mime type
        if mime_major == "image":
            item = {
                "id": obj.id,
                "type": "image",
                "role": obj_role,
                "variants": {
                    "thumb": getattr(obj, "thumbnail_url", None),
                    "preview": getattr(obj, "webview_url", None) or getattr(obj, "thumbnail_url", None),
                    "print": file_url,
                },
                "width": getattr(obj, "width", None),
                "height": getattr(obj, "height", None),
                "aspect_ratio": (getattr(obj, "width", None) / getattr(obj, "height", None)) if (getattr(obj, "width", None) and getattr(obj, "height", None)) else None,
                "link_id": obj_link_id,
                "collection_id": getattr(obj, "collection_id", None),
                "alt": getattr(obj, "title", None),
                "original_filename": getattr(obj, "original_filename", None),
                "mime_type": mime,
                "file_size_bytes": getattr(obj, "file_size_bytes", None),
                "created_at": getattr(obj, "created_at", None).isoformat() if getattr(obj, "created_at", None) else None,
                "updated_at": getattr(obj, "updated_at", None).isoformat() if getattr(obj, "updated_at", None) else None,
            }
        elif mime_major == "video":
            item = {
                "id": obj.id,
                "type": "video",
                "role": obj_role,
                "video": {
                    "hls": hls_url,
                    "posterThumb": getattr(obj, "thumbnail_url", None),
                    "posterPreview": getattr(obj, "webview_url", None) or getattr(obj, "thumbnail_url", None),
                    "print": file_url,
                },
                "width": getattr(obj, "width", None),
                "height": getattr(obj, "height", None),
                "aspect_ratio": (getattr(obj, "width", None) / getattr(obj, "height", None)) if (getattr(obj, "width", None) and getattr(obj, "height", None)) else None,
                "duration": getattr(obj, "duration_seconds", None),
                "link_id": obj_link_id,
                "collection_id": getattr(obj, "collection_id", None),
                "alt": getattr(obj, "title", None),
                "original_filename": getattr(obj, "original_filename", None),
                "mime_type": mime,
                "file_size_bytes": getattr(obj, "file_size_bytes", None),
                "created_at": getattr(obj, "created_at", None).isoformat() if getattr(obj, "created_at", None) else None,
                "updated_at": getattr(obj, "updated_at", None).isoformat() if getattr(obj, "updated_at", None) else None,
            }
        else:
            item = {
                "id": obj.id,
                "type": mime_major or "other",
                "role": obj_role,
                "variants": {
                    "thumb": getattr(obj, "thumbnail_url", None),
                    "preview": getattr(obj, "webview_url", None) or getattr(obj, "thumbnail_url", None),
                    "print": file_url,
                },
                "link_id": obj_link_id,
                "collection_id": getattr(obj, "collection_id", None),
                "alt": getattr(obj, "title", None),
                "original_filename": getattr(obj, "original_filename", None),
                "mime_type": mime,
                "file_size_bytes": getattr(obj, "file_size_bytes", None),
                "created_at": getattr(obj, "created_at", None).isoformat() if getattr(obj, "created_at", None) else None,
                "updated_at": getattr(obj, "updated_at", None).isoformat() if getattr(obj, "updated_at", None) else None,
            }

        # Group by link_id - return first match per link_id for simplicity
        # In production, you might want to return all matches or implement more complex grouping
        if obj_link_id not in results_map:
            results_map[obj_link_id] = item

    return {"results": results_map}


@router.head("/media/{object_id}")
def head_media_variant(
    object_id: int,
    variant: Optional[str] = Query(None, description="thumbnail | medium | full"),
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """
    Lightweight HEAD handler so clients (e.g., Unity/AVFoundation) can probe media files.
    Unity's VideoPlayer sends a HEAD request before streaming; without this we return 405,
    which causes AVFoundation to believe the file is invalid/no video tracks.

    NOTE: In production this handler is usually shadowed by the HEAD intercept
    middleware in main.py (which adds X-HLS-URL etc.). Kept as fallback for
    cases where the middleware is bypassed (e.g. internal direct hits).
    """
    obj = db.query(StorageObject).filter(StorageObject.id == object_id).first()
    if not obj:
        raise HTTPException(status_code=404, detail="Not found")

    # Safety gate — same policy as GET. 451 instead of 200 for blocked content.
    _check_quarantine(obj, current_user)

    # Re-use original resolver so we report actual file details.
    # For HEAD it is enough to resolve the source path without heavy processing.
    src_path = _resolve_storage_object_path(obj, refresh=False)
    if not src_path.exists():
        raise HTTPException(status_code=404, detail="Media file missing")

    media_type = (obj.mime_type or "application/octet-stream")
    headers = {
        "Content-Type": media_type,
        "Accept-Ranges": "bytes",
    }
    try:
        headers["Content-Length"] = str(src_path.stat().st_size)
    except Exception:
        pass

    # Starlette will strip the body for HEAD responses automatically.
    return Response(status_code=200, headers=headers)


# Danger-Level ≥ this triggers the public quarantine. Configurable so a tenant
# with stricter compliance can ship a tighter policy without redeploying.
QUARANTINE_DANGER_THRESHOLD = int(os.getenv("QUARANTINE_DANGER_THRESHOLD", "7"))

# Global opt-in: when true, NULL ai_safety_status also blocks public delivery
# (image/video only, to avoid bricking 3D models / audio / markup that
# intentionally skip the safety pipeline). Default false for backward
# compatibility with hosts that have a large legacy backfill of unanalyzed
# public assets (e.g. arkturian had ~7k images NULL before the redis-auth
# fix). Operators can enable per-host once their backfill is complete.
STRICT_PUBLIC_NULL_SAFETY = os.getenv("STRICT_PUBLIC_NULL_SAFETY", "false").lower() in ("1", "true", "yes")


def _check_quarantine(obj, current_user: Optional[User]) -> None:
    """
    Block public delivery of an asset whose AI safety verdict is unsafe, failed,
    or still pending. Raises HTTPException(451 Unavailable For Legal Reasons)
    for non-owner, non-admin requesters. Returns silently if access is allowed.

    Bypass rules:
      - Admins (`trust_level == "admin"`) always pass.
      - Owners (`owner_user_id == current_user.id`) always pass — they need to
        be able to download / delete their own flagged content.
      - Anonymous and non-owner authenticated users get blocked.

    Trigger conditions (any one is enough):
      - `ai_safety_rating == "unsafe"` AND `ai_danger_potential >= threshold`
      - `is_public == True` AND `ai_safety_status` in {"pending", "processing",
        "failed"} — closes the upload→AI-completion race window and the
        AI-error fail-closed path.
      - `metadata_json.requires_safety_verdict == True` (per-object opt-in)
        AND `ai_safety_status != "completed"` — for callers that need
        strict-deny semantics (e.g. anonymous user submissions). NULL status
        blocks under this flag.
      - Global env `STRICT_PUBLIC_NULL_SAFETY=true` AND `is_public == True`
        AND `ai_safety_status is None` AND mime is image/* or video/* — opt-in
        host-wide enforcement once legacy NULLs have been backfilled. 3D
        models, audio, markup etc. that intentionally skip AI are unaffected
        because their mime doesn't match the image/video filter.

    Without either opt-in, NULL `ai_safety_status` (legacy objects, no AI ever
    queued) does NOT trip the gate — we don't retroactively block historical
    assets that pre-date the safety pipeline.
    """
    if obj is None:
        return
    # Tombstoned (pending cascade-delete) → treated as gone for ALL callers. The
    # bytes survive so the saga can untombstone (compensate), but no serving path
    # delivers it. Admin management still works via /admin/storage/{id}/* which
    # query the DB directly and don't go through this gate.
    if getattr(obj, "tombstoned_at", None) is not None:
        raise HTTPException(status_code=404, detail={"error": "asset not found", "code": "tombstoned"})
    # Owner / admin bypass
    if current_user is not None:
        if getattr(current_user, "trust_level", None) == "admin":
            return
        if getattr(obj, "owner_user_id", None) == current_user.id:
            return

    danger = obj.ai_danger_potential or 0
    if obj.ai_safety_rating == "unsafe" and danger >= QUARANTINE_DANGER_THRESHOLD:
        raise HTTPException(
            status_code=451,
            detail={
                "error": "Content blocked by safety policy",
                "ai_safety_rating": obj.ai_safety_rating,
                "ai_danger_potential": danger,
                "code": "safety_blocked",
            },
        )

    # Public asset whose analysis is still IN PROGRESS → block briefly (race window
    # between upload and AI completion). NOTE: "failed" is deliberately NOT blocked
    # here. A `failed` status means the AI safety check could not RUN (backend 503/500,
    # timeout, concurrency-cap) — a transient *infrastructure* error, NOT a verdict that
    # the content is unsafe. Blocking public delivery on an infra error conflates
    # "couldn't check" with "unsafe" and permanently bricks legitimate content on a
    # passing AI hiccup. A genuine unsafe VERDICT is still caught above
    # (rating == "unsafe" + danger). Strict callers that need a hard verdict can still
    # force-block `failed` via metadata_json.requires_safety_verdict (below).
    if obj.is_public and obj.ai_safety_status in ("pending", "processing"):
        raise HTTPException(
            status_code=451,
            detail={
                "error": "Content unavailable: AI safety check in progress",
                "ai_safety_status": obj.ai_safety_status,
                "code": "safety_pending",
            },
        )

    # Per-object opt-in: caller marked the upload as requiring a verdict
    # before public delivery (anonymous user submissions, regulated content,
    # etc.). Anything other than 'completed' blocks — including NULL, which
    # closes the upload→pending-flag race window.
    requires_verdict = False
    md = getattr(obj, "metadata_json", None)
    if isinstance(md, dict):
        requires_verdict = bool(md.get("requires_safety_verdict"))
    if requires_verdict and obj.ai_safety_status != "completed":
        raise HTTPException(
            status_code=451,
            detail={
                "error": "Content unavailable: safety verdict required before public delivery",
                "ai_safety_status": obj.ai_safety_status or "missing",
                "code": "safety_required",
            },
        )

    # Global opt-in: strict-deny NULL on public images/videos
    if (
        STRICT_PUBLIC_NULL_SAFETY
        and obj.is_public
        and obj.ai_safety_status is None
    ):
        mime = (obj.mime_type or "").lower()
        if mime.startswith("image/") or mime.startswith("video/"):
            raise HTTPException(
                status_code=451,
                detail={
                    "error": "Content unavailable: safety verdict missing",
                    "ai_safety_status": "missing",
                    "code": "safety_missing",
                },
            )


# In-memory cache of the 3D-API pipeline version (git SHA prefix), refreshed
# from {THREED_API_URL}/health at most once per _THREED_VERSION_TTL seconds and
# mixed into the GLB derivative cache token. A 3D-API redeploy bumps the version
# → storage's cached GLBs auto-invalidate (we never serve output from an old
# pipeline). Failure-safe: on any probe error we keep the last known value (or
# "unknown"), never blocking GLB serving.
_THREED_VERSION_CACHE = {"value": None, "fetched_at": 0.0}
_THREED_VERSION_TTL = 300  # seconds


def _get_threed_pipeline_version(threed_api_url: str) -> str:
    import time
    now = time.time()
    cached = _THREED_VERSION_CACHE
    if cached["value"] is not None and (now - cached["fetched_at"]) < _THREED_VERSION_TTL:
        return cached["value"]
    version = cached["value"] or "unknown"
    try:
        with httpx.Client(timeout=3.0) as client:
            resp = client.get(f"{threed_api_url}/health")
            if resp.status_code == 200:
                version = (resp.json().get("pipeline_version") or version)[:8]
    except Exception:
        pass  # keep prior/unknown — the version probe must never block serving
    cached["value"] = version
    cached["fetched_at"] = now
    return version


# GLB preset → concrete-param defaults. Kept here as the single source of truth
# shared by the GET handler and the HEAD middleware (main.py) so HEAD's
# X-Transcoded-Size is computed from the exact same cache key GET serves from.
_GLB_PRESET_DEFAULTS = {
    "web":     {"decimate": 0.0,  "texture_format": "webp", "texture_quality": 85, "texture_max_size": 2048, "mesh_compression": "meshopt", "output": "glb"},
    "mobile":  {"decimate": 0.5,  "texture_format": "webp", "texture_quality": 75, "texture_max_size": 1024, "mesh_compression": "meshopt", "output": "glb"},
    "preview": {"decimate": 0.85, "texture_format": "webp", "texture_quality": 60, "texture_max_size": 512,  "mesh_compression": "meshopt", "output": "glb"},
}


def _resolve_glb_params(decimate, texture_format, texture_quality, texture_max_size,
                        mesh_compression, output, preset) -> Dict[str, Any]:
    """Resolve GLB transform params, applying preset defaults for anything not
    explicitly supplied. Returns the concrete params that key the derivative cache."""
    d = _GLB_PRESET_DEFAULTS.get(preset or "", {})
    # decimate is a keep-ratio (fraction of triangles kept): 1.0 = keep all = no
    # decimation. 3D-API treats 0.0 as a legacy alias for 1.0 (keep all), so we
    # normalize 1.0 -> 0.0 here: byte-identical mesh, single canonical cache key
    # (no duplicate derivative between ?decimate=1.0 and the omit/default path).
    _decimate = decimate if decimate is not None else d.get("decimate", 0.0)
    if _decimate == 1.0:
        _decimate = 0.0
    return {
        "decimate":         _decimate,
        "texture_format":   (texture_format  if texture_format   is not None else d.get("texture_format", "webp")).lower(),
        "texture_quality":  texture_quality  if texture_quality  is not None else d.get("texture_quality", 85),
        "texture_max_size": texture_max_size if texture_max_size is not None else d.get("texture_max_size", 2048),
        "mesh_compression": (mesh_compression if mesh_compression is not None else d.get("mesh_compression", "none")).lower(),
        "output":           (output           if output           is not None else d.get("output", "glb")).lower(),
    }


def _glb_cache_path_for(obj, resolved: Dict[str, Any], pipeline_version: str) -> Path:
    """Deterministic GLB derivative cache path for resolved params + 3D-API
    pipeline version. Mirrors the GET handler's cache key exactly."""
    suffix_ext = "zip" if resolved["output"] == "zip" else "glb"
    cache_token_src = (
        f"{obj.checksum or 'nochk'}|{pipeline_version}|{resolved['decimate']}|"
        f"{resolved['texture_format']}|{resolved['texture_quality']}|"
        f"{resolved['texture_max_size']}|{resolved['mesh_compression']}|{resolved['output']}"
    )
    cache_token = hashlib.md5(cache_token_src.encode()).hexdigest()[:12]
    base_name = Path(obj.object_key).stem if obj.object_key else f"obj_{obj.id}"
    glb_cache_name = f"glb_{base_name}_{cache_token}.{suffix_ext}"
    return generic_storage.webview_dir / (obj.tenant_id or "arkturian") / glb_cache_name


@router.get("/media/{object_id}")
def get_media_variant(
    object_id: int,
    variant: Optional[str] = Query(None, description="thumbnail | medium | full"),
    display_for: Optional[str] = Query(None, description="e.g., figma-feed | web | print"),
    width: Optional[int] = Query(None, ge=1),
    height: Optional[int] = Query(None, ge=1),
    aspect_ratio: Optional[str] = Query(
        None,
        description="Optional canvas aspect ratio (e.g., '1:1' or '16:9') applied via letterboxing without stretching",
    ),
    format: Optional[str] = Query(None, description="Images/video-frame: jpg | png | webp. Audio (or a video's audio track): mp3 | m4a | aac | ogg | opus | wav | flac"),
    quality: Optional[int] = Query(None, ge=1, le=100),
    bitrate: Optional[str] = Query(None, description="Audio only: target bitrate for lossy formats, e.g. '192k' or '192000' (ignored for wav/flac)"),
    sample_rate: Optional[int] = Query(None, ge=8000, le=192000, description="Audio only: output sample rate in Hz, e.g. 44100"),
    channels: Optional[int] = Query(None, ge=1, le=2, description="Audio only: output channels (1=mono, 2=stereo)"),
    trim: Optional[bool] = Query(None, description="Set true to crop using stored trim bounds (if available)"),
    refresh: bool = Query(False, description="When true, clears cached derivatives before rendering"),
    download: bool = Query(False, description="When true, serve with Content-Disposition: attachment so the browser downloads the file (named after its original filename) instead of rendering/playing it inline. Keyless for public objects."),
    # GLB / 3D model optimization params — forwarded to 3D-API /optimize/glb
    decimate: Optional[float] = Query(None, ge=0, le=1.0, description="GLB: Blender keep-ratio (fraction of triangles kept). 1.0 = keep all = no decimation (default); 0.5 = keep 50%; 0 = legacy alias for 1.0 (keep all)"),
    texture_format: Optional[str] = Query(None, description="GLB: webp | jpg | png"),
    texture_quality: Optional[int] = Query(None, ge=1, le=100, description="GLB: texture encoder quality 1..100"),
    texture_max_size: Optional[int] = Query(None, ge=0, description="GLB: max texture edge in px (0 = no downscale)"),
    mesh_compression: Optional[str] = Query(None, description="GLB: none | meshopt | draco"),
    output: Optional[str] = Query(None, description="GLB: glb | zip (re-bundled vs. split)"),
    preset: Optional[str] = Query(None, description="GLB: web | mobile | preview (param shortcut)"),
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
    tenant_id: Optional[str] = Depends(get_tenant_id_optional),
):
    """Serve an optimized media variant directly.

    Strategy:
    - If variant == thumbnail: serve existing thumbnail if present.
    - If variant == medium or display_for == figma-feed: serve or create a webview-sized derivative (~max 1920px), persist it, and return it.
    - If variant == full: stream original file.
    - width/height/format/quality can override defaults and will materialize a persistent derivative in webview dir.
    """
    # Import PIL.Image at function level (needed for image processing)
    from io import BytesIO
    from PIL import Image, ImageOps

    try:
        target_aspect_ratio_value = _parse_aspect_ratio_value(aspect_ratio)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid aspect_ratio: {exc}") from exc

    # Public endpoint - allow access to any storage object by globally unique ID
    obj = db.query(StorageObject).filter(StorageObject.id == object_id).first()

    if not obj:
        raise HTTPException(status_code=404, detail="Not found")

    # Safety gate (raises 451 if blocked, bypassed for owner/admin)
    _check_quarantine(obj, current_user)

    media_type_current = (obj.mime_type or "application/octet-stream")
    mime = media_type_current.lower()

    # Audio-format params (?format=mp3|wav|...) only make sense for an audio object
    # or a video's audio track. Reject early with 415 for anything else — image,
    # pdf, glb, json — so a nonsensical ?format=mp3 on a picture fails honestly
    # instead of silently falling through to the image pipeline (which would return
    # a webp). Images never enter the non-image block below, so this guard must live
    # here, before the type-specific branches. Positive audio/video handling happens
    # in the non-image block further down.
    if (format or "").lower() in _AUDIO_FORMAT_SPEC and not (
        mime.startswith("audio/") or mime.startswith("video/")
    ):
        raise HTTPException(
            status_code=415,
            detail=(
                f"Audio format {format!r} was requested but object {object_id} is "
                f"{mime!r}, not an audio or video source."
            ),
        )

    # Build extra headers for downstream consumers (HLS-aware frontends, etc.).
    # CORSMiddleware exposes these via Access-Control-Expose-Headers (see main.py).
    if download:
        # Force a browser "Save As" with the object's real filename instead of
        # inline render/play. Emit both an ASCII-sanitized filename= (legacy
        # fallback) and an RFC 5987 filename*= (UTF-8) so unicode names like
        # "Ólafur Arnalds …" survive. Keyless for public objects — the canonical
        # share/download link is /storage/media/{id}?download=1.
        import re as _re
        from urllib.parse import quote as _quote
        _raw_name = os.path.basename(obj.original_filename or f"file_{object_id}")
        _ascii_name = _re.sub(r"[^A-Za-z0-9._ -]", "_", _raw_name).strip() or f"file_{object_id}"
        _disposition = (
            f"attachment; filename=\"{_ascii_name}\"; "
            f"filename*=UTF-8''{_quote(_raw_name)}"
        )
    else:
        _disposition = "inline"

    _media_extra_headers: Dict[str, str] = {"Content-Disposition": _disposition}
    if obj.mime_type:
        _media_extra_headers["X-Mime-Type"] = obj.mime_type
    if obj.transcoding_status:
        _media_extra_headers["X-Transcoding-Status"] = obj.transcoding_status
    if obj.hls_url and obj.transcoding_status == "completed":
        _media_extra_headers["X-HLS-URL"] = obj.hls_url


    # Resolve file path (handles both local and external URIs)
    src_path = _resolve_storage_object_path(obj, refresh=refresh)

    if mime == "application/pdf":
        if not PDF_RENDER_AVAILABLE:
            return FileResponse(src_path, media_type=media_type_current, headers=_media_extra_headers)
        try:
            preview_path = _ensure_pdf_preview(
                obj,
                src_path,
                width=width,
                height=height,
                refresh=refresh,
            )
            src_path = preview_path
            mime = "image/png"
            media_type_current = "image/png"
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"Failed to render PDF preview: {exc}")

    context_meta = obj.ai_context_metadata or {}
    stored_trim = None
    if isinstance(context_meta, dict):
        stored_trim = context_meta.get("trim_bounds")

    # === GLB / 3D MODEL HANDLING ===
    # Detect 3D model by MIME or filename suffix. Binary GLBs are frequently
    # stored with a generic mime (application/octet-stream) and sometimes
    # without a .glb suffix, so for those generic types additionally sniff the
    # glTF magic bytes ("glTF" = 0x46546C67). Without this, such objects fall
    # through to the plain FileResponse below and the raw original is served
    # with the transform params silently ignored (the "28 MB broken blob"
    # symptom reported from the AR frontend).
    fname_lower = (obj.original_filename or "").lower()
    is_glb_model = (
        mime in {"model/gltf-binary", "model/gltf+json"}
        or fname_lower.endswith((".glb", ".gltf"))
    )
    if not is_glb_model and mime in {"application/octet-stream", "", "application/x-3ds"}:
        try:
            with open(src_path, "rb") as _glb_probe:
                is_glb_model = _glb_probe.read(4) == b"glTF"
        except Exception:
            is_glb_model = False

    # If GLB-specific transform params were supplied but this object is not a
    # GLB, fail explicitly (415) instead of silently serving the untransformed
    # original — which reaches the caller as a corrupt/oversized download.
    glb_params_supplied = any([
        decimate is not None,
        texture_format is not None,
        texture_quality is not None,
        texture_max_size is not None,
        mesh_compression is not None,
        output is not None,
        preset is not None,
    ])
    if glb_params_supplied and not is_glb_model:
        raise HTTPException(
            status_code=415,
            detail=(
                "3D/GLB transform parameters were supplied but object "
                f"{object_id} is not a recognized glTF/GLB model "
                f"(mime={mime!r}, filename={obj.original_filename!r})."
            ),
        )

    if is_glb_model:
        glb_transform_requested = any([
            decimate is not None,
            texture_format is not None,
            texture_quality is not None,
            texture_max_size is not None,
            mesh_compression is not None,
            output is not None,
            preset is not None,
        ])

        if not glb_transform_requested:
            # No transform requested → serve original
            return FileResponse(src_path, media_type="model/gltf-binary", headers=_media_extra_headers)

        # Resolve preset → concrete params via the shared helper (also used by
        # the HEAD middleware so HEAD's X-Transcoded-Size matches GET's output).
        resolved = _resolve_glb_params(
            decimate, texture_format, texture_quality,
            texture_max_size, mesh_compression, output, preset,
        )
        glb_decimate         = resolved["decimate"]
        glb_texture_format   = resolved["texture_format"]
        glb_texture_quality  = resolved["texture_quality"]
        glb_texture_max_size = resolved["texture_max_size"]
        glb_mesh_compression = resolved["mesh_compression"]
        glb_output           = resolved["output"]

        if glb_texture_format not in {"webp", "jpg", "jpeg", "png"}:
            raise HTTPException(status_code=400, detail="texture_format must be webp, jpg, or png")
        if glb_mesh_compression not in {"none", "meshopt", "draco"}:
            raise HTTPException(status_code=400, detail="mesh_compression must be none, meshopt, or draco")
        if glb_output not in {"glb", "zip"}:
            raise HTTPException(status_code=400, detail="output must be glb or zip")

        # Local cache: keyed by checksum + 3D-API pipeline version + params →
        # auto-invalidated when the file is updated OR the 3D-API pipeline is
        # redeployed (so we never serve a GLB produced by an old pipeline).
        threed_api_url = os.getenv("THREED_API_URL", "http://127.0.0.1:8065")
        threed_pipeline_version = _get_threed_pipeline_version(threed_api_url)
        glb_cache_path = _glb_cache_path_for(obj, resolved, threed_pipeline_version)

        if refresh and glb_cache_path.exists():
            glb_cache_path.unlink(missing_ok=True)

        media_type_glb = "application/zip" if glb_output == "zip" else "model/gltf-binary"

        if glb_cache_path.exists():
            # Derivative is content-addressed server-side (checksum+pipeline+params)
            # but the URL itself is NOT immutable (source may be replaced), so a
            # moderate max-age + the existing ETag revalidation is the safe cache
            # contract for browsers/CDNs (requested by CHAP2/Alex for the 67
            # pre-warmed Flora/Fauna GLBs, 2026-07-22).
            _media_extra_headers["Cache-Control"] = "public, max-age=86400"
            return FileResponse(glb_cache_path, media_type=media_type_glb, headers=_media_extra_headers)

        # Forward to 3D-API via multipart upload (portable across storage instances).
        # threed_api_url resolved above for the pipeline-version cache key.
        try:
            with open(src_path, "rb") as glb_f:
                glb_bytes = glb_f.read()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to read GLB source: {e}")

        params_payload = {
            "decimate": glb_decimate,
            "texture_format": glb_texture_format,
            "texture_quality": glb_texture_quality,
            "texture_max_size": glb_texture_max_size,
            "mesh_compression": glb_mesh_compression,
            "output": glb_output,
            # Propagate the caller's refresh down to the 3D-API so ?refresh=true
            # busts BOTH caches. Without this, refresh only clears the local
            # storage cache and the 3D-API happily returns its own stale entry
            # (the cause of the post-fix "still 14.97 MB" for preset=mobile).
            "refresh": refresh,
        }
        try:
            with httpx.Client(timeout=300.0) as client:
                resp = client.post(
                    f"{threed_api_url}/optimize/glb",
                    params=params_payload,
                    files={"file": (obj.original_filename or "model.glb", glb_bytes, "model/gltf-binary")},
                )
        except httpx.ConnectError as exc:
            raise HTTPException(status_code=503, detail=f"3D-API unreachable: {exc}")
        except httpx.TimeoutException:
            raise HTTPException(status_code=504, detail="3D-API timeout during GLB optimization")
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"3D-API call failed: {exc}")

        if resp.status_code != 200:
            try:
                detail = resp.json().get("detail", resp.text[:200])
            except Exception:
                detail = resp.text[:200]
            raise HTTPException(status_code=resp.status_code, detail=f"3D-API: {detail}")

        try:
            glb_cache_path.parent.mkdir(parents=True, exist_ok=True)
            glb_cache_path.write_bytes(resp.content)
        except Exception:
            pass  # Best-effort cache write — still serve the response

        _media_extra_headers["Cache-Control"] = "public, max-age=86400"
        return Response(content=resp.content, media_type=media_type_glb, headers=_media_extra_headers)

    if not mime.startswith("image/"):
        # === AUDIO TRANSCODE ===
        # ?format=<mp3|m4a|aac|ogg|opus|wav|flac> re-encodes an audio object — or
        # the audio track of a video — to the requested container via ffmpeg. This
        # is the audio analogue of image ?format=jpg. Without an audio format param
        # audio is served raw below (backwards compatible). Image formats (jpg/png/
        # webp) are NOT in _AUDIO_FORMAT_SPEC, so a video ?format=jpg still routes to
        # frame extraction — the two never collide.
        audio_fmt = (format or "").lower()
        audio_spec = _AUDIO_FORMAT_SPEC.get(audio_fmt)
        if audio_spec:
            import re as _re

            # Non-audio/video sources were already rejected (415) upstream, so here
            # the object is guaranteed to be audio or video.
            is_audio_src = mime.startswith("audio/")

            # Validate optional encode params. subprocess uses list-args (no shell),
            # so this is for clean 400s rather than injection defence.
            norm_bitrate = None
            if bitrate:
                norm_bitrate = bitrate.strip().lower()
                if not _re.fullmatch(r"\d{2,7}k?", norm_bitrate):
                    raise HTTPException(
                        status_code=400,
                        detail="bitrate must look like '192k' or '192000'",
                    )

            codec, ext, mime_out, is_lossy = audio_spec

            # Short-circuit: source already IS the requested container and no
            # re-encode knobs supplied → serve raw (no needless ffmpeg pass).
            src_ext = Path(obj.original_filename or "").suffix.lower().lstrip(".")
            if is_audio_src and src_ext == ext and not any([norm_bitrate, sample_rate, channels]):
                return FileResponse(src_path, media_type=media_type_current, headers=_media_extra_headers)

            cache_path, mime_out, ext = _audio_cache_path_for(
                obj, audio_fmt,
                bitrate=norm_bitrate, sample_rate=sample_rate, channels=channels,
            )

            if cache_path.exists() and not refresh:
                return FileResponse(cache_path, media_type=mime_out, headers=_media_extra_headers)
            if refresh:
                cache_path.unlink(missing_ok=True)

            try:
                _transcode_audio(
                    src_path,
                    audio_spec,
                    bitrate=norm_bitrate,
                    sample_rate=sample_rate,
                    channels=channels,
                    cache_path=cache_path,
                )
                return FileResponse(cache_path, media_type=mime_out, headers=_media_extra_headers)
            except RuntimeError as exc:
                # Concurrency cap → fall back to raw (like the frame path, OOM guard).
                # Genuine encode failure → surface 500 so it is never a silent no-op.
                if "concurrency limit" in str(exc):
                    print(f"⚠️ Audio transcode concurrency cap for object {object_id}; serving raw")
                    return FileResponse(src_path, media_type=media_type_current, headers=_media_extra_headers)
                print(f"⚠️ Audio transcode failed for object {object_id}: {exc}")
                raise HTTPException(status_code=500, detail=f"Audio transcode failed: {exc}")

        # Handle video frame extraction if image format is requested OR variant=thumbnail
        should_extract_frame = mime.startswith("video/") and (
            (format and format.lower() in ["jpg", "jpeg", "png", "webp"]) or
            variant == "thumbnail"
        )

        if should_extract_frame:
            # Determine cache path for video frame derivative
            target_format = format.lower() if format else "jpg"
            target_width = width or (320 if variant == "thumbnail" else 1920)
            q = quality or 85
            suffix = "jpg" if target_format in ["jpg", "jpeg"] else target_format

            # Build cache filename: vidframe_{object_id}_{width}_q{quality}.{format}
            cache_name = f"vidframe_{object_id}_{target_width}_q{q}.{suffix}"
            cache_path = generic_storage.webview_dir / (obj.tenant_id or "arkturian") / cache_name

            # Check if cached frame exists (skip ffmpeg if so)
            if cache_path.exists() and not refresh:
                media_type_map = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
                return FileResponse(cache_path, media_type=media_type_map.get(suffix, "image/jpeg"), headers=_media_extra_headers)

            acquired = _FFMPEG_FRAME_SEM.acquire(timeout=_FFMPEG_FRAME_WAIT)
            try:
                if not acquired:
                    # Too many concurrent ffmpeg frame-extractions in flight —
                    # fall through to serving the original instead of piling on
                    # memory (OOM guard, 2026-06-04).
                    raise RuntimeError("ffmpeg frame-extraction concurrency limit reached")
                # Extract frame from middle of video
                import subprocess

                # Get video duration to find middle timestamp
                probe_cmd = [
                    "ffprobe", "-v", "error", "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1", str(src_path)
                ]
                result = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=10)
                duration = float(result.stdout.strip()) if result.returncode == 0 and result.stdout.strip() else 0
                timestamp = max(duration / 2, 0)  # Middle of video

                # Extract frame using ffmpeg - output directly to JPEG for speed
                ffmpeg_cmd = [
                    "ffmpeg", "-nostdin", "-threads", "1", "-y",
                    "-ss", str(timestamp), "-i", str(src_path),
                    "-an", "-vframes", "1", "-q:v", "2", str(cache_path)
                ]
                # Ensure cache directory exists
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                frame_result = subprocess.run(ffmpeg_cmd, capture_output=True, timeout=60)

                if frame_result.returncode == 0 and cache_path.exists():
                    # Resize if needed
                    img = Image.open(cache_path)
                    w, h = img.size

                    if target_width and w > target_width:
                        new_h = int(h * (target_width / w))
                        if img.mode in ('P', 'PA'):
                            img = img.convert('RGBA')
                        img = img.resize((target_width, new_h), Image.Resampling.LANCZOS)

                    # Save with requested format/quality
                    save_format = "JPEG" if suffix in ["jpg", "jpeg"] else suffix.upper()
                    if save_format == "JPEG" and img.mode in {"RGBA", "LA", "P"}:
                        img = img.convert("RGB")

                    save_kwargs = {"quality": q, "optimize": True} if save_format == "JPEG" else {"quality": q}
                    img.save(cache_path, format=save_format, **save_kwargs)

                    media_type_map = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
                    return FileResponse(cache_path, media_type=media_type_map.get(save_format, "image/jpeg"), headers=_media_extra_headers)
            except Exception as e:
                print(f"⚠️ Failed to extract video frame for object {object_id}: {e}")
                # Fall through to return original video
            finally:
                if acquired:
                    _FFMPEG_FRAME_SEM.release()

        return FileResponse(src_path, media_type=media_type_current, headers=_media_extra_headers)

    # Compute source paths
    # Try local file first
    # (src_path already resolved above)

    # Auto-generate trim bounds when explicitly requested or default-enabled but missing
    trim_param_supplied = trim is not None
    trim_requested = bool(trim) if trim_param_supplied else False
    default_trim_enabled = bool(context_meta.get("trim_delivery_default")) if isinstance(context_meta, dict) else False

    if (trim_requested or default_trim_enabled) and (not stored_trim or not stored_trim.get("normalized")):
        try:
            generated_meta = _generate_trim_metadata_from_image(src_path)
            if generated_meta:
                if not isinstance(context_meta, dict):
                    context_meta = {}
                context_meta = dict(context_meta)
                context_meta["trim_bounds"] = generated_meta
                db.query(StorageObject).filter(StorageObject.id == object_id).update(
                    {"ai_context_metadata": context_meta}, synchronize_session=False
                )
                db.commit()
                obj.ai_context_metadata = context_meta
                stored_trim = generated_meta
                print(f"✅ Generated trim bounds for object {object_id}")
        except Exception as exc:
            db.rollback()
            print(f"⚠️ Failed to generate trim bounds for object {object_id}: {exc}")
        finally:
            context_meta = obj.ai_context_metadata or {}

    # DEPRECATED: Pre-generated thumbnails are no longer used
    # All thumbnails are now generated on-demand via the image processing pipeline below
    # Keeping this for backwards compatibility with old thumbnail files (if they still exist)
    thumb_path = None
    if obj.object_key:
        # Check if old pre-generated thumbnail exists (backwards compatibility only)
        old_thumb_filename = f"thumb_{Path(obj.object_key).stem}.jpg"
        old_thumb_path = generic_storage.thumbnails_dir / obj.tenant_id / old_thumb_filename
        if old_thumb_path.exists():
            thumb_path = old_thumb_path

    def serve_trimmed_image(
        source_path: Path,
        target_format_value: str,
        quality_value: int,
        target_max_edge: Optional[int],
        requested_width: Optional[int],
        requested_height: Optional[int],
        base_media_type: str,
        target_aspect_ratio: Optional[float],
        cache_path: Optional[Path] = None,
    ) -> Response:
        if not stored_trim or not stored_trim.get("applied"):
            return FileResponse(
                source_path,
                media_type=base_media_type,
                headers=_media_extra_headers,
            )

        # Serve from cache if available
        if cache_path and cache_path.exists():
            media_type_map = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
            return FileResponse(
                cache_path,
                media_type=media_type_map.get(target_format_value, "image/png"),
                headers=_media_extra_headers,
            )

        buffer = BytesIO()
        with Image.open(source_path) as base_img:
            base_img = ImageOps.exif_transpose(base_img)
            # Convert palette to RGBA BEFORE any crop/resize for maximum quality
            if base_img.mode in ('P', 'PA'):
                base_img = base_img.convert('RGBA')

            base_width, base_height = base_img.size
            normalized = stored_trim.get("normalized") or [0.0, 0.0, 1.0, 1.0]
            if len(normalized) != 4:
                normalized = [0.0, 0.0, 1.0, 1.0]
            x1 = int(normalized[0] * base_width)
            y1 = int(normalized[1] * base_height)
            x2 = int(normalized[2] * base_width)
            y2 = int(normalized[3] * base_height)
            x1 = max(0, min(x1, base_width - 1))
            y1 = max(0, min(y1, base_height - 1))
            x2 = max(x1 + 1, min(x2, base_width))
            y2 = max(y1 + 1, min(y2, base_height))

            img = base_img.crop((x1, y1, x2, y2))
            w, h = img.size

            if requested_width and requested_height:
                if target_aspect_ratio:
                    source_ratio = (w / float(h)) if h else 1.0
                    bounding_ratio = requested_width / float(requested_height)
                    if source_ratio >= bounding_ratio:
                        target_w = requested_width
                        target_h = max(int(round(requested_width / max(source_ratio, 1e-9))), 1)
                    else:
                        target_h = requested_height
                        target_w = max(int(round(requested_height * source_ratio)), 1)
                else:
                    target_w, target_h = requested_width, requested_height
            elif target_max_edge:
                if w >= h:
                    target_w = min(target_max_edge, w)
                    target_h = int(h * (target_w / float(w))) if w else h
                else:
                    target_h = min(target_max_edge, h)
                    target_w = int(w * (target_h / float(h))) if h else w
            else:
                target_w, target_h = w, h

            if target_w > 0 and target_h > 0 and (target_w != w or target_h != h):
                # Convert palette/indexed images to RGBA before resize for quality
                if img.mode in ('P', 'PA'):
                    img = img.convert('RGBA')
                img = img.resize((target_w, target_h), Image.Resampling.LANCZOS)

            img = _apply_letterbox_aspect_ratio(img, target_aspect_ratio, target_format_value)

            save_format = {
                "jpg": "JPEG",
                "jpeg": "JPEG",
                "png": "PNG",
                "webp": "WEBP",
            }.get(target_format_value, "PNG")

            media_type_map = {
                "JPEG": "image/jpeg",
                "PNG": "image/png",
                "WEBP": "image/webp",
            }
            media_type = media_type_map.get(save_format, "image/png")

            save_kwargs: Dict[str, Any] = {}
            if save_format == "JPEG":
                if img.mode in {"RGBA", "LA", "P"}:
                    img = img.convert("RGB")
                save_kwargs = {"quality": quality_value, "optimize": True}
            elif save_format == "WEBP":
                save_kwargs = {"quality": quality_value, "method": 6}

            img.save(buffer, format=save_format, **save_kwargs)

        buffer.seek(0)
        content = buffer.getvalue()

        # Cache the trimmed result for next time
        if cache_path:
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_bytes(content)
            except Exception:
                pass

        return Response(content=content, media_type=media_type, headers=_media_extra_headers)

    def _serve_oriented_original(_src):
        """Serve the image original, but bake in EXIF orientation first if it
        carries a non-trivial one. Many consumers (canvas / WebGL / AR / native
        map views) ignore the EXIF orientation tag and would render the image
        rotated — normalising on the server means no consumer has to handle it.
        Images without an orientation tag are served byte-exact (no re-encode)."""
        try:
            with Image.open(_src) as _im:
                _orient = _im.getexif().get(0x0112)  # 274 = Orientation
                if _orient in (None, 0, 1):
                    return FileResponse(_src, media_type=media_type_current, headers=_media_extra_headers)
                _cache_dir = generic_storage.webview_dir / (obj.tenant_id or "_")
                _cache_dir.mkdir(parents=True, exist_ok=True)
                _oriented = _cache_dir / f"oriented_{object_id}.jpg"
                if refresh and _oriented.exists():
                    try:
                        _oriented.unlink()
                    except Exception:
                        pass
                if not _oriented.exists():
                    _fixed = ImageOps.exif_transpose(_im)
                    if _fixed.mode in {"RGBA", "LA", "P"}:
                        _fixed = _fixed.convert("RGB")
                    _fixed.save(_oriented, format="JPEG", quality=95, optimize=True)
                return FileResponse(_oriented, media_type="image/jpeg", headers=_media_extra_headers)
        except Exception as _exc:
            print(f"⚠️ orient-serve fallback for object {object_id}: {_exc}")
            return FileResponse(_src, media_type=media_type_current, headers=_media_extra_headers)

    apply_trim = False
    if stored_trim and stored_trim.get("applied"):
        if trim_param_supplied:
            apply_trim = bool(trim)
        else:
            apply_trim = default_trim_enabled

    # Default: if no hints provided, return original (full) — orientation-normalised
    if not apply_trim and variant is None and display_for is None and not width and not height and not format:
        return _serve_oriented_original(src_path)

    # Derive a deterministic webview name for medium/custom
    if obj.object_key:
        ext = Path(obj.object_key).suffix.lower() or ".jpg"
        base_name = Path(obj.object_key).stem
    else:
        # For external URIs without object_key, use object_id
        ext = ".jpg"
        base_name = f"ext_{object_id}"

    if refresh:
        derivative_prefix = f"web_{base_name}"
        derivative_dirs = {generic_storage.webview_dir}
        if obj.tenant_id:
            derivative_dirs.add(generic_storage.webview_dir / obj.tenant_id)
        for derivative_dir in derivative_dirs:
            if not derivative_dir.exists():
                continue
            for cached_variant in derivative_dir.glob(f"{derivative_prefix}*"):
                try:
                    cached_variant.unlink()
                except Exception:
                    pass

    # Heuristics for target size/format
    target_format = (format or ("webp" if mime != "image/png" else "png")).lower()
    if target_format not in {"jpg", "jpeg", "png", "webp"}:
        target_format = "webp"
    q = quality or (70 if (display_for == "figma-feed" or variant == "medium") else 90)
    max_edge = None
    if variant == "thumbnail":
        # ALWAYS generate thumbnails on-the-fly (no longer using pre-generated files)
        # Pre-generated thumbnails are deprecated - all thumbnails are now dynamic
        max_edge = 320  # Default thumbnail width (increased from 300 to match video frame extraction)
    elif variant == "full":
        if not apply_trim:
            return _serve_oriented_original(src_path)
        max_edge = None
    else:
        # medium or custom
        max_edge = 1920 if variant == "medium" or variant is None else 1920
        if display_for == "figma-feed":
            max_edge = 1024
        if width or height:
            max_edge = max(width or 0, height or 0) or max_edge

    if apply_trim and variant is None and display_for is None and not width and not height:
        max_edge = None

    # If only format is specified (no sizing hints), don't resize - only convert format
    if variant is None and display_for is None and not width and not height and format:
        max_edge = None

    # Prepare destination path for derivative (in tenant subdirectory)
    suffix = "jpg" if target_format in {"jpg", "jpeg"} else target_format
    aspect_ratio_token = (
        f"_ar{int(round(target_aspect_ratio_value * 1000))}" if target_aspect_ratio_value else ""
    )
    trim_token = "_trim" if apply_trim else ""
    dest_name = f"web_{base_name}_{max_edge}e{aspect_ratio_token}{trim_token}_q{q}.{suffix}"
    # Webview derivatives are now stored in tenant subdirectories
    dest_path = generic_storage.webview_dir / obj.tenant_id / dest_name

    if refresh and dest_path.exists():
        dest_path.unlink(missing_ok=True)

    if apply_trim:
        return serve_trimmed_image(
            src_path,
            target_format,
            q,
            max_edge,
            width,
            height,
            media_type_current,
            target_aspect_ratio_value,
            cache_path=dest_path,
        )

    # If derivative exists, serve it
    if dest_path.exists():
        media_type = {
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "png": "image/png",
            "webp": "image/webp",
        }.get(suffix, "image/jpeg")
        return FileResponse(dest_path, media_type=media_type, headers=_media_extra_headers)

    # Generate derivative and persist
    try:
        is_svg_asset = mime in {"image/svg", "image/svg+xml"} or src_path.suffix.lower() == ".svg"
        if is_svg_asset:
            if not CAIROSVG_AVAILABLE:
                raise RuntimeError("CairoSVG not installed for SVG conversion")
            with open(src_path, "rb") as svg_file:
                svg_bytes = svg_file.read()
            raster_bytes = svg2png(bytestring=svg_bytes)  # type: ignore[arg-type]
            base_image = Image.open(BytesIO(raster_bytes))
        else:
            base_image = Image.open(src_path)

        # Honour EXIF orientation so derivatives of rotated photos (iPhone
        # Orientation=6/8) aren't served sideways — resizing otherwise bakes the
        # raw pixels AND drops the tag, leaving the browser no way to correct.
        base_image = ImageOps.exif_transpose(base_image)

        with base_image as img:
            w, h = img.size
            if width and height:
                if target_aspect_ratio_value:
                    source_ratio = (w / float(h)) if h else 1.0
                    bounding_ratio = width / float(height)
                    if source_ratio >= bounding_ratio:
                        target_w = width
                        target_h = max(int(round(width / max(source_ratio, 1e-9))), 1)
                    else:
                        target_h = height
                        target_w = max(int(round(height * source_ratio)), 1)
                else:
                    target_w, target_h = width, height
            elif max_edge:
                if w >= h:
                    target_w = max_edge
                    target_h = int(h * (max_edge / max(w, 1)))
                else:
                    target_h = max_edge
                    target_w = int(w * (max_edge / max(h, 1)))
            else:
                target_w, target_h = w, h

            out = img.convert("RGB") if img.mode in ("RGBA", "LA", "P") and target_format in {"jpg", "jpeg"} else img.copy()
            out = out.resize((target_w, target_h), Image.Resampling.LANCZOS)
            out = _apply_letterbox_aspect_ratio(out, target_aspect_ratio_value, target_format)
            save_kwargs = {}
            if target_format in {"jpg", "jpeg"}:
                save_kwargs = {"quality": q, "optimize": True}
            elif target_format == "webp":
                save_kwargs = {"quality": q, "method": 6}
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            out.save(dest_path, format=("JPEG" if target_format in {"jpg", "jpeg"} else target_format.upper()), **save_kwargs)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Derivative generation failed: {e}")

    media_type = {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "webp": "image/webp",
    }.get(suffix, "image/jpeg")
    return FileResponse(dest_path, media_type=media_type, headers=_media_extra_headers)


@router.get("/media/{object_id}/trim-bounds")
def get_media_trim_bounds(
    object_id: int,
    generate: bool = Query(True, description="Auto-generate trim bounds if missing"),
    return_type: str = Query("rect", description="rect | polygon - Return bounding rect or contour polygon"),
    simplify: float = Query(0.002, description="Polygon simplification factor (0.001-0.05, lower = more detail)"),
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
    tenant_id: Optional[str] = Depends(get_tenant_id_optional),
) -> Dict[str, Any]:
    """
    Get trim bounds for an image as JSON.

    Returns the trim bounding box coordinates or contour polygon.

    Response format (rect):
    {
        "type": "rect",
        "width": 1920,          // Original image width
        "height": 1080,         // Original image height
        "trim_width": 1800,     // Trimmed width
        "trim_height": 1000,    // Trimmed height
        "x1": 60,              // Left edge (pixels)
        "y1": 40,              // Top edge (pixels)
        "x2": 1860,            // Right edge (pixels)
        "y2": 1040,            // Bottom edge (pixels)
        "normalized": [0.03125, 0.037, 0.96875, 0.963],  // [x1, y1, x2, y2] normalized to 0-1
        "applied": true        // Whether trim was actually needed
    }

    Response format (polygon):
    {
        "type": "polygon",
        "width": 1920,
        "height": 1080,
        "points": [[x1, y1], [x2, y2], ...],  // Pixel coordinates
        "normalized_points": [[0.05, 0.1], [0.95, 0.1], ...],  // Normalized 0-1
        "point_count": 24
    }
    """
    # Get storage object from database
    obj = db.query(StorageObject).filter(StorageObject.id == object_id).first()

    if not obj:
        raise HTTPException(status_code=404, detail="Storage object not found")

    # Check permissions
    tenant_id = current_user.tenant_id if current_user else "public"
    if obj.tenant_id != "public":
        if not tenant_id or tenant_id != obj.tenant_id:
            raise HTTPException(status_code=403, detail="Access denied")

    # Check if it's an image
    mime = obj.mime_type or ""
    if not mime.startswith("image/"):
        raise HTTPException(status_code=400, detail="Object is not an image")

    # Get existing trim bounds from metadata
    context_meta = obj.ai_context_metadata or {}
    if not isinstance(context_meta, dict):
        context_meta = {}

    stored_trim = context_meta.get("trim_bounds")

    # Auto-generate if missing and requested
    if generate and (not stored_trim or not stored_trim.get("normalized")):
        try:
            # Resolve file path (handles both local and external URIs)
            src_path = _resolve_storage_object_path(obj)

            # Generate trim metadata
            generated_meta = _generate_trim_metadata_from_image(src_path)

            if generated_meta:
                # Save to database
                context_meta["trim_bounds"] = generated_meta
                db.query(StorageObject).filter(StorageObject.id == object_id).update(
                    {"ai_context_metadata": context_meta},
                    synchronize_session=False
                )
                db.commit()
                stored_trim = generated_meta
                print(f"✅ Generated trim bounds for object {object_id}")
        except Exception as exc:
            db.rollback()
            print(f"⚠️ Failed to generate trim bounds for object {object_id}: {exc}")
            raise HTTPException(status_code=500, detail=f"Failed to compute trim bounds: {exc}")

    # Handle polygon request
    if return_type == "polygon":
        if not OPENCV_AVAILABLE:
            raise HTTPException(status_code=501, detail="OpenCV not available - polygon extraction not supported")

        try:
            # Resolve file path (handles both local and external URIs)
            src_path = _resolve_storage_object_path(obj)

            # Generate polygon contour
            polygon_data = _generate_contour_polygon_from_image(src_path, simplify)
            return polygon_data

        except ImportError as exc:
            raise HTTPException(status_code=501, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to compute polygon: {exc}")

    # Default: return rect bounds
    if not stored_trim:
        raise HTTPException(status_code=404, detail="Trim bounds not available")

    # Add type field and scale factor for consistency
    result = dict(stored_trim)
    result["type"] = "rect"
    # Scale factor: how much of the original the content fills (0-1)
    # Used by renderers to normalize visual size after trim
    w = result.get("width", 1)
    h = result.get("height", 1)
    tw = result.get("trim_width", w)
    th = result.get("trim_height", h)
    result["scale_x"] = round(tw / w, 4) if w else 1.0
    result["scale_y"] = round(th / h, 4) if h else 1.0
    result["scale"] = round(max(tw / w, th / h), 4) if w and h else 1.0
    return result


# ---------------------------------------------------------------------------
# Face-detection focal point (AI-cost-free, opt-in) — Content-Post #4225.
#
# Computes the center of the primary face in an image so downstream consumers
# (e.g. WebConverter) can set object-position for portrait crops instead of
# blindly cropping to the middle/top. Runs entirely locally via OpenCV-YuNet
# (see storage/face_focal.py) — no external AI, no network. Default-off: the
# focal point is only computed when a caller explicitly asks (lazy GET below,
# force POST, or the X-Compute-Focal upload header). Once computed it is cached
# in ai_context_metadata["focal_point"], so site-rebuilds pay zero CPU.
# ---------------------------------------------------------------------------

def _compute_and_store_focal(obj: Any, db: Session, object_id: int) -> Dict[str, Any]:
    """Detect faces on the object's image, persist + return the focal result.

    Raises HTTPException with an honest status on decode/model failure.
    """
    from storage.face_focal import compute_focal_point, FaceDetectionUnavailable

    src_path = _resolve_storage_object_path(obj)
    try:
        result = compute_focal_point(src_path)
    except FaceDetectionUnavailable as exc:
        raise HTTPException(status_code=501, detail=f"Face detection unavailable: {exc}")
    except ValueError as exc:
        # Empty/corrupt/undecodable file — honest 422 instead of a 500.
        raise HTTPException(status_code=422, detail=f"Cannot compute focal point: {exc}")

    context_meta = obj.ai_context_metadata or {}
    if not isinstance(context_meta, dict):
        context_meta = {}
    context_meta["focal_point"] = result
    try:
        db.query(StorageObject).filter(StorageObject.id == object_id).update(
            {"ai_context_metadata": context_meta},
            synchronize_session=False,
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to persist focal point: {exc}")
    print(f"✅ Computed focal point for object {object_id}: {result.get('focal_point')}")
    return result


def _focal_access_check(object_id: int, db: Session, current_user: Optional[User]) -> Any:
    """Load an image object for focal ops (public-by-ID like the media endpoint)."""
    obj = db.query(StorageObject).filter(StorageObject.id == object_id).first()
    if not obj:
        raise HTTPException(status_code=404, detail="Storage object not found")
    # Same safety gate as the media endpoint (451 if quarantined, owner/admin bypass).
    _check_quarantine(obj, current_user)
    mime = (obj.mime_type or "").lower()
    if not mime.startswith("image/"):
        raise HTTPException(status_code=400, detail="Focal point is only available for images")
    return obj


@router.get("/media/{object_id}/focal")
def get_media_focal_point(
    object_id: int,
    generate: bool = Query(True, description="Auto-compute focal point if missing (cached after)"),
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
    tenant_id: Optional[str] = Depends(get_tenant_id_optional),
) -> Dict[str, Any]:
    """Get the face-detection focal point for an image.

    Response::

        {
          "focal_point": {"x_pct": 50.2, "y_pct": 24.7} | null,
          "faces_detected": 1,
          "faces": [{"x_pct", "y_pct", "w_pct", "h_pct", "confidence", "is_primary"}],
          "image_width", "image_height", "model", "computed_at"
        }

    ``focal_point`` is the center of the primary (largest, center-tiebreak) face,
    ``x_pct``/``y_pct`` in 0-100. ``null`` when no face is found — consumers then
    fall back to ``object-position: top``. Computed lazily on first call (unless
    ``generate=false``) and cached; use POST to force a recompute.
    """
    obj = _focal_access_check(object_id, db, current_user)

    context_meta = obj.ai_context_metadata or {}
    if not isinstance(context_meta, dict):
        context_meta = {}
    cached = context_meta.get("focal_point")
    if isinstance(cached, dict) and cached.get("computed_at"):
        return cached

    if not generate:
        raise HTTPException(status_code=404, detail="Focal point not computed yet")

    return _compute_and_store_focal(obj, db, object_id)


@router.post("/media/{object_id}/focal")
def recompute_media_focal_point(
    object_id: int,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
    tenant_id: Optional[str] = Depends(get_tenant_id_optional),
) -> Dict[str, Any]:
    """Force-recompute the focal point (invalidate cache) and return the fresh result."""
    obj = _focal_access_check(object_id, db, current_user)
    return _compute_and_store_focal(obj, db, object_id)


@router.get("/batch/focal")
def get_batch_focal_points(
    object_ids: List[int] = Query(..., alias="id", description="List of storage object IDs"),
    generate: bool = Query(False, description="Auto-compute focal point for objects that lack it"),
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
    tenant_id: Optional[str] = Depends(get_tenant_id_optional),
) -> Dict[str, Any]:
    """Batch focal points: map of object_id -> focal result.

    Efficient path for site-builds that iterate over many portraits. With
    ``generate=false`` (default) only already-cached focal points are returned;
    missing ones are omitted. With ``generate=true`` missing focal points are
    computed + cached on the fly. Per-object failures are reported under
    ``errors`` instead of failing the whole batch.

    Example: ``/storage/media/batch/focal?id=1763&id=3571&generate=true``
    """
    result: Dict[str, Any] = {}
    errors: Dict[str, str] = {}
    for oid in object_ids:
        try:
            obj = _focal_access_check(oid, db, current_user)
        except HTTPException as exc:
            errors[str(oid)] = str(exc.detail)
            continue
        context_meta = obj.ai_context_metadata or {}
        if not isinstance(context_meta, dict):
            context_meta = {}
        cached = context_meta.get("focal_point")
        if isinstance(cached, dict) and cached.get("computed_at"):
            result[str(oid)] = cached
            continue
        if not generate:
            continue
        try:
            result[str(oid)] = _compute_and_store_focal(obj, db, oid)
        except HTTPException as exc:
            errors[str(oid)] = str(exc.detail)
    payload: Dict[str, Any] = {"focal_points": result}
    if errors:
        payload["errors"] = errors
    return payload


@router.get("/batch/trim-bounds")
def get_batch_trim_bounds(
    object_ids: List[int] = Query(..., alias="id", description="List of storage object IDs"),
    generate: bool = Query(False, description="Auto-generate trim bounds if missing"),
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
    tenant_id: Optional[str] = Depends(get_tenant_id_optional),
) -> Dict[str, Any]:
    """
    Batch get trim bounds for multiple storage objects.

    Returns a map of object_id → trim_bounds.
    Objects without trim bounds are omitted from the result.

    Example: /storage/media/batch/trim-bounds?id=1763&id=3571&id=4200
    """
    tenant = current_user.tenant_id if current_user else "public"
    objects = db.query(StorageObject).filter(
        StorageObject.id.in_(object_ids)
    ).all()

    results: Dict[str, Any] = {}

    for obj in objects:
        # Skip non-images
        if not (obj.mime_type or "").startswith("image/"):
            continue

        context_meta = obj.ai_context_metadata or {}
        if not isinstance(context_meta, dict):
            context_meta = {}

        stored_trim = context_meta.get("trim_bounds")

        # Auto-generate if requested and missing
        if generate and (not stored_trim or not stored_trim.get("applied")):
            try:
                src_path = _resolve_storage_object_path(obj)
                generated_meta = _generate_trim_metadata_from_image(src_path)
                if generated_meta and generated_meta.get("applied"):
                    context_meta["trim_bounds"] = generated_meta
                    db.query(StorageObject).filter(StorageObject.id == obj.id).update(
                        {"ai_context_metadata": context_meta}, synchronize_session=False
                    )
                    stored_trim = generated_meta
            except Exception:
                pass

        if stored_trim and stored_trim.get("applied"):
            w = stored_trim.get("width", 1)
            h = stored_trim.get("height", 1)
            tw = stored_trim.get("trim_width", w)
            th = stored_trim.get("trim_height", h)
            results[str(obj.id)] = {
                "normalized": stored_trim.get("normalized"),
                "scale_x": round(tw / w, 4) if w else 1.0,
                "scale_y": round(th / h, 4) if h else 1.0,
                "scale": round(max(tw / w, th / h), 4) if w and h else 1.0,
                "trim_width": tw,
                "trim_height": th,
                "width": w,
                "height": h,
            }

    if generate:
        try:
            db.commit()
        except Exception:
            db.rollback()

    return {
        "count": len(results),
        "total_requested": len(object_ids),
        "bounds": results,
    }


@router.get("/proxy")
async def proxy_external_image(
    url: str = Query(..., description="External image URL to proxy"),
    width: Optional[int] = Query(None, ge=1, description="Target width in pixels"),
    height: Optional[int] = Query(None, ge=1, description="Target height in pixels"),
    format: Optional[str] = Query(None, description="Output format: jpg | png | webp"),
    quality: Optional[int] = Query(None, ge=1, le=100, description="JPEG/WebP quality (1-100)"),
):
    """
    Proxy and transform external images with caching.
    
    This endpoint:
    1. Fetches the external image (with 24h cache)
    2. Applies transformations (resize, format conversion, quality)
    3. Caches the transformed result
    4. Returns the optimized image
    
    Example:
        /storage/proxy?url=https://example.com/image.png&width=400&format=webp&quality=80
    """
    try:
        # Fetch external file (uses cache automatically)
        data, metadata = await fetch_external_file(url, use_cache=True)
        
        # Determine source format from content-type
        content_type = metadata.get('content-type', 'image/jpeg')
        
        # If no transformations requested, return original
        if not width and not height and not format and not quality:
            return Response(content=data, media_type=content_type)
        
        # Apply transformations using PIL
        from PIL import Image
        from io import BytesIO
        
        # Load image
        img = Image.open(BytesIO(data))
        
        # Resize if requested
        if width or height:
            w, h = img.size
            if width and height:
                target_w, target_h = width, height
            elif width:
                target_w = width
                target_h = int(h * (width / max(w, 1)))
            else:  # height only
                target_h = height
                target_w = int(w * (height / max(h, 1)))
            
            if img.mode in ('P', 'PA'):
                img = img.convert('RGBA')
            img = img.resize((target_w, target_h), Image.Resampling.LANCZOS)

        # Convert format if requested
        target_format = (format or "webp").lower()
        if target_format not in {"jpg", "jpeg", "png", "webp"}:
            target_format = "webp"
        
        # Convert to RGB if saving as JPEG
        if target_format in {"jpg", "jpeg"} and img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGB")
        
        # Save to bytes
        output = BytesIO()
        save_kwargs = {}
        if target_format in {"jpg", "jpeg"}:
            save_kwargs = {"quality": quality or 85, "optimize": True}
            pil_format = "JPEG"
            media_type = "image/jpeg"
        elif target_format == "webp":
            save_kwargs = {"quality": quality or 85, "method": 6}
            pil_format = "WEBP"
            media_type = "image/webp"
        else:  # png
            save_kwargs = {"optimize": True}
            pil_format = "PNG"
            media_type = "image/png"
        
        img.save(output, format=pil_format, **save_kwargs)
        output.seek(0)
        
        return Response(content=output.read(), media_type=media_type)
        
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch external URL: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Image processing failed: {str(e)}")


@router.get("/objects/{object_id}", response_model=StorageObjectResponse)
def get_object_metadata(
    object_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant_id: str = Depends(get_tenant_id),
):
    # Try tenant-specific first, then public
    obj = None
    if tenant_id:
        obj = db.query(StorageObject).filter(
            StorageObject.id == object_id,
            StorageObject.tenant_id == tenant_id
        ).first()

    # If not found and no tenant_id, try public objects
    if not obj:
        obj = db.query(StorageObject).filter(
            StorageObject.id == object_id,
            StorageObject.is_public == True
        ).first()

    if not obj:
        raise HTTPException(status_code=404, detail="Not found")

    # Check permissions
    if not obj.is_public:
        if not current_user:
            raise HTTPException(status_code=401, detail="Authentication required")
        if obj.owner_user_id != current_user.id and current_user.trust_level != "admin":
            raise HTTPException(status_code=403, detail="Forbidden")

    response_obj = StorageObjectResponse.from_orm(obj)

    # Build URLs dynamically based on object ID and current request
    base_url = get_base_url_from_request(request)
    urls = build_storage_urls(
        object_id=obj.id,
        tenant_id=obj.tenant_id,
        checksum=obj.checksum,
        metadata_json=obj.metadata_json,
        base_url=base_url,
        storage_mode=obj.storage_mode,
        stored_file_url=obj.file_url if obj.file_url else None,
        mime_type=obj.mime_type,
    )
    response_obj.file_url = urls["file_url"]
    response_obj.thumbnail_url = urls["thumbnail_url"]
    response_obj.webview_url = urls["webview_url"]

    # Check for HLS files if this is a video or pre-transcoded zip
    if (obj.mime_type and obj.mime_type.startswith("video/")) or \
       (obj.mime_type and obj.mime_type in ["application/zip", "application/x-zip-compressed"] and
        obj.original_filename and obj.original_filename.lower().endswith('.zip')):
        try:
            path = generic_storage.absolute_path_for_key(obj.object_key, obj.tenant_id)
            basename = Path(obj.object_key).stem
            hls_dir_path = path.parent / basename

            master = hls_dir_path / "master.m3u8"
            if master.exists():
                response_obj.hls_url = build_hls_url(tenant_id, basename)
                response_obj.transcoding_status = "completed"  # Set status to completed when HLS exists
                size_file = hls_dir_path / "hls_size.txt"
                if size_file.exists():
                    try:
                        with open(size_file, "r") as f:
                            response_obj.transcoded_file_size_bytes = int(f.read())
                    except Exception:
                        pass
            elif (hls_dir_path / "error.log").exists():
                response_obj.transcoding_status = "failed"
                try:
                    with open(hls_dir_path / "error.log", "r") as f:
                        response_obj.transcoding_error = f.read()
                except Exception:
                    pass
        except Exception:
            pass

    return response_obj


@router.get("/list", response_model=StorageListResponse)
def list_objects(
    request: Request,
    mine: bool = True,
    context: Optional[str] = None,
    collection_id: Optional[str] = None,
    collection_like: Optional[str] = Query(None, description="Case-insensitive contains filter for collection_id (use %25 for % wildcards)"),
    link_id: Optional[str] = None,
    ext: Optional[str] = Query(None, description="Filter by original_filename extension, e.g. 'png'"),
    name: Optional[str] = Query(None, description="Filter by filename (contains, case-insensitive)"),
    search: Optional[str] = Query(None, description="Full-text search across filename, title, description, ai_title, ai_tags (case-insensitive)"),
    mime_type: Optional[str] = Query(None, description="Filter by MIME type prefix, e.g. 'video' matches video/*, 'image/png' matches exactly"),
    has_hls: Optional[bool] = Query(None, description="Filter to only objects that have HLS transcoded streams (true) or lack them (false)"),
    min_id: Optional[int] = Query(None, description="Only return objects with id >= min_id"),
    max_id: Optional[int] = Query(None, description="Only return objects with id <= max_id"),
    id: Optional[int] = Query(None, description="Return only the object with this exact id (within the tenant) — exact-id lookup for admin deep-links"),
    sort: Optional[str] = Query(None, description="Sort field: 'created_at' (default), 'id', 'filename', 'file_size'. Prefix with '-' for asc, default desc."),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    limit: int = Query(100, ge=1, le=5000),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant_id: str = Depends(get_tenant_id),
):
    # Join with User table to get owner email
    q = db.query(StorageObject, User.email.label('owner_email')).outerjoin(User, StorageObject.owner_user_id == User.id)

    # Filter by tenant_id first for performance
    q = q.filter(StorageObject.tenant_id == tenant_id)

    # Exact-id lookup (admin deep-link / Search-ID): the most specific filter,
    # returns just that object regardless of pagination/other filters.
    if id is not None:
        q = q.filter(StorageObject.id == id)

    # Prioritize link_id to fetch all related items regardless of owner
    # Supports multiple link_ids separated by semicolon: "101554;101563"
    # Query for "101554" will match both "101554" and "101554;101563"
    if link_id:
        # Pattern: ';{search};' in ';{stored_value};' ensures exact key match
        # Use + operator which translates to || in SQLite
        from sqlalchemy import literal
        pattern = literal(';') + StorageObject.link_id + literal(';')
        q = q.filter(pattern.like(f'%;{link_id};%'))
    elif collection_id:
        q = q.filter(StorageObject.collection_id == collection_id)
    elif collection_like:
        like = f"%{collection_like}%"
        q = q.filter(StorageObject.collection_id.ilike(like))
    elif not mine and current_user.trust_level == "admin":
        pass
    else:
        q = q.filter(StorageObject.owner_user_id == current_user.id)

    if context:
        q = q.filter(StorageObject.context == context)
    
    if name:
        like = f"%{name}%"
        q = q.filter(StorageObject.original_filename.ilike(like))
    if ext:
        # Normalize and filter by file extension (case-insensitive)
        e = ext.lower().lstrip('.')
        like_ext = f"%.{e}"
        q = q.filter(StorageObject.original_filename.ilike(like_ext))

    # Full-text search across multiple fields
    if search:
        like_search = f"%{search}%"
        from sqlalchemy import or_
        q = q.filter(or_(
            StorageObject.original_filename.ilike(like_search),
            StorageObject.title.ilike(like_search),
            StorageObject.description.ilike(like_search),
            StorageObject.ai_title.ilike(like_search),
            StorageObject.ai_subtitle.ilike(like_search),
            StorageObject.ai_tags.ilike(like_search),
        ))

    # MIME type filter (prefix match: "video" matches "video/*", "image/png" matches exactly)
    if mime_type:
        if '/' in mime_type:
            q = q.filter(StorageObject.mime_type == mime_type)
        else:
            q = q.filter(StorageObject.mime_type.ilike(f"{mime_type}/%"))

    # ID range filters
    if min_id is not None:
        q = q.filter(StorageObject.id >= min_id)
    if max_id is not None:
        q = q.filter(StorageObject.id <= max_id)

    # Sorting
    order_col = StorageObject.created_at
    order_asc = False
    if sort:
        raw = sort.lstrip('-')
        if sort.startswith('-'):
            order_asc = True
        sort_map = {
            'created_at': StorageObject.created_at,
            'id': StorageObject.id,
            'filename': StorageObject.original_filename,
            'file_size': StorageObject.file_size_bytes,
        }
        order_col = sort_map.get(raw, StorageObject.created_at)

    q = q.order_by(order_col.asc() if order_asc else order_col.desc())

    # Pagination
    q = q.offset(offset)

    results = q.limit(limit).all()

    # Get base URL from request for dynamic URL building
    base_url = get_base_url_from_request(request)

    response_items: List[StorageObjectResponse] = []
    for storage_obj, owner_email in results:
        response_obj = StorageObjectResponse.from_orm(storage_obj)
        # DEBUG: Log transcoding status for ID 33648
        if storage_obj.id == 33648:
            print(f"DEBUG 33648: After from_orm, transcoding_status = {response_obj.transcoding_status}")
        # Add owner_email to the response
        response_obj.owner_email = owner_email

        # Build URLs dynamically based on object ID and current request
        urls = build_storage_urls(
            object_id=storage_obj.id,
            tenant_id=storage_obj.tenant_id,
            checksum=storage_obj.checksum,
            metadata_json=storage_obj.metadata_json,
            base_url=base_url,
            storage_mode=storage_obj.storage_mode,
            stored_file_url=storage_obj.file_url if storage_obj.file_url else None,
            mime_type=storage_obj.mime_type,
        )
        response_obj.file_url = urls["file_url"]
        response_obj.thumbnail_url = urls["thumbnail_url"]
        response_obj.webview_url = urls["webview_url"]

        # Check for HLS files if this is a video or pre-transcoded zip
        if (storage_obj.mime_type and storage_obj.mime_type.startswith("video/")) or \
           (storage_obj.mime_type and storage_obj.mime_type in ["application/zip", "application/x-zip-compressed"] and
            storage_obj.original_filename and storage_obj.original_filename.lower().endswith('.zip')):
            try:
                path = generic_storage.absolute_path_for_key(storage_obj.object_key, storage_obj.tenant_id)
                basename = Path(storage_obj.object_key).stem
                hls_dir_path = path.parent / basename

                master = hls_dir_path / "master.m3u8"
                if master.exists():
                    response_obj.hls_url = build_hls_url(tenant_id, basename)
                    response_obj.transcoding_status = "completed"  # Set status to completed when HLS exists
                    size_file = hls_dir_path / "hls_size.txt"
                    if size_file.exists():
                        try:
                            with open(size_file, "r") as f:
                                response_obj.transcoded_file_size_bytes = int(f.read())
                        except Exception:
                            pass
                elif (hls_dir_path / "error.log").exists():
                    response_obj.transcoding_status = "failed"
                    try:
                        with open(hls_dir_path / "error.log", "r") as f:
                            error_content = f.read()
                        response_obj.transcoding_error = base64.b64encode(
                            error_content.encode("utf-8")
                        ).decode("ascii")
                    except Exception:
                        response_obj.transcoding_error = base64.b64encode(
                            "Could not read error log.".encode("utf-8")
                        ).decode("ascii")
                elif hls_dir_path.exists():
                    response_obj.transcoding_status = "processing"
                    try:
                        with open(hls_dir_path / "duration.txt", "r") as f:
                            duration = float(f.read())
                        with open(hls_dir_path / "pass.txt", "r") as f:
                            current_pass = int(f.read())

                        out_time_us = 0
                        progress_log = hls_dir_path / "progress.log"
                        if progress_log.exists():
                            with open(progress_log, "r") as f:
                                for line in f:
                                    if "out_time_us" in line:
                                        out_time_us = int(line.strip().split("=")[1])

                        current_time = out_time_us / 1_000_000
                        pass_progress = (current_time / duration) * 100 if duration > 0 else 0.0

                        total_progress = int(((current_pass - 1) * 25) + (pass_progress / 4))
                        response_obj.transcoding_progress = min(max(total_progress, 0), 99)
                    except Exception:
                        response_obj.transcoding_progress = 0
                else:
                    try:
                        with open("/var/log/transcode_queue.txt", "r") as f:
                            if storage_obj.object_key in f.read():
                                response_obj.transcoding_status = "queued"
                    except Exception:
                        pass
            except Exception:
                # Never let a single file's probing break the list
                pass

        response_items.append(response_obj)

    # Post-query filter: has_hls (checked after URL building since HLS is filesystem-based)
    if has_hls is not None:
        response_items = [
            item for item in response_items
            if (bool(item.hls_url) == has_hls)
        ]
    
    # Get total count for pagination (pre has_hls filter)
    total = len(response_items)
    
    return StorageListResponse(
        items=response_items,
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/files/{object_id}")
def download_file(
    object_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant_id: str = Depends(get_tenant_id),
):
    # Try tenant-specific first, then public
    obj = None
    if tenant_id:
        obj = db.query(StorageObject).filter(
            StorageObject.id == object_id,
            StorageObject.tenant_id == tenant_id
        ).first()
    
    # If not found and no tenant_id, try public objects
    if not obj:
        obj = db.query(StorageObject).filter(
            StorageObject.id == object_id,
            StorageObject.is_public == True
        ).first()
    
    if not obj:
        raise HTTPException(status_code=404, detail="Not found")

    # Safety quarantine
    _check_quarantine(obj, current_user)

    # Check permissions
    if not obj.is_public:
        if not current_user:
            raise HTTPException(status_code=401, detail="Authentication required")
        if obj.owner_user_id != current_user.id and current_user.trust_level != "admin":
            raise HTTPException(status_code=403, detail="Forbidden")
    path = generic_storage.absolute_path_for_key(obj.object_key, obj.tenant_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="File missing")
    obj.download_count = (obj.download_count or 0) + 1
    db.commit()
    return FileResponse(path, media_type=obj.mime_type, headers={"Content-Disposition": "inline"})


@router.post("/objects/{object_id}/like", response_model=StorageObjectResponse)
def like_object(
    object_id: int,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(get_tenant_id),
):
    obj = db.query(StorageObject).filter(
        StorageObject.id == object_id,
        StorageObject.tenant_id == tenant_id
    ).first()
    if not obj:
        raise HTTPException(status_code=404, detail="Not found")
    obj.likes = (obj.likes or 0) + 1
    db.commit()
    db.refresh(obj)
    return StorageObjectResponse.from_orm(obj)


def _perform_delete(object_id: int, db: Session, owner_user_id: int, is_admin: bool = False) -> dict:
    """Internal function to perform object deletion with cascade."""
    obj = db.query(StorageObject).filter(StorageObject.id == object_id).first()
    if not obj or (obj.owner_user_id != owner_user_id and not is_admin):
        raise HTTPException(status_code=404, detail="Not found or forbidden")

    # Cancel Mac transcoding job if exists
    if obj.metadata_json and 'mac_job_id' in obj.metadata_json:
        mac_job_id = obj.metadata_json['mac_job_id']
        print(f"--- Attempting to cancel Mac transcoding job: {mac_job_id}")
        try:
            from mac_transcoding_client import mac_transcoding_client
            if mac_transcoding_client.is_available():
                success = mac_transcoding_client.cancel_job(mac_job_id)
                if success:
                    print(f"--- Successfully canceled Mac job: {mac_job_id}")
                else:
                    print(f"--- WARNING: Failed to cancel Mac job: {mac_job_id} (may already be finished)")
            else:
                print(f"--- WARNING: Mac API not available to cancel job: {mac_job_id}")
        except Exception as e:
            print(f"--- ERROR: Could not cancel Mac job {mac_job_id}: {e}")

    # Remove from local transcoding queue if present
    try:
        transcode_queue = Path("/var/log/transcode_queue.txt")
        if transcode_queue.exists():
            with open(transcode_queue, 'r') as f:
                lines = f.readlines()
            # Filter out lines containing this object's path
            filtered_lines = [line for line in lines if obj.object_key not in line]
            if len(filtered_lines) < len(lines):
                with open(transcode_queue, 'w') as f:
                    f.writelines(filtered_lines)
                print(f"--- Removed {obj.object_key} from local transcoding queue")
    except Exception as e:
        print(f"Error cleaning transcoding queue: {e}")

    # CASCADE DELETE: Find and delete all linked child objects
    linked_children = db.query(StorageObject).filter(
        StorageObject.link_id == str(obj.id)
    ).all()

    if linked_children:
        print(f"🔗 Found {len(linked_children)} linked child objects for object {obj.id}")

        for child in linked_children:
            try:
                # Delete child's embedding from tenant-specific collection
                from knowledge_graph.vector_store import get_vector_store
                child_tenant_store = get_vector_store(tenant_id=child.tenant_id)
                child_tenant_store.delete_embedding(child.id)
                print(f"  🗑️  Deleted embedding for child object {child.id}")
            except Exception as e:
                print(f"  ⚠️  Warning: Could not delete embedding for child {child.id}: {e}")

            try:
                # Delete child's physical file and all derived assets (if not external)
                if child.storage_mode != "external":
                    generic_storage.delete(child.object_key, child.tenant_id)
                print(f"  🗑️  Deleted child storage object {child.id}")
            except Exception as e:
                print(f"  ⚠️  Warning: Could not delete file for child {child.id}: {e}")

            # Delete child from DB
            db.delete(child)

        db.commit()
        print(f"✅ Cascade deleted {len(linked_children)} child objects")

    # Delete embedding from Knowledge Graph (tenant-specific collection)
    try:
        from knowledge_graph.vector_store import get_vector_store
        obj_tenant_store = get_vector_store(tenant_id=obj.tenant_id)
        obj_tenant_store.delete_embedding(obj.id)
        print(f"🗑️  Deleted embedding for storage object {obj.id}")
    except Exception as e:
        print(f"Warning: Could not delete embedding for object {obj.id}: {e}")

    # Delete pending async tasks referencing this object
    try:
        deleted_tasks = db.query(AsyncTask).filter(AsyncTask.object_id == obj.id).delete(synchronize_session=False)
        if deleted_tasks:
            print(f"🗑️  Deleted {deleted_tasks} async task(s) for object {obj.id}")
    except Exception as e:
        print(f"Warning: Could not delete async tasks for object {obj.id}: {e}")

    # Delete physical file and all derived assets (only if not external/reference mode)
    # This will delete: original file, thumbnails, webview versions, and HLS directories
    if obj.storage_mode not in ["external", "reference"]:
        generic_storage.delete(obj.object_key, obj.tenant_id)
    db.delete(obj)
    db.commit()

    # Cross-service ref cleanup: notify artrack-api so it can sweep its
    # denormalized storage refs (media_files, waypoint metadata, track JSON
    # lists). Without this, artrack waypoints end up with dangling 100822-
    # style refs that surface as 404s in consuming apps. Fire-and-forget on
    # a thread so the storage delete returns immediately and isn't blocked
    # by artrack downtime — worst case artrack stays out-of-sync until
    # someone runs the manual cleanup script.
    _notify_artrack_storage_deleted(object_id)

    return {"message": "deleted", "cascade_deleted": len(linked_children) if linked_children else 0}


def _notify_artrack_storage_deleted(object_id: int) -> None:
    """POST to artrack-api's cascade-cleanup endpoint. Non-blocking, best-effort."""
    import os
    import threading
    import urllib.request
    import json as _json

    artrack_url = os.getenv("ARTRACK_API_URL", "https://api-artrack.arkturian.com")
    api_key = os.getenv("ARTRACK_API_KEY") or os.getenv("X_API_KEY") or "Inetpass1"
    url = f"{artrack_url}/admin/storage/{object_id}/cleanup-refs"

    def _fire():
        try:
            req = urllib.request.Request(url, method="POST", data=b"")
            req.add_header("X-API-KEY", api_key)
            req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=5) as r:
                resp = _json.loads(r.read())
                summary = resp.get("refs_cleaned", {})
                total = sum(v for v in summary.values() if isinstance(v, int))
                if total:
                    print(f"🔁 artrack ref-cleanup for {object_id}: {summary}")
        except Exception as e:  # noqa: BLE001
            # Non-fatal — log and move on. Manual cleanup script still works.
            print(f"⚠️  artrack ref-cleanup notification failed for {object_id}: {e}")

    threading.Thread(target=_fire, daemon=True, name=f"artrack-cleanup-{object_id}").start()


def _clean_tenant_objects(
    db: Session,
    tenant_id: str,
    current_user: User,
    *,
    batch_size: int = 100,
    dry_run: bool = False,
) -> dict:
    """Iteratively delete all storage objects for a tenant using the robust single-object delete path."""
    if batch_size <= 0:
        batch_size = 100

    total_objects = db.query(func.count(StorageObject.id)).filter(StorageObject.tenant_id == tenant_id).scalar() or 0

    if dry_run:
        sample_ids = [
            row[0]
            for row in (
                db.query(StorageObject.id)
                .filter(StorageObject.tenant_id == tenant_id)
                .order_by(StorageObject.id)
                .limit(min(batch_size, 20))
                .all()
            )
        ]
        return {
            "tenant_id": tenant_id,
            "dry_run": True,
            "total_objects": total_objects,
            "sample_ids": sample_ids,
            "message": "Dry run complete. No data was deleted.",
        }

    deleted = 0
    errors = []

    while True:
        ids = [
            row[0]
            for row in (
                db.query(StorageObject.id)
                .filter(StorageObject.tenant_id == tenant_id)
                .order_by(StorageObject.id)
                .limit(batch_size)
                .all()
            )
        ]

        if not ids:
            break

        deleted_this_batch = 0
        for object_id in ids:
            try:
                _perform_delete(object_id, db, current_user.id, is_admin=True)
                deleted += 1
                deleted_this_batch += 1
            except HTTPException as exc:
                db.rollback()
                errors.append({"object_id": object_id, "error": exc.detail})
            except Exception as exc:
                db.rollback()
                errors.append({"object_id": object_id, "error": str(exc)})

        if deleted_this_batch == 0:
            # Avoid infinite loop if nothing could be deleted (likely due to constraints)
            break

        # Refresh session state before next batch
        db.expire_all()

    remaining = db.query(func.count(StorageObject.id)).filter(StorageObject.tenant_id == tenant_id).scalar() or 0

    return {
        "tenant_id": tenant_id,
        "dry_run": False,
        "total_objects_before": total_objects,
        "deleted": deleted,
        "remaining": remaining,
        "errors": errors[:20],
    }


@router.delete("/{object_id}")
def delete_object(
    object_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete storage object (legacy path without /objects/)."""
    is_admin = current_user.trust_level == "admin"
    return _perform_delete(object_id, db, current_user.id, is_admin)


@router.delete("/objects/{object_id}")
def delete_object_alt(
    object_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete storage object (intuitive path with /objects/)."""
    is_admin = current_user.trust_level == "admin"
    return _perform_delete(object_id, db, current_user.id, is_admin)


class StorageObjectUpdate(BaseModel):
    is_public: Optional[bool] = None
    context: Optional[str] = None
    collection_id: Optional[str] = None
    link_id: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    ai_safety_rating: Optional[str] = None
    metadata_json: Optional[dict] = None
    original_filename: Optional[str] = None
    # TTL — set to null to make object permanent, or to a future datetime to schedule expiry
    expires_at: Optional[datetime] = None
    # Enhanced AI metadata fields
    ai_title: Optional[str] = None
    ai_subtitle: Optional[str] = None
    ai_tags: Optional[list] = None
    ai_collections: Optional[list] = None
    ai_context_metadata: Optional[dict] = None  # ADDED: Debug info (prompt, response, etc.)
    safety_info: Optional[dict] = None


def _clear_object_derivatives(obj) -> None:
    """Drop cached derivatives (webview / oriented / vidframe / thumbnail) for an
    object so the next serve regenerates from freshly-replaced bytes."""
    tenant = obj.tenant_id or "arkturian"
    stem = Path(obj.object_key).stem if obj.object_key else None
    patterns = [f"oriented_{obj.id}.jpg", f"vidframe_{obj.id}_*"]
    if stem:
        patterns += [f"web_{stem}*", f"thumb_{stem}*"]
    for d in {generic_storage.webview_dir, generic_storage.webview_dir / tenant,
              generic_storage.thumbnails_dir, generic_storage.thumbnails_dir / tenant}:
        if not d.exists():
            continue
        for pat in patterns:
            for f in d.glob(pat):
                try:
                    f.unlink()
                except Exception:
                    pass


@router.post("/objects/{object_id}/replace-image", response_model=StorageObjectResponse)
async def replace_object_image(
    object_id: int,
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Replace an object's image bytes IN PLACE (e.g. from the editor: rotate/crop)
    targeting the exact object by id — not the fragile filename+owner reuse path.

    Preserves ALL DB metadata (latitude/longitude, title, description, collection,
    ai_*); only the file + dimensions/checksum change. So a re-encoded edit — whose
    canvas export has no EXIF — keeps its stored GPS etc. Clears stale derivative
    caches so the new bytes are served everywhere immediately."""
    obj = db.query(StorageObject).filter(StorageObject.id == object_id).first()
    if not obj:
        raise HTTPException(status_code=404, detail="Not found")
    if current_user.trust_level != "admin" and obj.owner_user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Forbidden")
    data = await file.read()
    is_image = (
        data[:3] == b"\xff\xd8\xff"            # JPEG
        or data[:8] == b"\x89PNG\r\n\x1a\n"    # PNG
        or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")  # WebP
    )
    if not is_image:
        raise HTTPException(status_code=415, detail="replace-image expects a JPEG/PNG/WebP")
    from storage.domain import update_file_and_record
    await update_file_and_record(db, storage_obj=obj, data=data, context=None)
    try:
        _clear_object_derivatives(obj)
    except Exception as _e:  # noqa: BLE001
        print(f"⚠️ derivative cache clear after replace-image {object_id}: {_e}")
    print(f"✏️  replace-image {object_id} ({len(data)} bytes) — metadata preserved")
    _resp = StorageObjectResponse.from_orm(obj)
    _hydrate_storage_urls(_resp, obj, request)
    return _resp


@router.patch("/objects/{object_id}", response_model=StorageObjectResponse)
async def update_object_metadata(
    object_id: int,
    payload: StorageObjectUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant_id: str = Depends(get_tenant_id),
):
    obj = db.query(StorageObject).filter(
        StorageObject.id == object_id,
        StorageObject.tenant_id == tenant_id
    ).first()
    if not obj or (obj.owner_user_id != current_user.id and current_user.trust_level != "admin"):
        raise HTTPException(status_code=404, detail="Not found or forbidden")

    update_data = payload.dict(exclude_unset=True)

    # Check if ai_context_metadata is being updated
    ai_metadata_updated = "ai_context_metadata" in update_data

    # Track is_public flip from False/None → True. If safety analysis wasn't
    # completed before, we must re-queue it now so the quarantine gate can
    # actually evaluate the content. Otherwise the toggle would expose
    # `is_public=True` objects with stale or absent safety metadata.
    going_public = (
        "is_public" in update_data
        and bool(update_data["is_public"])
        and not bool(obj.is_public)
    )
    needs_safety_recheck = going_public and obj.ai_safety_status != "completed"

    for key, value in update_data.items():
        setattr(obj, key, value)

    if needs_safety_recheck:
        # Force the quarantine gate to block until the new analysis completes.
        obj.ai_safety_status = "pending"

    db.commit()
    db.refresh(obj)

    if needs_safety_recheck:
        try:
            from storage.service import enqueue_ai_safety_and_transcoding
            print(f"🛡️ PATCH is_public→True triggered safety re-check for object {obj.id}")
            await enqueue_ai_safety_and_transcoding(obj, db=db, ai_mode="safety")
        except Exception as e:
            print(f"⚠️ PATCH safety re-check enqueue failed for {obj.id}: {e}")

    # CRITICAL: Trigger Knowledge Graph Pipeline if AI metadata was updated
    # This ensures async worker path creates embeddings and extracts images
    if ai_metadata_updated and obj.ai_context_metadata:
        try:
            import sys
            from knowledge_graph.pipeline import kg_pipeline

            print(f"🎯 PATCH triggered KG Pipeline for object {obj.id}", file=sys.stderr, flush=True)
            kg_entry = await kg_pipeline.process_storage_object(obj, db)

            if kg_entry:
                print(f"✅ KG Pipeline completed for object {obj.id}", file=sys.stderr, flush=True)
            else:
                print(f"⚠️ KG Pipeline returned None for object {obj.id}", file=sys.stderr, flush=True)
        except Exception as kg_error:
            # Don't fail the PATCH request if KG processing fails
            print(f"❌ KG Pipeline failed for object {obj.id}: {kg_error}", file=sys.stderr, flush=True)
            import traceback
            traceback.print_exc()

    _resp = StorageObjectResponse.from_orm(obj)
    _hydrate_storage_urls(_resp, obj, request)
    return _resp


@router.post("/objects/{object_id}/embed", response_model=StorageObjectResponse)
async def create_embedding_for_object(
    object_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Create or refresh the knowledge graph embedding for a storage object.

    Requires access to the object. Safe to call multiple times; replaces prior embedding.
    """
    obj = db.query(StorageObject).filter(
        StorageObject.id == object_id,
        StorageObject.tenant_id == tenant_id
    ).first()
    if not obj:
        raise HTTPException(status_code=404, detail="Not found")
    if obj.owner_user_id != current_user.id and current_user.trust_level != "admin":
        raise HTTPException(status_code=403, detail="Forbidden")

    try:
        from knowledge_graph.pipeline import kg_pipeline
        kg_entry = await kg_pipeline.process_storage_object(obj, db)
        if not kg_entry:
            # Return 202 to indicate accepted but nothing created (e.g., no embeddable text)
            return StorageObjectResponse.from_orm(obj)
        return StorageObjectResponse.from_orm(obj)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Embedding failed: {str(e)}")


@router.get("/objects/{object_id}/processing-status")
async def get_processing_status(
    object_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Get real-time processing status for AI analysis.
    Shows chunking progress, current stage, timing info.
    """
    obj = db.query(StorageObject).filter(
        StorageObject.id == object_id,
        StorageObject.tenant_id == tenant_id
    ).first()
    if not obj:
        raise HTTPException(status_code=404, detail="Not found")
    if obj.owner_user_id != current_user.id and current_user.trust_level != "admin":
        raise HTTPException(status_code=403, detail="Forbidden")

    # Read status from file system
    import os
    import json
    from pathlib import Path

    status_dir = Path(f"/tmp/ai_analysis_status_{object_id}")
    status_file = status_dir / "progress.json"
    error_file = status_dir / "error.log"

    status = {
        "object_id": object_id,
        "filename": obj.original_filename,
        "status": "unknown",
        "stage": None,
        "progress": None,
        "details": {},
        "error": None,
        "started_at": None,
        "updated_at": None
    }

    # Check if completed
    if obj.ai_title:
        status["status"] = "completed"
        status["stage"] = "finished"
        status["details"] = {
            "embeddings_created": len(obj.ai_context_metadata.get("embedding_info", {}).get("embeddingsList", [])) if obj.ai_context_metadata else 0,
            "mode": obj.ai_context_metadata.get("mode") if obj.ai_context_metadata else None,
            "chunks": obj.ai_context_metadata.get("embedding_info", {}).get("metadata", {}).get("chunks_processed") if obj.ai_context_metadata else None
        }
        return status

    # Check for errors
    if error_file.exists():
        status["status"] = "failed"
        status["error"] = error_file.read_text()[:500]  # First 500 chars
        return status

    # Check for active processing
    if status_file.exists():
        try:
            progress_data = json.loads(status_file.read_text())
            status.update(progress_data)
            status["status"] = "processing"
        except Exception as e:
            status["status"] = "processing"
            status["details"]["read_error"] = str(e)
    elif status_dir.exists():
        # Directory exists but no progress file yet
        status["status"] = "queued"
        status["stage"] = "initializing"
    else:
        # No status directory - either queued or not started
        status["status"] = "queued"
        status["stage"] = "waiting"

    return status


class TransferOwnerByLinkRequest(BaseModel):
    link_id: str
    owner_email: str


@admin_router.post("/transfer_owner_by_link")
def transfer_owner_by_link(
    payload: TransferOwnerByLinkRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Admin-only: Transfer ownership of all storage objects that share the given link_id
    to the user identified by owner_email. Creates the user if necessary.
    """
    if current_user.trust_level != "admin":
        raise HTTPException(status_code=403, detail="Forbidden: Admin access required.")

    if not payload.link_id:
        raise HTTPException(status_code=400, detail="link_id is required")

    # Resolve or create target owner
    owner = db.query(User).filter(User.email == payload.owner_email).first()
    if not owner:
        owner = User(
            email=payload.owner_email,
            display_name=payload.owner_email.split("@")[0],
            password_hash="",
            api_key=generate_api_key(),
            trust_level=settings.NEW_USER_TRUST_LEVEL,
            device_ids=[],
        )
        db.add(owner)
        db.commit()
        db.refresh(owner)

    # Bulk update
    objs = db.query(StorageObject).filter(
        StorageObject.link_id == payload.link_id,
        StorageObject.tenant_id == tenant_id
    ).all()
    if not objs:
        return {"updated": 0}

    for obj in objs:
        obj.owner_user_id = owner.id
    db.commit()

    return {"updated": len(objs), "owner_user_id": owner.id}


@router.put("/files/{object_id}", response_model=StorageObjectResponse)
async def replace_file(
    object_id: int,
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant_id: str = Depends(get_tenant_id),
):
    obj = db.query(StorageObject).filter(
        StorageObject.id == object_id,
        StorageObject.tenant_id == tenant_id
    ).first()
    if not obj or (obj.owner_user_id != current_user.id and current_user.trust_level != "admin"):
        raise HTTPException(status_code=404, detail="Not found or forbidden")

    data = await file.read()
    updated_obj = await update_file_and_record(db, storage_obj=obj, data=data)
    updated_obj.original_filename = file.filename or updated_obj.original_filename

    # File content has changed → existing safety verdict is no longer valid.
    # Reset to "pending" so the quarantine logic treats the asset as untrusted
    # until the freshly-enqueued analysis completes. Without this, a caller
    # could upload a safe image, get past safety, then PUT-replace with unsafe.
    updated_obj.ai_safety_rating = None
    updated_obj.ai_safety_status = "pending"
    updated_obj.safety_info = None
    updated_obj.ai_danger_potential = None
    db.commit()
    db.refresh(updated_obj)

    # Re-enqueue AI safety + transcoding for the new bytes. Default ai_mode is
    # 'safety' (cheap haiku check) — callers wanting deeper analysis must use
    # /storage/objects/{id}/analyze explicitly afterwards.
    try:
        from storage.service import enqueue_ai_safety_and_transcoding
        await enqueue_ai_safety_and_transcoding(updated_obj, db=db, ai_mode="safety")
    except Exception as e:
        print(f"⚠️ PUT /files re-enqueue failed for {updated_obj.id}: {e}")

    # Auto-trigger AI analysis for CSV files to enable differential updates
    if updated_obj.mime_type and ("csv" in updated_obj.mime_type.lower() or "text/plain" in updated_obj.mime_type.lower()):
        try:
            print(f"🔄 Auto-triggering differential update for CSV file: {updated_obj.id}")

            # Run AI analysis
            analysis_result = await analyze_content(data, updated_obj.mime_type, context=None)

            # Save AI analysis results to database
            updated_obj.ai_category = analysis_result.get("category")
            updated_obj.ai_danger_potential = analysis_result.get("danger_potential")
            if "safety_info" in analysis_result:
                updated_obj.safety_info = analysis_result.get("safety_info")
                safety_info = analysis_result.get("safety_info", {})
                updated_obj.ai_safety_rating = "safe" if safety_info.get("isSafe", True) else "unsafe"
            updated_obj.ai_title = analysis_result.get("ai_title")
            updated_obj.ai_subtitle = analysis_result.get("ai_subtitle")
            updated_obj.ai_tags = analysis_result.get("ai_tags", [])
            updated_obj.ai_collections = analysis_result.get("ai_collections", [])

            # Store extracted tags and embedding info for Knowledge Graph
            if "extracted_tags" in analysis_result or "embedding_info" in analysis_result:
                context_meta = updated_obj.ai_context_metadata.copy() if updated_obj.ai_context_metadata else {}
                context_meta["extracted_tags"] = analysis_result.get("extracted_tags", {})
                context_meta["embedding_info"] = analysis_result.get("embedding_info", {})
                context_meta["mode"] = analysis_result.get("mode", "")
                context_meta["prompt"] = analysis_result.get("prompt", "")
                context_meta["response"] = analysis_result.get("ai_response", "")
                updated_obj.ai_context_metadata = context_meta

            db.commit()
            db.refresh(updated_obj)

            # Trigger Knowledge Graph Pipeline (handles differential update)
            from knowledge_graph.pipeline import kg_pipeline
            kg_entry = await kg_pipeline.process_storage_object(updated_obj, db)

            if kg_entry:
                print(f"✅ Differential update completed for CSV file: {updated_obj.id}")
            else:
                print(f"⚠️  KG Pipeline returned None for object {updated_obj.id}")

        except Exception as e:
            # Don't fail the upload if analysis fails - just log it
            print(f"❌ Auto-analysis failed (non-fatal): {e}")
            import traceback
            traceback.print_exc()

    _resp = StorageObjectResponse.from_orm(updated_obj)
    _hydrate_storage_urls(_resp, updated_obj, request)
    return _resp

@admin_router.post("/trigger-processing/{object_id}")
async def admin_trigger_processing(
    object_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant_id: str = Depends(get_tenant_id),
):
    """Manually trigger processing for a storage object (admin-only)"""
    if current_user.trust_level != "admin":
        raise HTTPException(status_code=403, detail="Forbidden: Admin access required.")

    storage_obj = db.query(StorageObject).filter(
        StorageObject.id == object_id,
        StorageObject.tenant_id == tenant_id
    ).first()
    if not storage_obj:
        raise HTTPException(status_code=404, detail="Storage object not found")
    
    try:
        from storage.service import enqueue_ai_safety_and_transcoding
        await enqueue_ai_safety_and_transcoding(storage_obj, db)
        return {"message": f"Processing triggered for storage object {object_id}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Processing failed: {str(e)}")

# ============================================================================
# ASYNC KNOWLEDGE GRAPH PIPELINE ENDPOINTS
# ============================================================================

@router.post("/analyze-async/{object_id}")
async def analyze_async(
    object_id: int,
    mode: str = "quality",  # "fast" or "quality"
    ai_tasks: Optional[str] = Query(None, description="CSV/JSON list: safety,vision,product,embedding,kg,notify"),
    ai_vision_mode: Optional[str] = Query(None, description="auto|generic|product"),
    ai_context_role: Optional[str] = Query(None, description="product|lifestyle|doc|other"),
    ai_context_text: Optional[str] = Query(None, description="Optional free-text context for AI"),
    ai_metadata: Optional[str] = Query(None, description="JSON with domain metadata (e.g., brand, model, specs, features)"),
    trim_before_analysis: bool = Query(False, description="If true, crop transparent/empty borders before AI analysis."),
    trim_delivery_default: bool = Query(False, description="If true, prefer delivering trimmed media variants by default."),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    api_key_header: str = Security(_APIKeyHeader(name="X-API-KEY", auto_error=True)),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Trigger async AI analysis and knowledge graph building for a storage object.

    This endpoint starts async processing and returns immediately with a task ID.
    Use GET /storage/tasks/{task_id} to check progress.

    Modes:
    - fast: Single flash model for everything (quick results)
    - quality: Flash for safety check, Pro with thinking for embeddings (best results)
    """
    # Get storage object
    storage_obj = db.query(StorageObject).filter(
        StorageObject.id == object_id,
        StorageObject.tenant_id == tenant_id
    ).first()

    if not storage_obj:
        raise HTTPException(status_code=404, detail="Storage object not found")

    # Check permissions
    if storage_obj.owner_user_id != current_user.id and current_user.trust_level != "admin":
        raise HTTPException(status_code=403, detail="Forbidden")

    # Prepare context overrides (metadata, descriptions, etc.)
    context_overrides: Dict[str, Any] = {}
    metadata_payload: Optional[Dict[str, Any]] = None

    if ai_metadata:
        try:
            metadata_payload = json.loads(ai_metadata)
            if isinstance(metadata_payload, dict):
                context_overrides["metadata"] = metadata_payload
        except Exception:
            metadata_payload = None

    if ai_context_text:
        context_overrides["context_text"] = ai_context_text

    # Persist metadata/context for future synchronous runs
    if context_overrides:
        context_meta = storage_obj.ai_context_metadata.copy() if storage_obj.ai_context_metadata else {}

        if "metadata" in context_overrides and isinstance(context_overrides["metadata"], dict):
            existing_meta = context_meta.get("metadata")
            if isinstance(existing_meta, dict):
                existing_meta.update(context_overrides["metadata"])
            else:
                context_meta["metadata"] = context_overrides["metadata"]

        if "context_text" in context_overrides:
            context_meta["context_text"] = context_overrides["context_text"]

        storage_obj.ai_context_metadata = context_meta
        db.commit()
        db.refresh(storage_obj)

    # Start async task
    from storage.async_pipeline import pipeline_manager

    task_id = await pipeline_manager.start_task(
        object_id=object_id,
        mode=mode,
        db=db,
        ai_tasks_str=ai_tasks,
        vision_mode=ai_vision_mode,
        context_role=ai_context_role,
        context_overrides=context_overrides if context_overrides else None,
        trim_before_analysis=trim_before_analysis,
        trim_delivery_default=trim_delivery_default,
    )

    return {
        "task_id": task_id,
        "object_id": object_id,
        "mode": mode,
        "status": "queued",
        "message": "Processing started. Use GET /storage/tasks/{task_id} to check status."
    }


@router.get("/tasks/{task_id}")
async def get_task_status(
    task_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    api_key_header: str = Security(_APIKeyHeader(name="X-API-KEY", auto_error=True)),
):
    """
    Get status of an async processing task.

    Returns task info including:
    - status: queued, processing, completed, failed
    - progress: 0-100
    - current_phase: which phase is currently running
    - result: final results when completed
    - error: error message if failed
    """
    from storage.async_pipeline import pipeline_manager

    task_info = pipeline_manager.get_task_status(task_id, db)

    if not task_info:
        raise HTTPException(status_code=404, detail="Task not found")

    return task_info


@router.get("/tasks")
async def list_tasks(
    limit: int = 100,
    object_id: int = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    api_key_header: str = Security(_APIKeyHeader(name="X-API-KEY", auto_error=True)),
):
    """
    List all async processing tasks (most recent first).

    Optional query parameters:
    - object_id: Filter tasks for a specific storage object

    Useful for monitoring and debugging.
    """
    from storage.async_pipeline import pipeline_manager

    tasks = pipeline_manager.get_all_tasks(db, limit=limit, object_id=object_id)

    return {
        "tasks": tasks,
        "count": len(tasks)
    }




@router.get("/objects/{object_id}/embedding-text")
async def get_embedding_text(
    object_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant_id: str = Depends(get_tenant_id)
):
    """Get the current embedding text for a storage object."""
    obj = db.query(StorageObject).filter(
        StorageObject.id == object_id,
        StorageObject.tenant_id == tenant_id
    ).first()
    
    if not obj:
        raise HTTPException(status_code=404, detail="Object not found")
    
    metadata = obj.ai_context_metadata or {}
    embedding_info = metadata.get("embedding_info", {})
    embedding_text = embedding_info.get("embeddingText", "")
    searchable_fields = embedding_info.get("searchableFields", [])
    
    return {
        "object_id": obj.id,
        "title": obj.title,
        "embedding_text": embedding_text,
        "searchable_fields": searchable_fields,
        "char_count": len(embedding_text)
    }


@router.get("/objects/{object_id}/annotations")
async def get_object_annotations(
    object_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant_id: str = Depends(get_tenant_id)
):
    """Return product annotations (anchor points) for an object if available."""
    obj = db.query(StorageObject).filter(
        StorageObject.id == object_id,
        StorageObject.tenant_id == tenant_id
    ).first()

    if not obj:
        raise HTTPException(status_code=404, detail="Object not found")

    if obj.owner_user_id != current_user.id and current_user.trust_level != "admin":
        raise HTTPException(status_code=403, detail="Forbidden")

    metadata = obj.ai_context_metadata or {}
    embedding_info = metadata.get("embedding_info", {}) or {}
    meta = embedding_info.get("metadata", {}) or {}

    annotations = meta.get("annotations", [])
    image_space = meta.get("imageSpace", "relative")
    vision_mode = meta.get("vision_mode", None)

    return {
        "object_id": object_id,
        "annotations": annotations,
        "imageSpace": image_space,
        "vision_mode": vision_mode,
        "updated_at": obj.updated_at.isoformat() if obj.updated_at else None
    }


@router.put("/objects/{object_id}/embedding-text")
async def update_embedding_text(
    object_id: int,
    request: dict,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant_id: str = Depends(get_tenant_id)
):
    """
    Update the embedding text and regenerate the Knowledge Graph embedding.
    
    Request body:
    {
        "embedding_text": "New descriptive text for semantic search..."
    }
    """
    from knowledge_graph.embedding_service import embedding_service
    from knowledge_graph.vector_store import get_vector_store
    from knowledge_graph.models import EmbeddingVector

    obj = db.query(StorageObject).filter(
        StorageObject.id == object_id,
        StorageObject.tenant_id == tenant_id
    ).first()

    if not obj:
        raise HTTPException(status_code=404, detail="Object not found")

    new_text = request.get("embedding_text", "").strip()

    if not new_text:
        raise HTTPException(status_code=400, detail="embedding_text cannot be empty")

    # Update embedding text in ai_context_metadata
    if obj.ai_context_metadata is None:
        obj.ai_context_metadata = {}

    if "embedding_info" not in obj.ai_context_metadata:
        obj.ai_context_metadata["embedding_info"] = {}

    old_text = obj.ai_context_metadata.get("embedding_info", {}).get("embeddingText", "")
    obj.ai_context_metadata["embedding_info"]["embeddingText"] = new_text

    # Mark as modified for SQLAlchemy JSONB tracking
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(obj, "ai_context_metadata")

    db.commit()

    # Snapshot plain values for the background task: after db.commit() +
    # request teardown the ORM instance is expired/detached, so attribute
    # access inside the task would raise DetachedInstanceError.
    _obj_id = obj.id
    _obj_title = obj.title
    _obj_category = obj.ai_category
    _obj_tags = obj.ai_tags or []

    # Regenerate embedding in background
    async def regenerate_embedding_task():
        """Background task to regenerate embedding in tenant-specific collection."""
        try:
            # Get tenant-specific vector store
            tenant_vector_store = get_vector_store(tenant_id=tenant_id)

            # Generate new embedding vector
            vector = await embedding_service.generate_embedding(new_text)

            # Create EmbeddingVector model
            embedding = EmbeddingVector(
                object_id=_obj_id,
                vector=vector,
                embedding_text=new_text,
                metadata={
                    "object_id": _obj_id,
                    "tenant_id": tenant_id,
                    "title": _obj_title,
                    "ai_category": _obj_category,
                    "ai_tags": _obj_tags
                }
            )

            # Upsert into tenant-specific Chroma collection
            tenant_vector_store.upsert_embedding(embedding)

            # flush=True: gunicorn buffers stdout — without it, background-task
            # outcomes (incl. the ❌ path) never surface in the journal, which is
            # how the storage_object_id TypeError stayed invisible for months.
            print(f"✅ [Embedding Update] Object {_obj_id}: Updated {len(vector)}-dim vector in Knowledge Graph", flush=True)
        except Exception as e:
            print(f"❌ [Embedding Update] Object {_obj_id}: Failed to regenerate - {e}", flush=True)
            import traceback
            traceback.print_exc()
    
    background_tasks.add_task(regenerate_embedding_task)
    
    return {
        "object_id": obj.id,
        "status": "updated",
        "old_text_length": len(old_text),
        "new_text_length": len(new_text),
        "embedding_status": "regenerating_in_background",
        "message": "Embedding text updated. Knowledge Graph embedding is being regenerated."
    }


@router.get("/objects/{object_id}/public", response_model=StorageObjectResponse)
def get_public_object(
    object_id: int,
    db: Session = Depends(get_db),
):
    """
    Get public storage object WITHOUT tenant filtering.
    
    This endpoint is for public proxies and does NOT enforce tenant isolation.
    Only returns objects where is_public=True.
    
    Use this for:
    - Public proxy access (share.arkturian.com/proxy.php)
    - Public embed links
    - Open API access
    """
    obj = db.query(StorageObject).filter(
        StorageObject.id == object_id,
        StorageObject.is_public == True  # Only public objects
    ).first()
    
    if not obj:
        raise HTTPException(status_code=404, detail="Public object not found")
    
    return obj



@router.get("/objects/{object_id}/data-uri")
async def get_object_data_uri(
    object_id: int,
    max_size: int = 1024 * 1024,  # 1MB default limit
    db: Session = Depends(get_db),
):
    """
    Get storage object image as Base64 data URI.
    
    Returns the image encoded as a data URI (data:image/png;base64,...)
    that can be embedded directly in HTML/JSON without CSP issues.
    
    Args:
        object_id: Storage object ID
        max_size: Max file size in bytes (default 1MB)
    
    Returns:
        {"data_uri": "data:image/png;base64,...", "size": 12345, "mime_type": "image/png"}
    """
    import httpx
    import base64
    from pathlib import Path
    
    # Get object metadata
    obj = db.query(StorageObject).filter(
        StorageObject.id == object_id,
        StorageObject.is_public == True
    ).first()

    if not obj:
        raise HTTPException(status_code=404, detail="Public object not found")

    # Anonymous-only endpoint (no auth) → strict policy: even pending pubs blocked
    _check_quarantine(obj, current_user=None)
    
    if not obj.external_uri:
        raise HTTPException(status_code=400, detail="Object has no external URI")
    
    # Check cache first
    cache_dir = Path("/tmp/share_proxy_cache")
    cache_file = cache_dir / f"obj_{object_id}"
    meta_file = cache_dir / f"obj_{object_id}.meta"
    
    image_data = None
    mime_type = obj.mime_type or "image/png"
    
    # Try cache
    if cache_file.exists() and meta_file.exists():
        import json
        import time
        
        age = time.time() - cache_file.stat().st_mtime
        if age < 86400:  # 24h TTL
            image_data = cache_file.read_bytes()
            meta = json.loads(meta_file.read_text())
            mime_type = meta.get("mime_type", mime_type)
    
    # Fetch from external URI if not cached
    if not image_data:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(obj.external_uri)
            if not response.is_success:
                raise HTTPException(
                    status_code=502,
                    detail=f"Failed to fetch external image: HTTP {response.status_code}"
                )
            
            image_data = response.content
            
            # Detect MIME type from content
            if image_data.startswith(b'\x89PNG'):
                mime_type = "image/png"
            elif image_data.startswith(b'\xff\xd8\xff'):
                mime_type = "image/jpeg"
            elif image_data.startswith(b'GIF89a') or image_data.startswith(b'GIF87a'):
                mime_type = "image/gif"
            elif image_data.startswith(b'WEBP', 8):
                mime_type = "image/webp"
    
    # Check size limit
    if len(image_data) > max_size:
        raise HTTPException(
            status_code=413,
            detail=f"Image too large ({len(image_data)} bytes > {max_size} bytes)"
        )
    
    # Encode as Base64
    base64_data = base64.b64encode(image_data).decode('ascii')
    data_uri = f"data:{mime_type};base64,{base64_data}"
    
    return {
        "data_uri": data_uri,
        "size": len(image_data),
        "mime_type": mime_type,
        "object_id": object_id
    }


@router.post("/transcode/callback")
async def transcoding_callback(
    job_id: str = Body(...),
    status: str = Body(...),
    storage_object_id: int = Body(...),
    output_dir: Optional[str] = Body(None),
    error: Optional[str] = Body(None),
    db: Session = Depends(get_db)
):
    """
    Callback endpoint for transcoding API to report job completion
    """
    print(f"📨 Transcoding callback: job_id={job_id}, status={status}, storage_object_id={storage_object_id}, output_dir={output_dir}")
    
    try:
        # Get storage object
        storage_obj = db.query(StorageObject).filter(StorageObject.id == storage_object_id).first()
        
        if not storage_obj:
            print(f"⚠️  Storage object {storage_object_id} not found")
            raise HTTPException(status_code=404, detail="Storage object not found")
        
        # Update transcoding status
        if status == "completed":
            storage_obj.transcoding_status = "completed"
            storage_obj.transcoding_progress = 100
            storage_obj.transcoding_error = None

            # Copy HLS files from temp directory to storage and set hls_url
            if output_dir:
                try:
                    from pathlib import Path
                    import shutil
                    from sqlalchemy.orm.attributes import flag_modified
                    from storage.service import generic_storage

                    # Get the storage file path and create HLS directory
                    storage_file_path = generic_storage.absolute_path_for_key(
                        storage_obj.object_key,
                        storage_obj.tenant_id
                    )
                    original_basename = Path(storage_obj.object_key).stem
                    hls_dir = storage_file_path.parent / original_basename

                    # Copy HLS files from temp dir to storage
                    temp_output = Path(output_dir)
                    if temp_output.exists():
                        print(f"📁 Copying HLS files from {temp_output} to {hls_dir}")

                        # Remove old HLS directory if exists
                        if hls_dir.exists():
                            shutil.rmtree(hls_dir)

                        # Copy all files
                        shutil.copytree(temp_output, hls_dir)
                        print(f"✅ Copied {len(list(hls_dir.glob('*')))} files to {hls_dir}")

                        # Set HLS URL
                        tenant_id = storage_obj.tenant_id or "arkturian"
                        hls_url = build_hls_url(tenant_id, original_basename)
                        storage_obj.hls_url = hls_url
                        print(f"🎬 HLS URL set: {hls_url}")

                        # Clean up temp directory
                        try:
                            shutil.rmtree(temp_output.parent)
                            print(f"🧹 Cleaned up temp dir: {temp_output.parent}")
                        except Exception as cleanup_error:
                            print(f"⚠️  Could not cleanup temp dir: {cleanup_error}")
                    else:
                        print(f"⚠️  Output directory not found: {output_dir}")

                    # Save output_dir in metadata for reference
                    if not storage_obj.metadata_json:
                        storage_obj.metadata_json = {}
                    storage_obj.metadata_json["transcoding_output_dir"] = output_dir
                    flag_modified(storage_obj, "metadata_json")

                except Exception as copy_error:
                    print(f"❌ Error copying HLS files: {copy_error}")
                    import traceback
                    traceback.print_exc()

            print(f"✅ Transcoding completed for storage object {storage_object_id}")
            
        elif status == "failed":
            storage_obj.transcoding_status = "failed"
            storage_obj.transcoding_error = error or "Unknown error"
            print(f"❌ Transcoding failed for storage object {storage_object_id}: {error}")
            
        else:
            storage_obj.transcoding_status = status
            print(f"🔄 Transcoding status updated: {status}")
        
        db.commit()
        
        return {
            "success": True,
            "message": f"Status updated to {status}",
            "storage_object_id": storage_object_id
        }
        
    except Exception as e:
        print(f"❌ Callback error: {str(e)}")
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
