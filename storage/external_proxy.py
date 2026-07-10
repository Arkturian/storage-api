"""
External Storage Proxy with LRU Cache

Transparently proxies external web URIs with configurable caching.
Allows storage objects to reference external files without downloading them.
"""

import httpx
import hashlib
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Dict, Tuple
from functools import lru_cache
import os


# Hard cap on how many bytes we pull into RAM for one external ref. A single
# oversized ref (e.g. a 19 GB ZIP mislabeled as image/jpg in a marketing set)
# must never OOM the process — reject it instead of loading it. Default 100 MB,
# far above any real image; raise EXTERNAL_MAX_FETCH_MB if you truly need bigger.
_MAX_FETCH_BYTES = int(os.getenv("EXTERNAL_MAX_FETCH_MB", "100")) * 1024 * 1024


class ExternalProxyCache:
    """
    LRU Cache for external files with size limits.

    Configuration via environment variables:
    - EXTERNAL_CACHE_DIR: Cache directory (default: /tmp/external_cache)
    - EXTERNAL_CACHE_MAX_SIZE_MB: Max cache size in MB (default: 500)
    - EXTERNAL_CACHE_MAX_FILES: Max number of cached files (default: 1000)
    - EXTERNAL_CACHE_TTL_HOURS: Cache TTL in hours (default: 24)
    """

    def __init__(self):
        self.cache_dir = Path(os.getenv("EXTERNAL_CACHE_DIR", "/tmp/external_cache"))
        self.max_size_bytes = int(os.getenv("EXTERNAL_CACHE_MAX_SIZE_MB", "500")) * 1024 * 1024
        self.max_files = int(os.getenv("EXTERNAL_CACHE_MAX_FILES", "1000"))
        self.ttl_hours = int(os.getenv("EXTERNAL_CACHE_TTL_HOURS", "24"))

        # Create cache directory
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        print(f"📦 External Proxy Cache initialized:")
        print(f"   Directory: {self.cache_dir}")
        print(f"   Max Size: {self.max_size_bytes / 1024 / 1024:.0f} MB")
        print(f"   Max Files: {self.max_files}")
        print(f"   TTL: {self.ttl_hours} hours")

    def _uri_to_cache_key(self, uri: str) -> str:
        """Convert URI to cache filename."""
        return hashlib.sha256(uri.encode()).hexdigest()

    def _get_cache_path(self, uri: str) -> Path:
        """Get cache file path for URI."""
        cache_key = self._uri_to_cache_key(uri)
        return self.cache_dir / cache_key

    def _get_metadata_path(self, uri: str) -> Path:
        """Get metadata file path for cached URI."""
        cache_key = self._uri_to_cache_key(uri)
        return self.cache_dir / f"{cache_key}.meta"

    def _is_cache_valid(self, uri: str) -> bool:
        """Check if cached file is still valid (exists and not expired)."""
        cache_path = self._get_cache_path(uri)
        meta_path = self._get_metadata_path(uri)

        if not cache_path.exists() or not meta_path.exists():
            return False

        # Check TTL
        mtime = datetime.fromtimestamp(cache_path.stat().st_mtime)
        age = datetime.now() - mtime

        if age.total_seconds() > self.ttl_hours * 3600:
            # Expired - delete
            cache_path.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)
            return False

        return True

    def get_from_cache(self, uri: str) -> Optional[Tuple[bytes, Dict[str, str]]]:
        """
        Get file from cache if available and valid.

        Returns:
            Tuple of (file_data, metadata) or None if not cached
        """
        if not self._is_cache_valid(uri):
            return None

        cache_path = self._get_cache_path(uri)
        meta_path = self._get_metadata_path(uri)

        try:
            # Read file data
            with open(cache_path, 'rb') as f:
                data = f.read()

            # Read metadata
            import json
            with open(meta_path, 'r') as f:
                metadata = json.load(f)

            # Update access time for LRU
            cache_path.touch()

            print(f"✅ Cache HIT: {uri[:80]}...")
            return data, metadata

        except Exception as e:
            print(f"⚠️  Cache read error: {e}")
            return None

    def put_in_cache(self, uri: str, data: bytes, metadata: Dict[str, str]) -> bool:
        """
        Store file in cache with metadata.

        Args:
            uri: External URI
            data: File data
            metadata: HTTP headers (content-type, etc.)

        Returns:
            True if cached successfully
        """
        try:
            # Check if we need to evict old files
            self._evict_if_needed(len(data))

            cache_path = self._get_cache_path(uri)
            meta_path = self._get_metadata_path(uri)

            # Write file data
            with open(cache_path, 'wb') as f:
                f.write(data)

            # Write metadata
            import json
            with open(meta_path, 'w') as f:
                json.dump(metadata, f)

            print(f"💾 Cached: {uri[:80]}... ({len(data) / 1024:.0f} KB)")
            return True

        except Exception as e:
            print(f"❌ Cache write error: {e}")
            return False

    def _evict_if_needed(self, new_file_size: int):
        """Evict old files if cache is full."""
        now = datetime.now()
        ttl_seconds = max(self.ttl_hours * 3600, 0)

        def is_old(path: Path) -> bool:
            try:
                age = now.timestamp() - path.stat().st_atime
                return age >= ttl_seconds
            except Exception:
                return False

        cache_files = [f for f in self.cache_dir.glob("*") if f.is_file() and not f.name.endswith('.meta')]

        # Remove stale entries based on TTL/5-day rule
        stale_files = [f for f in cache_files if is_old(f)]
        for stale in stale_files:
            meta_file = self.cache_dir / f"{stale.name}.meta"
            stale.unlink(missing_ok=True)
            meta_file.unlink(missing_ok=True)
        if stale_files:
            print(f"🧹 Cache cleanup: removed {len(stale_files)} stale entries")

        # Calculate current cache size
        total_size = sum(f.stat().st_size for f in cache_files)
        file_count = len(cache_files)

        # Evict oldest files if limits exceeded
        evicted = 0
        while cache_files and (
            total_size + new_file_size > self.max_size_bytes or
            file_count >= self.max_files
        ):
            oldest = cache_files.pop(0)
            meta_file = self.cache_dir / f"{oldest.name}.meta"

            file_size = oldest.stat().st_size
            oldest.unlink(missing_ok=True)
            meta_file.unlink(missing_ok=True)

            total_size -= file_size
            file_count -= 1
            evicted += 1

        if evicted > 0:
            print(f"🗑️  Evicted {evicted} old cache files")

    def get_stats(self) -> Dict:
        """Get cache statistics."""
        cache_files = [f for f in self.cache_dir.glob("*") if f.is_file() and not f.name.endswith('.meta')]
        total_size = sum(f.stat().st_size for f in cache_files)

        return {
            "file_count": len(cache_files),
            "total_size_mb": round(total_size / 1024 / 1024, 2),
            "max_size_mb": round(self.max_size_bytes / 1024 / 1024, 2),
            "max_files": self.max_files,
            "ttl_hours": self.ttl_hours,
            "cache_dir": str(self.cache_dir)
        }


