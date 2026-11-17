"""Serving and auditing encrypted objects (#3230). Route glue lives in routes.py."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from typing import Optional, Tuple

from fastapi import HTTPException
from fastapi.responses import Response, StreamingResponse

from storage import encryption as E

LOG_WINDOW = timedelta(minutes=5)
SIGN_SKEW_SECONDS = 30

# Query parameters that ask for a derived rendering. Encrypted objects have
# none: no thumbnail, no transcode, no format change, no GLB optimisation.
TRANSFORM_PARAMS = ("display_for", "width", "height", "aspect_ratio", "format", "quality", "bitrate",
                    "sample_rate", "channels", "trim", "margin", "bg_color", "decimate", "texture_format",
                    "texture_quality", "texture_max_size", "mesh_compression", "output", "preset")


def reject_transforms(variant: Optional[str], params: dict) -> None:
    asked = [k for k in TRANSFORM_PARAMS if params.get(k) not in (None, False)]
    if variant not in (None, "", "full", "original"):
        asked.insert(0, "variant")
    if asked:
        raise HTTPException(status_code=409, detail={
            "error": "encrypted objects are served only as the original file",
            "code": "encrypted_no_variants", "params": asked})


def check_signed_ttl(exp: Optional[int]) -> None:
    """A signed link for an encrypted object may live at most 5 minutes."""
    if exp is not None and exp > time.time() + E.SIGNED_TTL_CAP_SECONDS + SIGN_SKEW_SECONDS:
        raise HTTPException(status_code=403, detail={
            "error": "signed links for encrypted objects are valid for at most 5 minutes",
            "code": "encrypted_signature_too_long"})


def parse_range(header: Optional[str], size: int) -> Optional[Tuple[int, int]]:
    """Single byte range -> (start, end) inclusive. None = whole file.
    Raises 416 for an unsatisfiable range; multi-range requests are answered
    with the whole file (allowed by RFC 9110)."""
    if not header:
        return None
    h = header.strip().lower()
    if not h.startswith("bytes=") or "," in h:
        return None
    spec = h[6:].strip()
    try:
        a, b = spec.split("-", 1)
        if a == "":
            n = int(b)
            if n <= 0:
                raise ValueError
            start, end = max(0, size - n), size - 1
        else:
            start = int(a)
            end = int(b) if b else size - 1
    except ValueError:
        return None
    if size == 0 or start >= size or start > end:
        raise HTTPException(status_code=416, detail={"error": "range not satisfiable", "code": "range_not_satisfiable"},
                            headers={"Content-Range": f"bytes */{size}"})
    return start, min(end, size - 1)


def principal_of(current_user, signed: bool, kid: Optional[str]) -> str:
    from storage import grant_restrict
    scope = grant_restrict.current_scope()
    if signed:
        return f"signed:{kid or '?'}" + (f"|{scope}" if scope else "")
    email = getattr(current_user, "email", None) or f"user:{getattr(current_user, 'id', '?')}"
    # #3230: name the key, not only the account — system@api is shared by many
    # services ("system@api[service:mcp-gateway]", "...[master]").
    via = getattr(current_user, "_storage_auth_via", None)
    return email + (f"[{via}]" if via else "") + (f"|{scope}" if scope else "")


def emit_event(kind: str, payload: dict) -> None:
    """storage.encrypted_read / storage.encrypted_share on the bus, so Cloud and
    Comm can tell the owner. Optional (STORAGE_EVENTS_REDIS_URL) and never
    blocks or breaks a request. Payload carries ids only, never a name."""
    url = os.getenv("STORAGE_EVENTS_REDIS_URL", "").strip()
    if not url:
        return
    try:
        import redis
        r = redis.Redis.from_url(url, socket_timeout=1, socket_connect_timeout=1)
        r.publish(f"swfme.webhook.storage.{kind}", json.dumps({"event": f"storage.{kind}", **payload,
                                                              "ts": datetime.utcnow().isoformat() + "Z"}))
    except Exception:
        pass


def log_decrypt(db, obj, principal: str, via: str, ip: Optional[str], rng: Optional[Tuple[int, int]]) -> None:
    """One row per (object, principal, via) and 5-minute window; a video
    player's burst of Range requests becomes a single line."""
    from models import StorageDecryptLog
    now = datetime.utcnow()
    lo = rng[0] if rng else 0
    hi = rng[1] if rng else max((obj.plain_size or 0) - 1, 0)
    row = (db.query(StorageDecryptLog)
           .filter(StorageDecryptLog.object_id == obj.id, StorageDecryptLog.principal == principal,
                   StorageDecryptLog.via == via, StorageDecryptLog.last_at >= now - LOG_WINDOW)
           .order_by(StorageDecryptLog.id.desc()).first())
    if row:
        row.last_at = now
        row.requests = (row.requests or 0) + 1
        row.range_min = min(row.range_min if row.range_min is not None else lo, lo)
        row.range_max = max(row.range_max if row.range_max is not None else hi, hi)
    else:
        db.add(StorageDecryptLog(object_id=obj.id, principal=principal, via=via, ip=ip,
                                 first_at=now, last_at=now, requests=1, range_min=lo, range_max=hi))
        db.commit()
        from storage import grant_restrict
        emit_event("encrypted_read", {"object_id": obj.id, "owner_user_id": obj.owner_user_id,
                                      "tenant_id": obj.tenant_id, "principal": principal, "via": via,
                                      "scope": grant_restrict.current_scope()})
        return
    db.commit()


