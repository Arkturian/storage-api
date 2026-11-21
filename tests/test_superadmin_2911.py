"""#2911: tenant keys and X-On-Behalf-Of never reach operator routes, listings
never return full keys, and the bootstrap no longer (re)activates keys."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import auth  # noqa: E402
import tenancy.config as tc  # noqa: E402
from config import settings  # noqa: E402
from database import get_db  # noqa: E402
from models import Base, Tenant, TenantAPIKey, User  # noqa: E402

TENANT_KEY = "tenantkey-oneal-0001"
SERVICE_KEY = "svc-key-0002-abcdef"
ADMIN_KEY = "personal-admin-0003x"


@pytest.fixture
def db(tmp_path, monkeypatch):
    eng = create_engine(f"sqlite:///{tmp_path/'t.db'}")
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng)
    s = Session()
    s.add_all([Tenant(id="oneal", is_active=True), Tenant(id="arkturian", is_active=True)])
    s.add_all([
        TenantAPIKey(api_key=TENANT_KEY, tenant_id="oneal", is_active=True),
        TenantAPIKey(api_key=SERVICE_KEY, tenant_id="arkturian", is_active=True, is_service=True),
    ])
    s.add_all([
        # historic auto-provisioned tenant user with admin trust (the #2911 hole)
        User(email="oneal@api", display_name="O", password_hash="", api_key=TENANT_KEY,
             trust_level="admin", device_ids=[]),
        User(email="system@api", display_name="S", password_hash="", api_key="sys-random-xyz",
             trust_level="admin", device_ids=[]),
        User(email="ops@example.org", display_name="Ops", password_hash="", api_key=ADMIN_KEY,
             trust_level="admin", device_ids=[]),
        User(email="person@example.org", display_name="P", password_hash="", api_key="person-key-1",
             trust_level="user", device_ids=[]),
    ])
    s.commit()
    monkeypatch.setattr(tc, "SessionLocal", Session)
    yield s
    s.close()


@pytest.fixture
def client(db):
    from tenancy import routes as TR
    from admin import routes as AR
    app = FastAPI()
    app.include_router(TR.router)
    app.include_router(AR.router, prefix="/admin")
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def _h(key, on_behalf=None):
    h = {"X-API-KEY": key}
    if on_behalf:
        h["X-On-Behalf-Of"] = on_behalf
    return h


@pytest.mark.parametrize("path", ["/tenants/keys", "/tenants/status", "/admin/users-with-collections"])
def test_tenant_key_with_admin_trust_refused(client, path):
    assert client.get(path, headers=_h(TENANT_KEY)).status_code == 403


@pytest.mark.parametrize("principal", ["system@api", "oneal@api", "ops@example.org"])
def test_on_behalf_of_admin_or_machine_principal_refused(client, principal):
    r = client.get("/tenants/keys", headers=_h(SERVICE_KEY, principal))
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "principal_not_allowed"


def test_on_behalf_of_person_still_works_but_not_operator(client):
    r = client.get("/tenants/keys", headers=_h(SERVICE_KEY, "person@example.org"))
    assert r.status_code == 403  # a real person, but no operator rights


def test_destructive_admin_routes_refused_for_tenant_key(client):
    assert client.delete("/tenants/oneal?purge_objects=true", headers=_h(TENANT_KEY)).status_code == 403
    assert client.post("/admin/cleanup/by-age", headers=_h(TENANT_KEY), json={}).status_code in (403, 422)


def test_operator_listing_is_masked(client):
    r = client.get("/tenants/keys", headers=_h(ADMIN_KEY))
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body.values()) == {"oneal", "arkturian"}
    for k in body:
        assert TENANT_KEY not in k and SERVICE_KEY not in k
        assert len(k.rstrip("·")) <= 5


def test_master_key_still_operator(client, monkeypatch):
    monkeypatch.setattr(settings, "API_KEY", "master-value-for-test")
    assert client.get("/tenants/keys", headers=_h("master-value-for-test")).status_code == 200


def test_is_privileged_admin_fails_closed_without_key(db):
    u = User(email="x", display_name="x", password_hash="", api_key=None, trust_level="admin", device_ids=[])
    assert auth.is_privileged_admin(u, db) is False


def test_bootstrap_does_not_reactivate_or_seed_keys(db, monkeypatch, tmp_path):
    row = db.query(TenantAPIKey).filter(TenantAPIKey.api_key == TENANT_KEY).one()
    row.is_active = False
    db.commit()
    snapshot = tmp_path / "tenant_config.json"
    monkeypatch.setattr(tc, "TENANT_CONFIG_PATH", snapshot)
    tc.bootstrap_tenant_registry()
    db.expire_all()
    assert db.query(TenantAPIKey).filter(TenantAPIKey.api_key == TENANT_KEY).one().is_active is False
    # #3597: the burnt O'Neal key is compared by hash, never written here.
    from test_example_secrets_3597 import BURNT_KEY_HASHES
    import hashlib
    keys = [r.api_key or "" for r in db.query(TenantAPIKey).all()]
    assert not any(hashlib.sha256(k.encode()).hexdigest() in BURNT_KEY_HASHES for k in keys)
    assert db.query(TenantAPIKey).filter(TenantAPIKey.api_key == "Inetpass1").count() == 0
    assert not snapshot.exists()  # no plain-text key mirror any more


def test_no_hardcoded_keys_in_tenancy_or_admin():
    root = Path(__file__).resolve().parent.parent
    for rel in ("tenancy/config.py", "admin/routes.py"):
        src = (root / rel).read_text()
        assert '"Inetpass1"' not in src, rel  # burnt keys: test_example_secrets_3597


def test_deactivated_tenant_key_is_rejected_not_promoted(client, db):
    # Deactivating must never turn the key's <tenant>@api user (trust admin)
    # into an operator: before the follow-up it lost its "tenant key" label.
    row = db.query(TenantAPIKey).filter(TenantAPIKey.api_key == TENANT_KEY).one()
    row.is_active = False
    db.commit()
    assert client.get("/tenants/keys", headers=_h(TENANT_KEY)).status_code == 401
    u = db.query(User).filter(User.email == "oneal@api").one()
    assert auth.is_privileged_admin(u, db) is False


def test_machine_account_never_operator(db):
    u = User(email="koralmbahn@api", display_name="k", password_hash="", api_key="random-not-a-tenant-key",
             trust_level="admin", device_ids=[])
    assert auth.is_privileged_admin(u, db) is False
    sys_user = db.query(User).filter(User.email == "system@api").one()
    # #1960: system@api is an operator only when it came in through the master key
    assert auth.is_privileged_admin(sys_user, db) is False
    sys_user._storage_privileged_admin = None
    sys_user._storage_auth_via = "master"
    assert auth.is_privileged_admin(sys_user, db) is True
