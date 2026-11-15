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
from typing import Optional

HEADER = b"x-grant-scope-restrict"
_scope: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("grant_scope_restrict", default=None)


def current_scope() -> Optional[str]:
    return _scope.get()


def plainly_public(obj) -> bool:
    md = obj.metadata_json if isinstance(getattr(obj, "metadata_json", None), dict) else {}
    return bool(getattr(obj, "is_public", False)) and not md.get("private_media")


def allowed(obj, db) -> bool:
    """True when no restriction is active, or the row is visible under it."""
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
    return db.query(MediaGrant.id).filter(MediaGrant.object_id == obj.id, MediaGrant.scope == scope).first() is not None


def sql_filter(model, db):
    """SQLAlchemy criterion for listings, or None when unrestricted."""
    scope = current_scope()
    if not scope:
        return None
    from sqlalchemy import and_, func, or_
    from models import MediaGrant
    granted = db.query(MediaGrant.object_id).filter(MediaGrant.scope == scope)
    return or_(
        and_(model.is_public.is_(True),
             func.coalesce(func.json_extract(model.metadata_json, "$.private_media"), 0) != 1),
        model.id.in_(granted),
    )


class GrantScopeRestrict:
    """ASGI middleware: read the header into the context for this request."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        value = None
        for k, v in scope.get("headers") or []:
            if k.lower() == HEADER:
                value = v.decode("latin-1").strip()[:200] or None
                break
        if value is None:
            await self.app(scope, receive, send)
            return
        token = _scope.set(value)
        try:
            await self.app(scope, receive, send)
        finally:
            _scope.reset(token)
