"""sign_scopes: ein Signierschlüssel, der nur genau benannte Scopes signieren darf (Post 5764)."""
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

DAILY = "swfme-daily:arkturian-daily-bild"
FREMD = "cloud-session:Business"
NARROW = "k-narrow"
WIDE = "k-wide"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_SIGN_SECRETS", '{"k1": "' + "x" * 40 + '"}')
    monkeypatch.setenv("STORAGE_SIGN_ACTIVE_KID", "k1")
    models._hwm_ready.clear()
    eng = create_engine(f"sqlite:///{tmp_path/'s.db'}")
    models.Base.metadata.create_all(eng, tables=[StorageObject.__table__, MediaGrant.__table__, TenantAPIKey.__table__])
    S = sessionmaker(bind=eng)
    s = S()
    s.add_all([
        TenantAPIKey(api_key=NARROW, tenant_id="arkturian", label="swfme daily", is_active=True,
                     sign_tenants="arkturian", sign_scopes=DAILY),
        TenantAPIKey(api_key=WIDE, tenant_id="arkturian", label="wide", is_active=True, sign_tenants="arkturian"),
    ])
    priv = {"tenant_id": "arkturian", "private_media": True}
    ref = StorageObject(object_key="ref", tenant_id="arkturian", is_public=False, metadata_json=priv)
    fremd = StorageObject(object_key="fremd", tenant_id="arkturian", is_public=False, metadata_json=priv)
    s.add_all([ref, fremd]); s.commit()
    s.add(MediaGrant(object_id=ref.id, scope=DAILY, granted_by_user_id=1, granted_via="admin"))
    s.add(MediaGrant(object_id=fremd.id, scope=FREMD, granted_by_user_id=1, granted_via="upload"))
    s.commit()
    ids = (ref.id, fremd.id)
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


def _sign(c, key, scope, ids):
    return c.post("/storage/sign", headers={"X-API-KEY": key},
                  json={"tenant": "arkturian", "scope": scope, "ids": list(ids), "ttl": 60})


def test_narrow_key_signs_its_scope(env):
    c, (ref, _) = env
    r = _sign(c, NARROW, DAILY, [ref])
    assert r.status_code == 200
    assert r.json()["items"][0]["signed"] is True


def test_narrow_key_cannot_sign_unrelated_scope_with_valid_grant(env):
    """Negativtest: das fremde Objekt HAT eine gültige Freigabe für FREMD, der enge Schlüssel bekommt sie trotzdem nicht."""
    c, (_, fremd) = env
    assert _sign(c, WIDE, FREMD, [fremd]).json()["items"][0]["signed"] is True  # Freigabe ist echt
    r = _sign(c, NARROW, FREMD, [fremd])
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "scope_not_allowed"


def test_narrow_key_gets_nothing_outside_its_grant(env):
    c, (_, fremd) = env
    r = _sign(c, NARROW, DAILY, [fremd])
    assert r.status_code == 200
    assert r.json()["items"][0] == {"id": fremd, "error": "not_granted"}


def test_no_wildcard(env):
    c, (ref, _) = env
    assert _sign(c, NARROW, "swfme-daily:arkturian-daily-bild2", [ref]).status_code == 403


@pytest.mark.parametrize("path,body", [
    ("/storage/grants/move", {"from_scope": FREMD, "to_scope": "cloud-session:X"}),
    ("/storage/grants/share", {"object_id": 1, "from_scope": DAILY, "to_scopes": [FREMD]}),
    ("/storage/grants/unshare", {"object_id": 1, "from_scope": DAILY, "scopes": [FREMD]}),
    ("/storage/grants/lookup", {"scope": FREMD, "object_ids": [1, 2]}),
])
def test_narrow_key_cannot_manage_grants(env, path, body):
    c, _ = env
    r = c.post(path, headers={"X-API-KEY": NARROW}, json={"tenant": "arkturian", **body})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "grant_management_not_allowed"


def test_wide_key_unchanged(env):
    c, (ref, fremd) = env
    assert _sign(c, WIDE, DAILY, [ref]).json()["items"][0]["signed"] is True
    assert c.post("/storage/grants/lookup", headers={"X-API-KEY": WIDE},
                  json={"tenant": "arkturian", "scope": FREMD, "object_ids": [fremd]}).status_code == 200


@pytest.mark.parametrize("blank", ["", "   ", " , ,"])
def test_blank_restriction_fails_closed(env, blank):
    """Nur NULL heißt unbeschränkt. Eine leere Einschränkung signiert gar nichts, auch keinen gültigen fremden Scope."""
    c, (ref, fremd) = env
    from sqlalchemy import update
    db = next(main.app.dependency_overrides[get_db]())
    db.execute(update(TenantAPIKey).where(TenantAPIKey.api_key == NARROW).values(sign_scopes=blank))
    db.commit(); db.close()
    for scope, oid in ((FREMD, fremd), (DAILY, ref)):
        r = _sign(c, NARROW, scope, [oid])
        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "scope_not_allowed"
    r = c.post("/storage/grants/lookup", headers={"X-API-KEY": NARROW},
               json={"tenant": "arkturian", "scope": FREMD, "object_ids": [fremd]})
    assert r.status_code == 403
