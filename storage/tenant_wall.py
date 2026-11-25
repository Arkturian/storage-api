"""Wall between tenants behind the MCP gateway (#4007).

The gateway calls storage-api with one key of tenant "arkturian" (owner
system@api) for every caller. X-Grant-Scope-Restrict (#3030) already keeps
an agent away from other agents' PRIVATE files, but a caller of another
tenant still listed every public object of the operator and of every other
tenant as "mine", and could rewrite the search text of any public object the
gateway account owns.

The wall applies when all of these hold:
  * the request is narrowed (X-Grant-Scope-Restrict, i.e. not a superadmin),
  * the presenting key is active and flagged may_assert_scope (the gateway),
  * the caller's verified home tenant (X-Caller-Tenant, #3811) is not the
    key's tenant, or is missing (fail closed: then no tenant is "own"),
  * and the key's tenant is not among the caller's verified tenants
    (X-Grant-Scope-Tenants, #3329: memberships of a person).

Under the wall a caller of tenant T
  * lists and finds only rows with origin_tenant == T, rows bound to its own
    scope (an explicit share), and intern rows (cross-tenant by definition,
    #3329/#3982),
  * may still read by id what anyone may read (plainly public, see
    grant_restrict.allowed), since /storage/objects/{id} and /storage/media
    answer that without a key anyway,
  * changes only rows with origin_tenant == T.
"""
from __future__ import annotations

from typing import Optional, Tuple

from storage import grant_restrict, origin_tenant

READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def wall(db) -> Optional[Tuple[Optional[str]]]:
    """None when no wall applies, else a 1-tuple with the caller's tenant (or None)."""
    if not grant_restrict.current_scope() or db is None:
        return None
    cached = grant_restrict._wall.get()
    if cached is not None:
        return cached[0]
    result: Optional[Tuple[Optional[str]]] = None
    key = grant_restrict._api_key.get()
    if key:
        try:
            from models import TenantAPIKey
            row = (db.query(TenantAPIKey)
                   .filter(TenantAPIKey.api_key == key, TenantAPIKey.is_active.is_(True)).one_or_none())
        except Exception:
            row = None
        if row is not None and bool(getattr(row, "may_assert_scope", False)):
            caller = origin_tenant.trusted(db)
            # A verified member of the key's tenant (X-Grant-Scope-Tenants, from
            # the same checked JWT) is no stranger, whatever their home tenant.
            member = row.tenant_id in grant_restrict.trusted_tenants(db)
            if caller != row.tenant_id and not member:
                result = (caller,)
    grant_restrict._wall.set((result,))
    return result


def _granted(obj, db) -> bool:
    scope = grant_restrict.current_scope()
    if not scope or obj is None or db is None:
        return False
    from models import MediaGrant
    return db.query(MediaGrant.id).filter(MediaGrant.object_id == obj.id,
                                          MediaGrant.scope == scope).first() is not None


def listable(obj, db) -> bool:
    """May this row appear in a listing or search result for the caller?"""
    w = wall(db)
    if w is None:
        return True
    if obj is None:
        return False
    if w[0] is not None and getattr(obj, "origin_tenant", None) == w[0]:
        return True
    if getattr(obj, "visibility_level", None) == "intern":
        return True
    return _granted(obj, db)


def may_touch(obj, db, method: str) -> bool:
    """Per-object gate: reads stay as grant_restrict decides, changes need the own tenant."""
    w = wall(db)
    if w is None or (method or "").upper() in READ_METHODS:
        return True
    return obj is not None and w[0] is not None and getattr(obj, "origin_tenant", None) == w[0]


def sql_filter(model, db):
    """Criterion for listings, or None when no wall applies."""
    w = wall(db)
    if w is None:
        return None
    from sqlalchemy import or_
    from models import MediaGrant
    granted = db.query(MediaGrant.object_id).filter(MediaGrant.scope == grant_restrict.current_scope())
    parts = [model.visibility_level == "intern", model.id.in_(granted)]
    if w[0] is not None:
        parts.insert(0, model.origin_tenant == w[0])
    return or_(*parts)
