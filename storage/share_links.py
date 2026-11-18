"""Share links with an unguessable token (#3329).

Public objects are served anonymously by id, and ids count up, so "public"
also means "enumerable". A share link is the way to hand a file to people
outside without making it public: the object stays private, the link
carries 192 bits of randomness, and only its SHA-256 is stored.

    POST   /storage/objects/{id}/share-link              -> {url, id, token_prefix, gueltig_bis}
    GET    /storage/objects/{id}/share-links             -> list (no tokens)
    DELETE /storage/objects/{id}/share-links/{link_id}   -> revoke
    GET    /storage/s/{token}[/{name}]                   -> 302 to a 120 s signed URL

Who may create: the owner (service key + X-On-Behalf-Of) or a real admin,
as for grants; or, for an agent, a key flagged may_create_share_links whose
sign_tenants lists the object's tenant, naming the agent's scope, which must
be bound to the object by the agent's own upload (not a share). The caller
(cloud-api) has verified the agent behind that scope. Encrypted objects are
refused in v1. Revoking is effective within the signature lifetime (120 s).
"""
import hashlib
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote, urlencode, urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from auth import get_current_user
from database import get_db
from models import MediaGrant, ShareLink, StorageObject, User
from storage import abuse_check as _abuse
from storage import routes as R
from storage.url_builder import _share_name

router = APIRouter()

_LOG_TOKEN_RE = re.compile(r"(/storage/s/)([A-Za-z0-9_-]{6})[A-Za-z0-9_-]*")