def log_sign(db, obj, signer: str, scope: str, ttl: int) -> None:
    """#3230: a signed link carries no identity, so the read itself only shows
    `signed:<kid>`. Who asked is known when the link is minted: one log row
    per (object, signer, scope) and window, plus an `encrypted_sign` event
    (cloud-api tells the owner which agent opened the file)."""
    from models import StorageDecryptLog
    now = datetime.utcnow()
    principal = f"sign:{signer}|{scope}"
    row = (db.query(StorageDecryptLog)
           .filter(StorageDecryptLog.object_id == obj.id, StorageDecryptLog.principal == principal,
                   StorageDecryptLog.via == "sign", StorageDecryptLog.last_at >= now - LOG_WINDOW)
           .order_by(StorageDecryptLog.id.desc()).first())
    if row:
        row.last_at = now
        row.requests = (row.requests or 0) + 1
        db.commit()
        return
    db.add(StorageDecryptLog(object_id=obj.id, principal=principal, via="sign", ip=None,
                             first_at=now, last_at=now, requests=1, range_min=None, range_max=None))
    db.commit()
    emit_event("encrypted_sign", {"object_id": obj.id, "owner_user_id": obj.owner_user_id,
                                  "tenant_id": obj.tenant_id, "scope": scope, "signer": signer, "ttl": ttl})


def lazy_rewrap(db, obj, rewrapped: Optional[bytes]) -> None:
    """The KMS re-encrypts under its newest key version on decrypt; store it."""
    if rewrapped and rewrapped != obj.enc_wrapped_dek:
        obj.enc_wrapped_dek = rewrapped
        db.commit()


def real_filename(obj, dek: bytes) -> str:
    if not obj.enc_filename:
        return ""
    return E.decrypt_filename(obj.enc_filename, dek, object_key=obj.object_key, tenant=obj.tenant_id or "arkturian")


def etag_for(obj) -> str:
    return f'"enc-{(obj.checksum or "")[:32]}"'


def build_response(*, path, obj, dek: bytes, rng: Optional[Tuple[int, int]], disposition: str,
                   head_only: bool = False, if_none_match: Optional[str] = None) -> Response:
    size = int(obj.plain_size or 0)
    etag = etag_for(obj)
    headers = {
        "Accept-Ranges": "bytes",
        "ETag": etag,
        "Cache-Control": "private, no-store",
        "Content-Disposition": disposition,
        "X-Content-Type-Options": "nosniff",
        "X-Encrypted": "1",
    }
    media_type = obj.mime_type or "application/octet-stream"
    if if_none_match and etag in [t.strip() for t in if_none_match.split(",")] and rng is None:
        return Response(status_code=304, headers=headers)
    status = 200
    start, end = 0, size - 1
    if rng is not None:
        start, end = rng
        status = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    headers["Content-Length"] = str(max(end - start + 1, 0))
    if head_only:
        return Response(status_code=status, headers=headers, media_type=media_type)
    tenant = obj.tenant_id or "arkturian"
    key = obj.object_key
    # The first segment is authenticated here, before the status line goes out:
    # a broken container answers 500 instead of a truncated 200.
    gen = E.iter_decrypt(path, dek, object_key=key, tenant=tenant, start=start, end=end,
                         expected_plain_size=size)
    try:
        first = next(gen)
    except StopIteration:
        first = b""

    def _body():
        if first:
            yield first
        for chunk in gen:
            yield chunk
    return StreamingResponse(_body(), status_code=status, headers=headers, media_type=media_type)
