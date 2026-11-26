"""Narrow a request to what one grant scope may see (#3030, Befund 105).

The MCP gateway talks to storage-api with one shared key (system@api), the
owner of every agent and chat attachment. Every agent was therefore "owner"
of every other agent's private files. The gateway now sends

    X-Grant-Scope-Restrict: cloud-session:<verified agent>

and storage-api lets such a request see a row only if it is plainly public
(is_public and not private_media) or carries a media_grant for that scope.

The header can only take visibility away, never add it, so it needs no trust:
anyone may send it, and a caller that omits it is judged exactly as before.
"""
from __future__ import annotations

import contextvars
import re
from typing import Optional

HEADER = b"x-grant-scope-restrict"
# #3329: the narrowed agent's verified tenants, set by the MCP gateway from
# the checked JWT. Believed only from a key flagged may_assert_scope and only
# together with X-Grant-Scope-Restrict; otherwise treated as empty.
TENANTS_HEADER = b"x-grant-scope-tenants"
_scope: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("grant_scope_restrict", default=None)
_tenants_raw: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("grant_scope_tenants", default=None)
_api_key: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("grant_scope_api_key", default=None)
_tenants_ok: contextvars.ContextVar[Optional[frozenset]] = contextvars.ContextVar("grant_scope_tenants_ok", default=None)
# #4007: per-request cache of storage.tenant_wall.wall(), reset with the rest.
_wall: contextvars.ContextVar[Optional[tuple]] = contextvars.ContextVar("grant_scope_tenant_wall", default=None)
# #4087: tenant of the presenting gateway key, cached by trusted_tenants().
_gw_tenant: contextvars.ContextVar[Optional[tuple]] = contextvars.ContextVar("grant_scope_gateway_tenant", default=None)
_TENANT_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
LEVELS = ("personal", "intern")


def current_scope() -> Optional[str]:
    return _scope.get()


def trusted_tenants(db) -> frozenset:
    """Tenants of the narrowed agent, or empty unless every condition holds:
    restrict header present, tenants header well-formed (<= 100 slugs), and
    the presenting key is active and flagged may_assert_scope."""
    if not current_scope():
        return frozenset()
    cached = _tenants_ok.get()
    if cached is not None:
        return cached
    result = frozenset()
    gw_tenant = None
    raw, key = _tenants_raw.get(), _api_key.get()
    if raw and key and db is not None:
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        if 0 < len(parts) <= 100 and all(_TENANT_RE.match(p) for p in parts):
            try:
                from models import TenantAPIKey
                row = (db.query(TenantAPIKey)
                       .filter(TenantAPIKey.api_key == key, TenantAPIKey.is_active.is_(True)).one_or_none())
                if row is not None and bool(getattr(row, "may_assert_scope", False)):
                    result = frozenset(parts)
                    gw_tenant = row.tenant_id
            except Exception:
                result = frozenset()
    _tenants_ok.set(result)
    _gw_tenant.set((gw_tenant,))
    return result


def tenant_readable(obj, db) -> bool:
    """#4087 (Alex 26.11.): an upload belongs to its tenant, and every agent of
    that tenant reads it, private_media included. Only for a narrowed agent
    whose tenants the gateway verified (X-Grant-Scope-Tenants from a key
    flagged may_assert_scope), only by origin_tenant, which storage-api itself
    stored from a trusted source (#3811/#4007).

    Not for the gateway key's own tenant (the instance operator): its agents
    keep the per-grant rule of #3030, so Business, Ebanking and the like stay
    apart. Never for encrypted objects or a safety block. Reads only; changes
    stay with the grant and the wall (tenant_wall.may_touch)."""
    if obj is None or not current_scope() or db is None:
        return False
    if getattr(obj, "encrypted", False) or getattr(obj, "abuse_status", None) == "hit":
        return False
    origin = getattr(obj, "origin_tenant", None)
    if not origin:
        return False
    tenants = trusted_tenants(db)
    if origin not in tenants:
        return False
    cached = _gw_tenant.get()
    gw = cached[0] if cached else None
    return bool(gw) and origin != gw


