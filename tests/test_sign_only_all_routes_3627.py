"""#3627: a sign_only key reaches POST /storage/sign and nothing else.

Walks every route the app really has, so a route added later is covered without
touching this file. Every refusal must carry the gate's own code, so a 403 from
a route handler (which would mean the key got that far) does not count.
"""
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.routing import APIRoute  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from starlette.routing import Route  # noqa: E402

import database  # noqa: E402
import main  # noqa: E402
import models  # noqa: E402
from database import get_db  # noqa: E402
from models import MediaGrant, StorageObject, TenantAPIKey  # noqa: E402

SIGN_ONLY = "k-sign-only-3627"
OFF = "k-sign-only-off-3627"
WIDE = "k-wide-3627"
SCOPE = "cloud-session:Goliath"
ROUTE = ("POST", "/storage/sign")


def _route_entries():
    """Every (path, methods, route) the app serves, also inside included routers.

    FastAPI 0.140 keeps include_router() as one nested entry in app.routes, so a
    plain walk sees only ten routes. A missing attribute after an upgrade must
    fail this test, not silently shrink what it covers.
    """
    for r in main.app.routes:
        if type(r).__name__ == "_IncludedRouter":
            for ctx in r.effective_route_contexts():
                yield ctx.path, ctx.methods, ctx.original_route
        else:
            yield getattr(r, "path", ""), getattr(r, "methods", None), r


def _all_endpoints():
    out = []
    for path, methods, _ in _route_entries():
        concrete = re.sub(r"\{[^}]+\}", "1", path)
        out += [(m, concrete) for m in sorted(methods or []) if m != "OPTIONS"]
    return sorted(set(out))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_SIGN_SECRETS", '{"k1": "' + "x" * 40 + '"}')
    monkeypatch.setenv("STORAGE_SIGN_ACTIVE_KID", "k1")
    models._hwm_ready.clear()
    eng = create_engine(f"sqlite:///{tmp_path/'s.db'}")
    models.Base.metadata.create_all(eng)
    S = sessionmaker(bind=eng)
    s = S()
    s.add_all([
        TenantAPIKey(api_key=SIGN_ONLY, tenant_id="david", is_active=True, sign_tenants="david", sign_only=True),
        TenantAPIKey(api_key=OFF, tenant_id="david", is_active=False, sign_tenants="david", sign_only=True),
        TenantAPIKey(api_key=WIDE, tenant_id="david", is_active=True, sign_tenants="david"),
    ])
    obj = StorageObject(object_key="o", tenant_id="david", is_public=False,
                        metadata_json={"tenant_id": "david", "private_media": True})
    s.add(obj)
    s.commit()
    s.add(MediaGrant(object_id=obj.id, scope=SCOPE, granted_by_user_id=1, granted_via="upload"))
    s.commit()
    oid = obj.id
    s.close()

    def _db():
        d = S()
        try:
            yield d
        finally:
            d.close()

    main.app.dependency_overrides[get_db] = _db
    # Real auth on purpose: the gate must stop the key before auth.py runs.
    try:
        yield TestClient(main.app, raise_server_exceptions=False), oid, eng
    finally:
        main.app.dependency_overrides.clear()


def _snapshot(eng):
    """Every row of every table, to prove a refused call changed nothing."""
    with eng.connect() as conn:
        return {t.name: sorted(map(repr, conn.execute(t.select()).all()))
                for t in models.Base.metadata.sorted_tables}


# What Cloud-release measured getting through auth on 0794fac (#3627).
LET_THROUGH_0794FAC = [
    ("POST", "/storage/grants/lookup"), ("POST", "/storage/grants/unshare"),
    ("POST", "/storage/media/clear-cache"), ("POST", "/storage/upload"), ("POST", "/storage/bulk-delete"),
    ("GET", "/storage/kg/stats"), ("GET", "/storage/kg/health"), ("GET", "/storage/cache-status"),
    ("GET", "/storage/list"), ("GET", "/storage/tasks"), ("GET", "/storage/asset-refs"),
]


def _sign(c, key, tenant, oid):
    return c.post("/storage/sign", headers={"X-API-KEY": key},
                  json={"tenant": tenant, "scope": SCOPE, "ids": [oid], "ttl": 60})


def test_route_walk_sees_the_app():
    eps = _all_endpoints()
    assert ROUTE in eps
    assert len(eps) > 60, "route walk found too few routes"
    assert ("POST", "/storage/upload") in eps and ("POST", "/storage/grants/unshare") in eps


def test_no_route_type_escapes_the_http_middleware():
    # The gate is an HTTP middleware: a websocket or mounted app would need its own.
    assert [path for path, _, r in _route_entries() if not isinstance(r, (APIRoute, Route))] == []