# Global cache instance
external_cache = ExternalProxyCache()


async def fetch_external_file(uri: str, use_cache: bool = True) -> Tuple[bytes, Dict[str, str]]:
    """
    Fetch file from external URI with optional caching.

    Args:
        uri: External web URI
        use_cache: Whether to use cache (default: True)

    Returns:
        Tuple of (file_data, headers)

    Raises:
        httpx.HTTPError: If fetch fails
    """
    # Try cache first
    if use_cache:
        cached = external_cache.get_from_cache(uri)
        if cached:
            return cached

    # Fetch from external source
    print(f"🌐 Fetching external URI: {uri[:80]}...")

    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        # Stream + size-guard: never pull an oversized ref fully into RAM. Reject
        # via Content-Length up front, and hard-stop the stream if a server omits
        # or understates it (2026-07-10: 19 GB ZIPs mislabeled as images OOM-killed
        # uvicorn). Raises ValueError → the analysis pipeline marks the object
        # failed instead of crashing the process.
        async with client.stream("GET", uri) as response:
            response.raise_for_status()

            clen = response.headers.get('content-length')
            if clen and clen.isdigit() and int(clen) > _MAX_FETCH_BYTES:
                raise ValueError(
                    f"External file too large ({int(clen)} bytes > {_MAX_FETCH_BYTES}) "
                    f"— refusing to load into RAM: {uri[:80]}"
                )

            chunks = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > _MAX_FETCH_BYTES:
                    raise ValueError(
                        f"External file exceeded {_MAX_FETCH_BYTES} bytes mid-download "
                        f"— aborting: {uri[:80]}"
                    )
                chunks.append(chunk)
            data = b"".join(chunks)

            metadata = {
                'content-type': response.headers.get('content-type', 'application/octet-stream'),
                'content-length': str(len(data)),
                'etag': response.headers.get('etag', ''),
                'last-modified': response.headers.get('last-modified', ''),
            }

        # Cache the result
        if use_cache:
            external_cache.put_in_cache(uri, data, metadata)

        return data, metadata