class _RedactShareTokens(logging.Filter):
    """uvicorn's access log prints the path; keep only the token prefix."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.args, tuple) and len(record.args) >= 3 and "/storage/s/" in str(record.args[2]):
                args = list(record.args)
                args[2] = _LOG_TOKEN_RE.sub(r"\1\2…", str(args[2]))
                record.args = tuple(args)
        except Exception:
            pass
        return True


logging.getLogger("uvicorn.access").addFilter(_RedactShareTokens())

TOKEN_BYTES = 24                      # 192 bit
DEFAULT_DAYS = 30
MAX_DAYS = 365
REDIRECT_TTL = 120                    # seconds; also the revoke latency
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
_AGENT_PREFIXES = ("cloud-session:", "cloud-session-x:")
_NO_LEAK = {"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer"}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ShareLinkCreate(BaseModel):
    gueltig_bis: Optional[datetime] = None
    scope: Optional[str] = None  # agent path: the agent's own scope


def _agent_authority(obj, request: Request, db: Session, scope: str) -> str:
    if request.headers.get("X-On-Behalf-Of"):
        raise HTTPException(status_code=400, detail={"error": "an agent's share link is the service's own act; drop X-On-Behalf-Of", "code": "on_behalf_not_allowed"})
    row = R._presented_tenant_key(request, db)
    tenants = {t.strip() for t in ((row.sign_tenants if row else None) or "").split(",") if t.strip()}
    if not row or obj.tenant_id not in tenants or not getattr(row, "may_create_share_links", False):
        raise HTTPException(status_code=403, detail={"error": "this key may not create share links for this tenant", "code": "share_link_not_allowed"})
    R._refuse_scoped_key_for_grant_management(row)
    scope = R._validate_scope(scope)
    if not scope.startswith(_AGENT_PREFIXES):
        raise HTTPException(status_code=422, detail={"error": "scope must be an agent scope", "code": "invalid_scope"})
    g = db.query(MediaGrant.granted_via).filter(MediaGrant.object_id == obj.id, MediaGrant.scope == scope).first()
    if g is None:
        raise HTTPException(status_code=403, detail={"error": "scope is not bound to this object", "code": "not_granted"})
    if (g[0] or "").startswith((R._SHARE_VIA, R._SHARE_RO_VIA)):
        raise HTTPException(status_code=403, detail={"error": "only the agent's own files may get a share link", "code": "not_own_file"})
    return f"agent:{scope}"


def _authority(obj, current_user, request: Request, db: Session, scope: Optional[str]) -> str:
    if scope:
        return _agent_authority(obj, request, db, scope)
    return R._require_grant_authority(obj, current_user, request, db)


def _share_base(request: Request) -> str:
    base = os.getenv("STORAGE_SHARE_BASE_URL", "").strip().rstrip("/")
    if base:
        return f"{base}/s"
    return f"{str(request.base_url).rstrip('/')}/storage/s"


def _row_out(r: ShareLink) -> dict:
    now = _utcnow()
    return {"id": r.id, "token_prefix": r.token_prefix, "created_by": r.created_by,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "gueltig_bis": r.expires_at.isoformat() if r.expires_at else None,
            "revoked_at": r.revoked_at.isoformat() if r.revoked_at else None,
            "aktiv": r.revoked_at is None and r.expires_at > now}


@router.post("/objects/{object_id}/share-link")
async def create_share_link(
    object_id: int,
    payload: ShareLinkCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    obj = R._grant_object(object_id, db)
    who = _authority(obj, current_user, request, db, payload.scope)
    if getattr(obj, "encrypted", False):
        raise HTTPException(status_code=403, detail={"error": "encrypted objects cannot get a share link", "code": "encrypted_share_link_not_allowed"})
    # #3329 (c): images/videos leave only after a clean abuse check. The link
    # is created either way; until "clear" it answers 404 (fail-closed).
    pruefung = await _abuse.ensure_for_share(db, obj)
    if pruefung == "hit":
        raise HTTPException(status_code=451, detail={"error": "Content blocked pending review", "code": "abuse_blocked"})
    if pruefung == "held":
        raise HTTPException(status_code=403, detail={"error": "this file may not be shared outside", "code": "abuse_held"})
    now = _utcnow()
    until = payload.gueltig_bis
    if until is not None and until.tzinfo is not None:
        until = until.astimezone(timezone.utc).replace(tzinfo=None)
    until = until or now + timedelta(days=DEFAULT_DAYS)
    if until <= now or until > now + timedelta(days=MAX_DAYS, minutes=5):
        raise HTTPException(status_code=422, detail={"error": f"gueltig_bis must lie between now and {MAX_DAYS} days ahead", "code": "invalid_gueltig_bis"})
    token = secrets.token_urlsafe(TOKEN_BYTES)
    row = ShareLink(token_hash=_hash(token), token_prefix=token[:6], object_id=obj.id, tenant_id=obj.tenant_id,
                    created_by=who, created_at=now, expires_at=until)
    db.add(row)
    db.commit()
    name = _share_name(obj.original_filename)
    url = f"{_share_base(request)}/{token}" + (f"/{quote(name)}" if name else "")
    print(f"🔗 share-link create id={row.id} prefix={row.token_prefix} object={obj.id} by={who} until={until.isoformat()}", flush=True)
    out = {**_row_out(row), "object_id": obj.id, "url": url}
    if pruefung is not None:
        out["pruefung"] = pruefung  # clear | pending | failed (link opens only after clear)
    return out


@router.get("/objects/{object_id}/share-links")
def list_share_links(
    object_id: int,
    request: Request,
    scope: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    obj = R._grant_object(object_id, db)
    who = _authority(obj, current_user, request, db, scope)
    q = db.query(ShareLink).filter(ShareLink.object_id == obj.id)
    if who.startswith("agent:"):
        q = q.filter(ShareLink.created_by == who)  # an agent sees only its own links
    return {"object_id": obj.id, "share_links": [_row_out(r) for r in q.order_by(ShareLink.id)]}


@router.delete("/objects/{object_id}/share-links/{link_id}")
def revoke_share_link(
    object_id: int,
    link_id: int,
    request: Request,
    scope: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    obj = R._grant_object(object_id, db)
    who = _authority(obj, current_user, request, db, scope)
    row = db.query(ShareLink).filter(ShareLink.id == link_id, ShareLink.object_id == obj.id).first()
    if row is None or (who.startswith("agent:") and row.created_by != who):
        raise HTTPException(status_code=404, detail="Not found")
    already = row.revoked_at is not None
    if not already:
        row.revoked_at = _utcnow()
        row.revoked_by = who
        db.commit()
    print(f"🔗 share-link revoke id={row.id} prefix={row.token_prefix} object={obj.id} by={who}", flush=True)
    return {"id": row.id, "object_id": obj.id, "revoked": True, "already": already}


def _is_share_host(request: Request) -> bool:
    base = os.getenv("STORAGE_SHARE_BASE_URL", "").strip()
    if not base:
        return False
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip().lower()
    return bool(host) and host == (urlparse(base).netloc or "").lower()


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="Not found", headers=_NO_LEAK)


def _creator_still_entitled(db: Session, row: ShareLink) -> bool:
    """A link must not outlive the right it came from. An agent's link holds
    only while the agent's own binding to the object still exists (unshare,
    owner change or a moved grant end it). Owner/admin links stand until
    revoked or expired."""
    who = row.created_by or ""
    if not who.startswith("agent:"):
        return True
    scope = who[len("agent:"):]
    g = db.query(MediaGrant.granted_via).filter(MediaGrant.object_id == row.object_id, MediaGrant.scope == scope).first()
    return g is not None and not (g[0] or "").startswith((R._SHARE_VIA, R._SHARE_RO_VIA))


@router.api_route("/s/{token}", methods=["GET", "HEAD"])
@router.api_route("/s/{token}/{name}", methods=["GET", "HEAD"])
def open_share_link(token: str, request: Request, name: Optional[str] = None, db: Session = Depends(get_db)):
    """Anonymous. Unknown, revoked, expired or deleted: the same 404."""
    from storage import signing

    if not _TOKEN_RE.match(token or ""):
        raise _not_found()
    row = db.query(ShareLink).filter(ShareLink.token_hash == _hash(token)).first()
    now = _utcnow()
    if row is None or row.revoked_at is not None or row.expires_at <= now:
        raise _not_found()
    obj = db.query(StorageObject).filter(StorageObject.id == row.object_id).first()
    if obj is None or getattr(obj, "tombstoned_at", None) is not None or getattr(obj, "encrypted", False):
        raise _not_found()
    if not _creator_still_entitled(db, row):
        raise _not_found()
    if not _abuse.share_allowed(obj):
        raise _not_found()
    try:
        params = signing.sign(obj.id, REDIRECT_TTL)
    except RuntimeError:
        raise HTTPException(status_code=503, detail={"error": "signing not configured", "code": "signing_unavailable"}, headers=_NO_LEAK)
    qs = urlencode(params)
    nice = _share_name(obj.original_filename)
    if _is_share_host(request):
        loc = f"/{obj.id}" + (f"/{quote(nice)}" if nice else "") + f"?{qs}"
    else:
        loc = f"/storage/media/{obj.id}?{qs}"
    print(f"🔗 share-link open id={row.id} prefix={row.token_prefix} object={obj.id}", flush=True)
    return Response(status_code=302, headers={"Location": loc, **_NO_LEAK})
