"""#3811: home tenant of the caller behind the MCP gateway.

The gateway uploads with one shared key of tenant "arkturian". Without a mark,
a customer agent's files are indistinguishable from ours. The gateway sends the
`tenant` claim of the verified JWT as X-Caller-Tenant; storage-api believes it
only from an active key flagged may_assert_scope (or, since #4007,
may_assert_origin), or from the instance's own master key (#4009, not the
old public default), and stores it on new objects
as storage_objects.origin_tenant. The object's tenant_id does not change.

A missing or untrusted header marks nothing; that is not an error.
"""
import contextvars
import re
from contextlib import contextmanager
from typing import Optional

HEADER = b"x-caller-tenant"
_TENANT_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
_raw: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("caller_tenant_raw", default=None)
_api_key: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("caller_tenant_key", default=None)
_override: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("caller_tenant_override", default=None)


def trusted(db) -> Optional[str]:
    """The caller's home tenant, or None unless the presenting key may assert it."""
    forced = _override.get()
    if forced is not None:
        return forced or None
    raw, key = _raw.get(), _api_key.get()
    if not raw or not key or db is None or not _TENANT_RE.match(raw):
        return None
    if _is_master(key):
        return raw  # #4009 (a): the instance's master key is full operator anyway
    try:
        from models import TenantAPIKey
        row = (db.query(TenantAPIKey)
               .filter(TenantAPIKey.api_key == key, TenantAPIKey.is_active.is_(True)).one_or_none())
    except Exception:
        return None
    if row is None:
        return None
    # #4007: may_assert_origin marks uploads only (cloud-api portal uploads).
    ok = bool(getattr(row, "may_assert_scope", False)) or bool(getattr(row, "may_assert_origin", False))
    return raw if ok else None


def _is_master(key: str) -> bool:
    """The storage-api's own master key (settings.API_KEY), never the old public default (#3969)."""
    try:
        import hmac
        from config import settings
        from storage.public_default import is_public_default
        master = settings.API_KEY or ""
        return bool(master) and not is_public_default(master) and hmac.compare_digest(
            key.encode(), master.encode())
    except Exception:
        return False


@contextmanager
def use(tenant: Optional[str]):
    """Apply a value captured earlier (upload ticket: created by the gateway, consumed without a key)."""
    token = _override.set(tenant or "")
    try:
        yield
    finally:
        _override.reset(token)


class CallerTenantHeader:
    """ASGI middleware: remember X-Caller-Tenant and X-API-KEY for this request."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        value = api_key = None
        for k, v in scope.get("headers") or []:
            kl = k.lower()
            if kl == HEADER:
                value = v.decode("latin-1").strip()[:80] or None
            elif kl == b"x-api-key":
                api_key = v.decode("latin-1").strip() or None
        if value is None:
            await self.app(scope, receive, send)
            return
        tokens = (_raw.set(value), _api_key.set(api_key))
        try:
            await self.app(scope, receive, send)
        finally:
            _raw.reset(tokens[0])
            _api_key.reset(tokens[1])
