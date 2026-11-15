"""
URI Handler für CSV/Excel Import

Intelligente Verarbeitung von Bild-URIs in strukturierten Daten:
- Erkennung von URI-Spalten
- Verifikation ob Bild erreichbar
- Auswahl der besten URI bei mehreren Optionen
- Erstellung von Storage-Objekten für externe Bilder
"""

import httpx
import re
from typing import List, Dict, Optional, Tuple
from urllib.parse import urlparse


# Common image URI column names
URI_COLUMN_PATTERNS = [
    r'.*image.*url.*',
    r'.*img.*url.*',
    r'.*picture.*url.*',
    r'.*photo.*url.*',
    r'.*thumbnail.*',
    r'.*preview.*url.*',
    r'.*media.*url.*',
    r'.*src.*',
    r'.*href.*',
]


def is_valid_url(url: str) -> bool:
    """Check if string looks like a valid URL"""
    if not url or not isinstance(url, str):
        return False

    try:
        result = urlparse(url.strip())
        return all([result.scheme in ['http', 'https'], result.netloc])
    except:
        return False


def detect_uri_columns(headers: List[str]) -> List[str]:
    """
    Detect columns that likely contain image URIs.

    Args:
        headers: List of column names from CSV/Excel

    Returns:
        List of column names that appear to contain image URIs
    """
    uri_columns = []

    for header in headers:
        header_lower = header.lower().strip()

        # Check against patterns
        for pattern in URI_COLUMN_PATTERNS:
            if re.match(pattern, header_lower, re.IGNORECASE):
                uri_columns.append(header)
                break

    return uri_columns


async def verify_image_uri(uri: str, timeout: int = 10) -> Tuple[bool, Optional[Dict]]:
    """
    Verify if image URI is accessible and get metadata.

    Args:
        uri: Image URL to verify
        timeout: Request timeout in seconds

    Returns:
        Tuple of (is_valid, metadata_dict)
        metadata includes: content_type, content_length, status_code
    """
    if not is_valid_url(uri):
        return False, None

    try:
        from storage.safe_fetch import safe_async_client  # #3032: guarded, every redirect hop
        async with safe_async_client() as client:
            # Use HEAD request to avoid downloading full image
            response = await client.head(uri, timeout=timeout)

            if response.status_code == 200:
                content_type = response.headers.get('content-type', '').lower()

                # Check if it's actually an image
                if not content_type.startswith('image/'):
                    return False, None

                metadata = {
                    'content_type': content_type,
                    'content_length': int(response.headers.get('content-length', 0)),
                    'status_code': response.status_code,
                    'url': str(response.url),  # Final URL after redirects
                }

                return True, metadata
            else:
                return False, {'status_code': response.status_code, 'url': uri}

    except httpx.TimeoutException:
        print(f"⏰ URI verification timeout: {uri}")
        return False, {'error': 'timeout'}
    except Exception as e:
        print(f"❌ URI verification failed: {uri} - {e}")
        return False, {'error': str(e)}


async def select_best_uri(uris: List[str]) -> Optional[str]:
    """
    Select the best URI from multiple options.

    Criteria:
    1. Must be accessible (verifiable)
    2. Prefer larger file size (better quality)
    3. Prefer HTTPS over HTTP
    4. Prefer common image formats (jpg, png, webp)

    Args:
        uris: List of image URIs to choose from

    Returns:
        Best URI, or None if none are valid
    """
    if not uris:
        return None

    # Filter valid URLs
    valid_uris = [uri for uri in uris if is_valid_url(uri)]
    if not valid_uris:
        return None

    # Verify all URIs and collect metadata
    candidates = []

    for uri in valid_uris:
        is_valid, metadata = await verify_image_uri(uri)
        if is_valid and metadata:
            score = 0

            # Scoring criteria
            # 1. HTTPS preferred
            if uri.startswith('https://'):
                score += 100

            # 2. File size (larger = better quality, max bonus 1000)
            content_length = metadata.get('content_length', 0)
            score += min(content_length // 1000, 1000)  # KB-based score

            # 3. Prefer certain formats
            content_type = metadata.get('content_type', '')
            if 'png' in content_type:
                score += 50  # PNG often higher quality
            elif 'webp' in content_type:
                score += 30
            elif 'jpeg' in content_type or 'jpg' in content_type:
                score += 20

            candidates.append({
                'uri': uri,
                'metadata': metadata,
                'score': score
            })

    if not candidates:
        return None

    # Sort by score and return best
    candidates.sort(key=lambda x: x['score'], reverse=True)
    best = candidates[0]

    print(f"✅ Selected best URI: {best['uri']} (score: {best['score']})")
    return best['uri']


async def extract_uris_from_row(row: Dict[str, str], uri_columns: List[str]) -> List[str]:
    """
    Extract all URIs from a CSV/Excel row.

    Args:
        row: Dictionary of column_name: value
        uri_columns: List of columns that contain URIs

    Returns:
        List of valid URIs found in the row
    """
    uris = []

    for col in uri_columns:
        value = row.get(col, '').strip()
        if is_valid_url(value):
            uris.append(value)

    return uris
