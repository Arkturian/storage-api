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


# --- Mandantenübergreifend: Absender, nur lesend, Widerruf (Post 5721) ---

C = "cloud-user:7"
D = "cloud-session:Dritter"


def _unshare(c, oid, src=A, scopes=(B,), key=SIGN, tenant="arkturian"):
    return c.post("/storage/grants/unshare", headers={"X-API-KEY": key},
                  json={"tenant": tenant, "object_id": oid, "from_scope": src, "scopes": list(scopes)})


def _lookup(c, scope, ids, key=SIGN, tenant="arkturian"):
    return c.post("/storage/grants/lookup", headers={"X-API-KEY": key},
                  json={"tenant": tenant, "scope": scope, "object_ids": list(ids)})


def _via(S, oid):
    s = S()
    try:
        return {g.scope: g.granted_via for g in s.query(MediaGrant).filter_by(object_id=oid)}
    finally:
        s.close()


def test_share_merkt_absender(env):
    c, S, (mine, _) = env
    assert _share(c, mine).status_code == 200
    assert _via(S, mine)[B] == "share:" + A


def test_nur_lesend_darf_nicht_weitergeben(env):
    c, S, (mine, _) = env
    r = c.post("/storage/grants/share", headers={"X-API-KEY": SIGN},
               json={"tenant": "arkturian", "object_id": mine, "from_scope": A, "to_scopes": [B, C], "reshare": False})
    assert r.status_code == 200 and _via(S, mine)[C] == "share-ro:" + A
    r = _share(c, mine, src=C, dst=(D,))
    assert r.status_code == 403 and r.json()["detail"]["code"] == "reshare_not_allowed"
    assert D not in _via(S, mine)


def test_widerruf_nur_eigene_und_kaskade(env):
    c, S, (mine, _) = env
    _share(c, mine, dst=(B,))
    _share(c, mine, src=B, dst=(D,))          # B gibt weiter
    r = _unshare(c, mine, scopes=(B, A))      # A ist Upload, nicht von A geteilt
    assert r.status_code == 200
    j = r.json()
    assert j["revoked"] == [B] and j["cascade"] == [D] and j["not_shared_by_you"] == [A]
    assert set(_via(S, mine)) == {A}


def test_widerruf_fremder_absender_wirkt_nicht(env):
    c, S, (mine, _) = env
    _share(c, mine, dst=(B,))
    r = _unshare(c, mine, src=D, scopes=(B,))
    assert r.json()["revoked"] == [] and B in _via(S, mine)


def test_widerruf_tor_und_mandant(env):
    c, S, (mine, other) = env
    assert _unshare(c, mine, key=PLAIN).status_code == 403
    assert _unshare(c, other).status_code == 404


def test_lookup_zeigt_absender_und_empfaenger(env):
    c, S, (mine, other) = env
    c.post("/storage/grants/share", headers={"X-API-KEY": SIGN},
           json={"tenant": "arkturian", "object_id": mine, "from_scope": A, "to_scopes": [C], "reshare": False})
    j = _lookup(c, C, [mine, other, 999]).json()
    assert [i["object_id"] for i in j["items"]] == [mine]
    assert j["items"][0]["shared_by"] == A and j["items"][0]["reshare"] is False
    j = _lookup(c, A, [mine]).json()
    assert j["items"][0]["shared_by"] is None and j["items"][0]["shared_with"] == [C]
    assert _lookup(c, A, [mine], key=PLAIN).status_code == 403


def test_move_nimmt_absender_mit(env):
    c, S, (mine, _) = env
    _share(c, mine, dst=(B,))
    neu = "cloud-session:HermineNeu"
    r = c.post("/storage/grants/move", headers={"X-API-KEY": SIGN},
               json={"tenant": "arkturian", "from_scope": A, "to_scope": neu})
    assert r.status_code == 200
    assert _via(S, mine) == {neu: "upload", B: "share:" + neu}
    assert _unshare(c, mine, src=neu).json()["revoked"] == [B]
