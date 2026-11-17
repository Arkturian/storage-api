"""#3230: POST /storage/sign caps an encrypted object's link at 300 s, per object."""
import sys
import time
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

SCOPE = "cloud-session:Martina-GPT"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_SIGN_SECRETS", '{"k1": "' + "x" * 40 + '"}')
    monkeypatch.setenv("STORAGE_SIGN_ACTIVE_KID", "k1")
    models._hwm_ready.clear()
    eng = create_engine(f"sqlite:///{tmp_path/'s.db'}")
    models.Base.metadata.create_all(eng, tables=[StorageObject.__table__, MediaGrant.__table__, TenantAPIKey.__table__])
    S = sessionmaker(bind=eng)
    s = S()
    s.add(TenantAPIKey(api_key="k-sign", tenant_id="arkturian", label="cloud", is_active=True, sign_tenants="arkturian",
                       may_sign_encrypted=True))
    s.add(TenantAPIKey(api_key="k-content", tenant_id="arkturian", label="content", is_active=True, sign_tenants="arkturian"))
    priv = {"private_media": True}
    enc = StorageObject(object_key="enc_a.bin", tenant_id="arkturian", is_public=False, metadata_json=priv, encrypted=True)
    plain = StorageObject(object_key="u1_b", tenant_id="arkturian", is_public=False, metadata_json=priv)
    s.add_all([enc, plain]); s.commit()
    for o in (enc, plain):
        s.add(MediaGrant(object_id=o.id, scope=SCOPE, granted_by_user_id=1, granted_via="upload"))
    s.commit()
    ids = (enc.id, plain.id)
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


def test_encrypted_capped_plain_untouched(env):
    c, (enc_id, plain_id) = env
    now = time.time()
    r = c.post("/storage/sign", headers={"X-API-KEY": "k-sign"},
               json={"tenant": "arkturian", "scope": SCOPE, "ids": [enc_id, plain_id], "ttl": 900})
    assert r.status_code == 200, r.text
    items = {i["id"]: i for i in r.json()["items"]}
    exp_enc = int(items[enc_id]["url"].split("exp=")[1].split("&")[0])
    exp_plain = int(items[plain_id]["url"].split("exp=")[1].split("&")[0])
    assert items[enc_id]["ttl"] == 300 and items[enc_id]["encrypted"] is True
    assert exp_enc <= now + 301 and exp_plain >= now + 899


def test_signing_key_without_flag_cannot_sign_encrypted(env):
    """#3230: content-api/comm-api sign every scope of the tenant; without
    may_sign_encrypted they get no link for an encrypted object (and still
    for a plain one)."""
    c, (enc_id, plain_id) = env
    r = c.post("/storage/sign", headers={"X-API-KEY": "k-content"},
               json={"tenant": "arkturian", "scope": SCOPE, "ids": [enc_id, plain_id], "ttl": 300})
    assert r.status_code == 200, r.text
    items = {i["id"]: i for i in r.json()["items"]}
    assert items[enc_id] == {"id": enc_id, "error": "encrypted_sign_not_allowed"}
    assert items[plain_id].get("signed") is True
