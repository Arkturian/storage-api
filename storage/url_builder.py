"""
Dynamic URL builder for storage objects.

URLs are built dynamically based on the current request context,
not stored in the database. This allows for:
- Easy domain/server changes
- Development vs production flexibility
- No database migrations when URLs change
"""

import os
import re
import unicodedata
from typing import Optional
from urllib.parse import quote

from fastapi import Request

_TRANSLIT = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss"})


def _share_name(original_filename: Optional[str]) -> str:
    """Readable, URL-safe file name for a share link ("" if nothing usable)."""
    name = (original_filename or "").replace("\\", "/").rsplit("/", 1)[-1].translate(_TRANSLIT)
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name)
    name = re.sub(r"-{2,}", "-", name)
    name = re.sub(r"-?\.-?", ".", name).strip("-.")
    if "." not in name and "." in (original_filename or ""):
        return ""  # nothing readable left of the stem (e.g. non-Latin name)
    if len(name) > 120:
        stem, dot, ext = name.rpartition(".")
        name = f"{stem[:120 - len(ext) - 1]}.{ext}" if dot and len(ext) <= 8 else name[:120]
    return name


def build_share_url(object_id: int, original_filename: Optional[str], is_public: bool) -> Optional[str]:
    """Link meant to be handed to people outside: <share host>/<id>/<name>.

    Only for public objects — the share host changes the address, not the
    rights, so a private object would just answer 403 there. Off unless
    STORAGE_SHARE_BASE_URL is set: the host exists per instance, and a
    hard-coded default would hand out links into another instance's DB.
    """
    base = os.getenv("STORAGE_SHARE_BASE_URL", "").strip().rstrip("/")
    if not base or not is_public:
        return None
    name = _share_name(original_filename)
    return f"{base}/{int(object_id)}/{quote(name)}" if name else f"{base}/{int(object_id)}"


def build_storage_urls(
    object_id: int,
    tenant_id: str,
    checksum: Optional[str] = None,
    metadata_json: Optional[dict] = None,
    base_url: Optional[str] = None,
    storage_mode: str = "copy",
    stored_file_url: Optional[str] = None,
    mime_type: Optional[str] = None,
) -> dict:
    """
    Build URLs dynamically for a storage object.

    Args:
        object_id: Storage object ID
        tenant_id: Tenant ID
        checksum: File checksum for cache busting
        metadata_json: Metadata containing thumbnail_filename, webview_filename, etc.
        base_url: Base URL of the API (e.g., "https://api-storage.arkturian.com")
        storage_mode: Storage mode (copy, reference, external)
        stored_file_url: For external mode, use the stored proxy URL
        mime_type: MIME type of the file (e.g., "video/mp4") - used to add format=jpg for videos

    Returns:
        Dict with file_url, thumbnail_url, webview_url
    """
    if not base_url:
        # Fallback to default
        base_url = "https://api-storage.arkturian.com"

    base_url = base_url.rstrip("/")

    # For external mode, use the stored proxy URL if available
    if storage_mode == "external" and stored_file_url:
        file_url = stored_file_url
    else:
        # Main file URL uses the /storage/media/{id} endpoint
        file_url = f"{base_url}/storage/media/{object_id}"
        if checksum:
            file_url = f"{file_url}?v={checksum}"

    # Thumbnail URL - ALWAYS generated on-demand via media endpoint
    # No longer checking for metadata_json.thumbnail_filename - all variants are dynamic
    # For videos, add format=jpg to extract a frame instead of returning the video
    is_video = mime_type and mime_type.lower().startswith("video/")
    thumbnail_url = f"{base_url}/storage/media/{object_id}?variant=thumbnail"
    if is_video:
        thumbnail_url = f"{thumbnail_url}&format=jpg"
    if checksum:
        thumbnail_url = f"{thumbnail_url}&v={checksum}"

    # Webview URL - ALWAYS generated on-demand via media endpoint (medium quality variant)
    # No longer checking for metadata_json.webview_filename - all variants are dynamic
    webview_url = f"{base_url}/storage/media/{object_id}?variant=medium"
    if checksum:
        webview_url = f"{webview_url}&v={checksum}"

    return {
        "file_url": file_url,
        "thumbnail_url": thumbnail_url,
        "webview_url": webview_url,
    }


def get_base_url_from_request(request: Request) -> str:
    """
    Extract base URL from FastAPI request.

    Args:
        request: FastAPI Request object

    Returns:
        Base URL (e.g., "https://api-storage.arkturian.com")
    """
    # Get scheme (http/https)
    scheme = request.url.scheme

    # Get host (includes port if non-standard)
    host = request.headers.get("host") or request.client.host

    # Check for X-Forwarded-* headers (for reverse proxy scenarios)
    forwarded_proto = request.headers.get("x-forwarded-proto")
    forwarded_host = request.headers.get("x-forwarded-host")

    if forwarded_proto:
        scheme = forwarded_proto
    if forwarded_host:
        host = forwarded_host

    # Clean scheme: remove any trailing slashes, backslashes, or colons
    if scheme:
        scheme = scheme.strip().rstrip(":/\\")

    # Clean up host: remove leading/trailing slashes or protocol prefixes
    if host:
        host = host.strip()
        # Remove leading slashes and backslashes
        host = host.lstrip("/\\")
        # Remove protocol if accidentally included
        if host.startswith("http://"):
            host = host[7:]
        elif host.startswith("https://"):
            host = host[8:]
        # Remove trailing slashes and backslashes
        host = host.rstrip("/\\")

    return f"{scheme}://{host}"