def test_gate_is_the_outermost_middleware():
    # A middleware outside the gate can answer without it (HEAD /storage/media did).
    outer = main.app.user_middleware[0]
    assert outer.kwargs.get("dispatch") is main.sign_only_keys


def test_head_on_media_is_refused_before_it_is_answered(env):
    c, oid, _ = env
    r = c.head(f"/storage/media/{oid}", headers={"X-API-KEY": SIGN_ONLY})
    assert r.status_code == 403
    assert "x-mime-type" not in r.headers


def test_sign_only_key_is_refused_on_every_other_route(env):
    c, _, eng = env
    before = _snapshot(eng)
    refused, wrong = 0, []
    for method, path in _all_endpoints():
        if (method, path) == ROUTE:
            continue
        r = c.request(method, path, headers={"X-API-KEY": SIGN_ONLY})
        ok = r.status_code == 403 and (method == "HEAD" or r.json()["detail"]["code"] == "sign_only_key")
        if ok:
            refused += 1
        else:
            wrong.append((method, path, r.status_code))
    assert wrong == []
    assert refused > 60
    assert _snapshot(eng) == before, "a refused call changed the database"


@pytest.mark.parametrize("method,path", LET_THROUGH_0794FAC)
def test_routes_that_let_it_through_before_now_refuse_it(env, method, path):
    c, _, eng = env
    assert (method, path) in _all_endpoints(), "route renamed: update this list"
    before = _snapshot(eng)
    r = c.request(method, path, headers={"X-API-KEY": SIGN_ONLY}, json={})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "sign_only_key"
    assert _snapshot(eng) == before


def test_signing_creates_no_user_and_copies_no_key(env):
    c, oid, eng = env
    assert _sign(c, SIGN_ONLY, "david", oid).status_code == 200
    with eng.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM users")).scalar() == 0
        assert conn.execute(text("SELECT count(*) FROM users WHERE api_key=:k"), {"k": SIGN_ONLY}).scalar() == 0


def test_key_stops_working_with_its_row(env):
    c, oid, eng = env
    assert _sign(c, SIGN_ONLY, "david", oid).status_code == 200
    with eng.begin() as conn:
        conn.execute(text("DELETE FROM tenant_api_keys WHERE api_key=:k"), {"k": SIGN_ONLY})
    assert _sign(c, SIGN_ONLY, "david", oid).status_code == 401


@pytest.mark.parametrize("method", ["GET", "PUT", "PATCH", "DELETE", "HEAD"])
def test_other_methods_on_the_sign_path_are_refused(env, method):
    c, _, _ = env
    assert c.request(method, "/storage/sign", headers={"X-API-KEY": SIGN_ONLY}).status_code == 403


def test_sign_only_key_signs_its_tenant_for_any_scope(env):
    c, oid, _ = env
    r = _sign(c, SIGN_ONLY, "david", oid)
    assert r.status_code == 200
    assert r.json()["items"][0]["signed"] is True


def test_sign_only_key_cannot_sign_a_foreign_tenant(env):
    c, oid, _ = env
    assert _sign(c, SIGN_ONLY, "arkturian", oid).status_code == 403


def test_forged_host_header_does_not_open_the_gate(env):
    c, _, _ = env
    r = c.post("/storage/grants/lookup",
               headers={"X-API-KEY": SIGN_ONLY, "Host": "x/storage/sign?"},
               json={"tenant": "david", "scope": SCOPE, "object_ids": [1]})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "sign_only_key"


def test_flag_is_what_decides(env):
    assert main._is_sign_only_key(SIGN_ONLY) is True
    assert main._is_sign_only_key(WIDE) is False
    assert main._is_sign_only_key(OFF) is False      # inactive: normal auth refuses it
    assert main._is_sign_only_key("unknown") is False


def test_key_without_flag_keeps_its_routes(env):
    c, oid, _ = env
    r = c.post("/storage/grants/lookup", headers={"X-API-KEY": WIDE},
               json={"tenant": "david", "scope": SCOPE, "object_ids": [oid]})
    assert r.status_code == 200


def test_migration_adds_the_column_to_an_old_table(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path/'old.db'}")
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE tenant_api_keys (api_key VARCHAR PRIMARY KEY, tenant_id VARCHAR, "
                          "label VARCHAR, is_active BOOLEAN, created_at DATETIME, last_used_at DATETIME)"))
        conn.execute(text("INSERT INTO tenant_api_keys (api_key, tenant_id, is_active) VALUES ('k', 'david', 1)"))
        database.migrate_tenant_api_keys(conn)
        database.migrate_tenant_api_keys(conn)   # idempotent
        assert conn.execute(text("SELECT sign_only FROM tenant_api_keys")).scalar() == 0
