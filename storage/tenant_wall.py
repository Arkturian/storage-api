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
  * the gateway names the caller's tenants (X-Caller-Tenant, #3811, and/or
    X-Grant-Scope-Tenants, #3329), and the key's tenant is not among them.
A gateway that names no tenant (older than #3811/#3329, as on single-tenant
customer instances) keeps the #3030 behaviour.

Under the wall a caller with own tenants T
  * lists as "mine" only rows with origin_tenant in T and rows bound to its
    own scope (an explicit share) (#4087),
  * finds and lists beyond "mine" also intern rows (cross-tenant by
    definition, #3329/#3982) and plainly public rows of anyone, the
    operator's included: "alles was öffentlich ist darf er natürlich sehen"
    (Alex 26.11., #4087),
  * reads by id what anyone may read (plainly public) plus, through
    grant_restrict.tenant_readable, every upload of its own tenant,
  * changes only rows with origin_tenant in T.
"""
from __future__ import annotations

from typing import Optional, Tuple

from storage import grant_restrict, origin_tenant

READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def wall(db) -> Optional[frozenset]:
    """None when no wall applies, else the caller's own tenants (possibly empty)."""
    if not grant_restrict.current_scope() or db is None:
        return None
    cached = grant_restrict._wall.get()
    if cached is not None:
        return cached[0]
    result: Optional[frozenset] = None
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
            # X-Grant-Scope-Tenants: verified tenants from the same checked JWT.
            tenants = grant_restrict.trusted_tenants(db)
            own = frozenset(t for t in ({caller} | set(tenants)) if t)
            # A gateway that names no tenant at all (older than #3811/#3329, e.g.
            # on a single-tenant customer instance) keeps the #3030 behaviour.
            # One that names tenants walls off everything not theirs, unless the
            # caller belongs to the key's own tenant.
            if own and row.tenant_id not in own:
                result = own
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
    if getattr(obj, "origin_tenant", None) in w:
        return True
    if getattr(obj, "visibility_level", None) == "intern":
        return True
    if grant_restrict.plainly_public(obj):  # #4087: public stays findable
        return True
    return _granted(obj, db)


def may_touch(obj, db, method: str) -> bool:
    """Per-object gate: reads stay as grant_restrict decides, changes need the own tenant."""
    w = wall(db)
    if w is None or (method or "").upper() in READ_METHODS:
        return True
    return obj is not None and getattr(obj, "origin_tenant", None) in w


def sql_filter(model, db, mine: bool = False):
    """Criterion for listings, or None when no wall applies.

    mine=True (#4087): only the own tenant's rows and rows bound to the
    caller's scope. Otherwise also intern rows and plainly public rows."""
    w = wall(db)
    if w is None:
        return None
    from sqlalchemy import and_, func, or_
    from models import MediaGrant
    granted = db.query(MediaGrant.object_id).filter(MediaGrant.scope == grant_restrict.current_scope())
    parts = [model.origin_tenant.in_(sorted(w)), model.id.in_(granted)]
    if not mine:
        not_pm = func.coalesce(func.json_extract(model.metadata_json, "$.private_media"), 0) != 1
        parts += [model.visibility_level == "intern", and_(model.is_public.is_(True), not_pm)]
    return or_(*parts)