def level_readable(obj, db) -> bool:
    """A level object the narrowed agent may see: intern with any verified
    tenant, personal when the object's tenant is one of them."""
    if obj is None or getattr(obj, "is_public", False) or getattr(obj, "encrypted", False):
        return False
    level = getattr(obj, "visibility_level", None)
    if level not in LEVELS or getattr(obj, "abuse_status", None) == "hit":
        return False
    md = obj.metadata_json if isinstance(getattr(obj, "metadata_json", None), dict) else {}
    if md.get("private_media"):
        return False
    tenants = trusted_tenants(db)
    if not tenants:
        return False
    return level == "intern" or getattr(obj, "tenant_id", None) in tenants


def plainly_public(obj) -> bool:
    md = obj.metadata_json if isinstance(getattr(obj, "metadata_json", None), dict) else {}
    return bool(getattr(obj, "is_public", False)) and not md.get("private_media")


def allowed(obj, db, *, tenant: bool = True) -> bool:
    """True when no restriction is active, or the row is visible under it.
    tenant=False leaves out the #4087 tenant read (for changing requests)."""
    scope = current_scope()
    if not scope:
        return True
    if obj is None:
        return False
    if plainly_public(obj):
        return True
    if db is None:
        return False
    from models import MediaGrant
    if db.query(MediaGrant.id).filter(MediaGrant.object_id == obj.id, MediaGrant.scope == scope).first() is not None:
        return True
    if level_readable(obj, db):
        return True
    return tenant and tenant_readable(obj, db)


def sql_filter(model, db):
    """SQLAlchemy criterion for listings, or None when unrestricted."""
    scope = current_scope()
    if not scope:
        return None
    from sqlalchemy import and_, func, or_
    from models import MediaGrant
    granted = db.query(MediaGrant.object_id).filter(MediaGrant.scope == scope)
    not_pm = func.coalesce(func.json_extract(model.metadata_json, "$.private_media"), 0) != 1
    parts = [and_(model.is_public.is_(True), not_pm), model.id.in_(granted)]
    tenants = trusted_tenants(db)
    if tenants:
        level_ok = and_(model.is_public.is_(False), not_pm, model.encrypted.is_(False),
                        or_(model.abuse_status.is_(None), model.abuse_status != "hit"))
        parts.append(and_(level_ok, model.visibility_level == "intern"))
        parts.append(and_(level_ok, model.visibility_level == "personal", model.tenant_id.in_(sorted(tenants))))
        # #4087: the own tenant's uploads, private_media included (see tenant_readable).
        cached = _gw_tenant.get()
        gw = cached[0] if cached else None
        own = sorted(t for t in tenants if t != gw)
        if gw and own:
            parts.append(and_(model.encrypted.is_(False),
                              or_(model.abuse_status.is_(None), model.abuse_status != "hit"),
                              model.origin_tenant.in_(own)))
    return or_(*parts)


class GrantScopeRestrict:
    """ASGI middleware: read the header into the context for this request."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        value = tenants = api_key = None
        for k, v in scope.get("headers") or []:
            kl = k.lower()
            if kl == HEADER:
                value = v.decode("latin-1").strip()[:200] or None
            elif kl == TENANTS_HEADER:
                tenants = v.decode("latin-1").strip()[:7000] or None
            elif kl == b"x-api-key":
                api_key = v.decode("latin-1").strip() or None
        if value is None:
            await self.app(scope, receive, send)
            return
        tokens = (_scope.set(value), _tenants_raw.set(tenants), _api_key.set(api_key), _tenants_ok.set(None),
                  _wall.set(None), _gw_tenant.set(None))
        try:
            await self.app(scope, receive, send)
        finally:
            for var, tok in zip((_scope, _tenants_raw, _api_key, _tenants_ok, _wall, _gw_tenant), tokens):
                var.reset(tok)
