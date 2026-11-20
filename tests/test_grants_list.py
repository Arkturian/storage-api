"""POST /storage/grants/list: Objekte, die an genau einen Scope gebunden sind (Portal-Galerie)."""
import sys
from datetime import datetime, timedelta
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

ALEX = "cloud-user:2"
FREMD = "cloud-user:7"
SIGNER = "k-signer"
NARROW = "k-narrow"
PLAIN = "k-plain"
SAMMLUNG = "tschapp-photospots"


@pytest.fixture
def env(tmp_path):
    models._hwm_ready.clear()
    eng = create_engine(f"sqlite:///{tmp_path/'s.db'}")
    models.Base.metadata.create_all(eng, tables=[StorageObject.__table__, MediaGrant.__table__, TenantAPIKey.__table__])
    S = sessionmaker(bind=eng)
    s = S()
    s.add_all([
        TenantAPIKey(api_key=SIGNER, tenant_id="arkturian", is_active=True, sign_tenants="arkturian"),
        TenantAPIKey(api_key=NARROW, tenant_id="arkturian", is_active=True, sign_tenants="arkturian", sign_scopes=ALEX),
        TenantAPIKey(api_key=PLAIN, tenant_id="arkturian", is_active=True),
    ])
    t0 = datetime(2026, 11, 9, 8, 0)
    priv = {"private_media": True}

    def obj(key, coll, minute, tenant="arkturian", **kw):
        return StorageObject(object_key=key, tenant_id=tenant, collection_id=coll, is_public=False,
                             metadata_json=priv, mime_type="image/jpeg", width=1170, height=2532,
                             file_size_bytes=900000, original_filename=f"{key}.jpg",
                             context="tschapp-photospot:s1", created_at=t0 + timedelta(minutes=minute), **kw)
    o = {
        "a1": obj("a1", SAMMLUNG, 1), "a2": obj("a2", SAMMLUNG, 2), "a3": obj("a3", SAMMLUNG, 3),
        "andere": obj("andere", "sonstwas", 4),
        "fremd": obj("fremd", SAMMLUNG, 5),
        "weg": obj("weg", SAMMLUNG, 6, tombstoned_at=t0),
        "alt": obj("alt", SAMMLUNG, 7, expires_at=datetime.utcnow() - timedelta(hours=1)),
        "mandant": obj("mandant", SAMMLUNG, 8, tenant="oneal"),
    }
    s.add_all(o.values()); s.commit()
    for k in ("a1", "a2", "a3", "andere", "weg", "alt", "mandant"):
        s.add(MediaGrant(object_id=o[k].id, scope=ALEX, granted_by_user_id=1, granted_via="upload"))
    s.add(MediaGrant(object_id=o["fremd"].id, scope=FREMD, granted_by_user_id=1, granted_via="upload"))
    s.commit()
    ids = {k: v.id for k, v in o.items()}
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
        yield TestClient(main.app), ids
    finally:
        main.app.dependency_overrides.clear()


def _list(c, key=SIGNER, **body):
    body = {"tenant": "arkturian", "scope": ALEX, **body}
    return c.post("/storage/grants/list", headers={"X-API-KEY": key}, json=body)


def test_nur_eigener_scope_neueste_zuerst(env):
    c, ids = env
    r = _list(c, collection_id=SAMMLUNG)
    assert r.status_code == 200
    j = r.json()
    assert [i["id"] for i in j["items"]] == [ids["a3"], ids["a2"], ids["a1"]]
    assert j["total"] == 3


def test_ohne_sammlung_alle_gebundenen_des_mandanten(env):
    c, ids = env
    got = {i["id"] for i in _list(c).json()["items"]}
    assert got == {ids["a1"], ids["a2"], ids["a3"], ids["andere"]}


def test_felder_ohne_adressen(env):
    c, _ = env
    item = _list(c, collection_id=SAMMLUNG).json()["items"][0]
    assert set(item) == {"id", "mime_type", "created_at", "width", "height", "file_size_bytes",
                         "original_filename", "context", "collection_id"}
    assert "url" not in str(item)


def test_blaettern_und_sortierung(env):
    c, ids = env
    seite = _list(c, collection_id=SAMMLUNG, limit=2, offset=1, sort="created_at").json()
    assert [i["id"] for i in seite["items"]] == [ids["a2"], ids["a3"]]
    assert seite["total"] == 3


@pytest.mark.parametrize("body,code", [
    ({"limit": 0}, "invalid_limit"), ({"limit": 501}, "invalid_limit"),
    ({"offset": -1}, "invalid_offset"), ({"sort": "filename"}, "invalid_sort"),
])
def test_ungueltige_parameter(env, body, code):
    c, _ = env
    r = _list(c, **body)
    assert r.status_code == 422 and r.json()["detail"]["code"] == code


def test_normaler_schluessel_darf_nicht(env):
    c, _ = env
    r = _list(c, key=PLAIN)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "sign_not_allowed"


def test_enger_signierschluessel_darf_nicht(env):
    c, _ = env
    assert _list(c, key=NARROW).status_code == 403


def test_fremder_mandant(env):
    c, _ = env
    r = _list(c, tenant="oneal")
    assert r.status_code == 403


def test_on_behalf_nicht(env):
    c, _ = env
    r = c.post("/storage/grants/list", headers={"X-API-KEY": SIGNER, "X-On-Behalf-Of": "a@b"},
               json={"tenant": "arkturian", "scope": ALEX})
    assert r.status_code in (400, 403)


def test_ungueltiger_scope(env):
    c, _ = env
    assert _list(c, scope="kein scope!").status_code == 422
