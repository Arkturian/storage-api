"""Upload-contract helpers shared by the Storage API entry points."""

from pathlib import Path
from typing import Optional


_HLS_ARCHIVE_MIME_TYPES = {
    "application/zip",
    "application/x-zip-compressed",
}


def is_hls_result_archive(
    requested: bool,
    content_type: Optional[str],
    filename: Optional[str],
) -> bool:
    """Return whether an upload can enter the HLS-result extraction path.

    ``hls_result`` describes a transcoder-produced ZIP archive. A source MP4
    must stay in the normal ingestion path even if a legacy client sends the
    flag incorrectly.
    """

    if not requested:
        return False

    normalized_content_type = (content_type or "").split(";", 1)[0].strip().lower()
    suffix = Path(filename or "").suffix.lower()
    return normalized_content_type in _HLS_ARCHIVE_MIME_TYPES or suffix == ".zip"
