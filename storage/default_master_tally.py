"""Count who still presents the built-in default master key (AP 2.6, #1960).

While API_KEY is unset, config.py falls back to a publicly known literal. Before
that key loses its operator rights (or is rotated away), we need to know which
callers still send it, so nothing in the field breaks unannounced.

The middleware never records the key, a body or a query string. Per request
that presents the default it counts (peer address, client address behind nginx,
User-Agent, method, path with numeric ids collapsed) in memory and appends one
aggregated JSON line per bucket every FLUSH_SECONDS to TALLY_PATH. Read it with
scripts/default_master_report.py.
"""
from __future__ import annotations

import contextvars
import json
import os
import re
import threading
import time
from collections import Counter

DEFAULT_LITERAL = "Inetpass1"
FLUSH_SECONDS = int(os.getenv("STORAGE_DEFAULT_MASTER_TALLY_FLUSH", "300"))
TALLY_PATH = os.getenv("STORAGE_DEFAULT_MASTER_TALLY_PATH", "/var/lib/storage-api/default-master-usage.jsonl")

_ID_RE = re.compile(r"/\d+(?=/|$)")

# Bucket of the current request when it presents the default key; auth sets
# "priv" when is_privileged_admin answered True for it — those are the calls
# that would change once the default key stops being an operator.
_current = contextvars.ContextVar("default_master_bucket", default=None)
_TALLY = None


def note_privilege_use() -> None:
    bucket = _current.get()
    if bucket is not None and _TALLY is not None:
        _TALLY.note(bucket, priv=True)


def _norm_path(path: str) -> str:
    return _ID_RE.sub("/{id}", path or "")[:200]


class DefaultMasterTally:
    def __init__(self, app):
        self.app = app
        self._lock = threading.Lock()
        self._counts: Counter = Counter()
        self._last_flush = time.time()
        global _TALLY
        _TALLY = self

    def note(self, bucket, priv: bool = False) -> None:
        with self._lock:
            self._counts[bucket + (priv,)] += 1

    def _active(self) -> bool:
        try:
            from config import settings
            return settings.API_KEY == DEFAULT_LITERAL
        except Exception:
            return False

    def _flush(self, force: bool = False) -> None:
        now = time.time()
        with self._lock:
            if not self._counts or (not force and now - self._last_flush < FLUSH_SECONDS):
                return
            counts, self._counts = self._counts, Counter()
            since, self._last_flush = self._last_flush, now
        try:
            with open(TALLY_PATH, "a", encoding="utf-8") as fh:
                for (peer, xff, ua, method, path, priv), n in counts.items():
                    fh.write(json.dumps({"since": int(since), "until": int(now), "peer": peer, "xff": xff,
                                         "ua": ua, "method": method, "path": path,
                                         "priv": priv, "n": n}) + "\n")
        except Exception as exc:  # never break a request over bookkeeping
            print(f"⚠️ default-master tally flush failed: {exc}", flush=True)

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and self._active():
            key = None
            ua = xff_roh = real_roh = ""
            for k, v in scope.get("headers") or []:
                lk = k.lower()
                if lk == b"x-api-key":
                    key = v
                elif lk == b"user-agent":
                    ua = v.decode("latin-1")[:120]
                elif lk == b"x-forwarded-for":
                    xff_roh = v.decode("latin-1")
                elif lk == b"x-real-ip":
                    real_roh = v.decode("latin-1")
            if key is not None and key.strip() == DEFAULT_LITERAL.encode():
                peer = (scope.get("client") or ("", 0))[0]
                # Befund 137 (#3072): nicht XFF[0] (vom Client gesetzt), sondern der
                # Eintrag, den ein vertrauter Proxy angehaengt hat. Feldname bleibt
                # "xff" fuer scripts/default_master_report.py.
                from .client_ip import client_ip as _client_ip
                xff = _client_ip(xff_roh, real_roh, peer)[:64]
                bucket = (peer, xff, ua, scope.get("method", ""), _norm_path(scope.get("path", "")))
                self.note(bucket)
                token = _current.set(bucket)
                try:
                    await self.app(scope, receive, send)
                finally:
                    _current.reset(token)
                    self._flush()
                return
        await self.app(scope, receive, send)
