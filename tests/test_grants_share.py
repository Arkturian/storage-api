"""POST /storage/grants/share: nur wer gebunden ist, darf weitergeben (Post 5721)."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import main  # noqa: E402
import models  # noqa: E402
from database import get_db  # noqa: E402
from models import MediaGrant, StorageObject, TenantAPIKey  # noqa: E402
from storage import routes as storage_routes  # noqa: E402

SIGN = "k-sign-test"
PLAIN = "k-plain-test"
A = "cloud-session-x:4845524d494e45"
B = "cloud-session:Business"


@pytest.fixture
def env(tmp_path):
    models._hwm_ready.clear()
    eng = create_engine(f"sqlite:///{tmp_path/'s.db'}")
    models.Base.metadata.create_all(eng, tables=[StorageObject.__table__, MediaGrant.__table__, TenantAPIKey.__table__])
    S = sessionmaker(bind=eng)
    s = S()
    s.add_all([TenantAPIKey(api_key=SIGN, tenant_id="arkturian", label="sign", is_active=True, sign_tenants="arkturian"),
               TenantAPIKey(api_key=PLAIN, tenant_id="arkturian", label="plain", is_active=True)])
    mine, other = StorageObject(object_key="a", tenant_id="arkturian"), StorageObject(object_key="b", tenant_id="oneal")
    s.add_all([mine, other]); s.commit()
    for o in (mine, other):
        s.add(MediaGrant(object_id=o.id, scope=A, granted_by_user_id=1, granted_via="upload"))
    s.commit()
    ids = (mine.id, other.id)
    s.close()

    def _db():
        d = S()
        try:
            yield d
        finally:
            d.close()

    main.app.dependency_overrides[get_db] = _db
    main.app.dependency_overrides[storage_routes.get_current_user] = lambda: SimpleNamespace(id=1)
    try:
        yield TestClient(main.app), S, ids
    finally:
        main.app.dependency_overrides.clear()


def _share(c, oid, src=A, dst=(B,), key=SIGN, tenant="arkturian", h=None):
    return c.post("/storage/grants/share", headers={"X-API-KEY": key, **(h or {})},
                  json={"tenant": tenant, "object_id": oid, "from_scope": src, "to_scopes": list(dst)})


def _scopes(S, oid):
    s = S()
    try:
        return sorted(g.scope for g in s.query(MediaGrant).filter(MediaGrant.object_id == oid))
    finally:
        s.close()


def test_gebundener_scope_gibt_weiter(env):
    c, S, (mine, _) = env
    r = _share(c, mine)
    assert r.status_code == 200 and r.json()["shared"] == [B]
    assert _scopes(S, mine) == sorted([A, B])
    r = _share(c, mine)
    assert r.json()["shared"] == [] and r.json()["already"] == [B]
    assert _scopes(S, mine) == sorted([A, B])


def test_ungebundener_scope_darf_nicht(env):
    c, S, (mine, _) = env
    r = _share(c, mine, src="cloud-session:Fremd")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "not_granted"
    assert _scopes(S, mine) == [A]


@pytest.mark.parametrize("key,tenant", [(PLAIN, "arkturian"), (SIGN, "oneal"), ("unbekannt", "arkturian")])
def test_nur_signierschluessel_des_mandanten(env, key, tenant):
    c, S, (mine, _) = env
    assert _share(c, mine, key=key, tenant=tenant).status_code == 403
    assert _scopes(S, mine) == [A]


def test_fremder_mandant_ist_404(env):
    c, S, (_, other) = env
    assert _share(c, other).status_code == 404
    assert _scopes(S, other) == [A]


def test_on_behalf_of_abgewiesen(env):
    c, _, (mine, _) = env
    assert _share(c, mine, h={"X-On-Behalf-Of": "x@y.z"}).status_code == 400


@pytest.mark.parametrize("dst", [("content-post:1",), (), tuple(f"cloud-session:a{i}" for i in range(20)), ("kaputt",)])
def test_ziele_werden_geprueft(env, dst):
    c, S, (mine, _) = env
    assert _share(c, mine, dst=dst).status_code == 422
    assert _scopes(S, mine) == [A]
