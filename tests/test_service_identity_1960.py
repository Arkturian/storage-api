"""#1960 variant 3: system@api is an operator only through the real master.
Service keys that act as system@api (MCP gateway, cloud-api) keep ownership
of its uploads but are never operators; the public literal dies at step 5."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import auth  # noqa: E402
import config  # noqa: E402
from models import Base, StorageObject, Tenant, TenantAPIKey, User  # noqa: E402

LITERAL = "Inetpass1"
MASTER = "fresh-master-0123456789abcdef0123456789"
GATEWAY = "gw-key-0123456789abcdef0123456789abcd"
CLOUDAPI = "ca-key-0123456789abcdef0123456789abcd"


@pytest.fixture
def db(tmp_path, monkeypatch):
    eng = create_engine(f"sqlite:///{tmp_path/'t.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    S = sessionmaker(bind=eng)
    s = S()
    s.add(Tenant(id="arkturian", display_name="Arkturian"))
    s.add(User(email="system@api", display_name="System", password_hash="", api_key="random-users-key",
               trust_level="admin", device_ids=[]))
    s.add_all([
        TenantAPIKey(api_key=GATEWAY, tenant_id="arkturian", label="mcp-gateway", is_active=True,
                     acts_as_email="system@api"),
        TenantAPIKey(api_key=CLOUDAPI, tenant_id="arkturian", label="cloud-api", is_active=True,
                     acts_as_email="system@api"),
        TenantAPIKey(api_key=LITERAL, tenant_id="arkturian", label="default", is_active=True),
    ])
    s.commit()
    yield S
    s.close()


def _rotate(monkeypatch, value=MASTER):
    monkeypatch.setattr(config.settings, "API_KEY", value)
    monkeypatch.setattr(auth.settings, "API_KEY", value)


def _who(S, key):
    s = S()
    u = auth.verify_api_key(key, s)
    return s, u


def test_new_master_is_operator(db, monkeypatch):
    _rotate(monkeypatch)
    s, u = _who(db, MASTER)
    assert u.email == "system@api" and auth.is_privileged_admin(u, s) is True


@pytest.mark.parametrize("key", [GATEWAY, CLOUDAPI])
def test_service_keys_are_system_api_but_not_operator(db, monkeypatch, key):
    _rotate(monkeypatch)
    s, u = _who(db, key)
    assert u.email == "system@api"
    assert auth.is_privileged_admin(u, s) is False


def test_users_api_key_of_system_is_not_operator(db, monkeypatch):
    _rotate(monkeypatch)
    s, u = _who(db, "random-users-key")
    assert u.email == "system@api" and auth.is_privileged_admin(u, s) is False


def test_literal_after_step5_is_401(db, monkeypatch):
    _rotate(monkeypatch)
    s = db()
    s.query(TenantAPIKey).filter(TenantAPIKey.api_key == LITERAL).update({"is_active": False})
    s.commit()
    assert auth.verify_api_key(LITERAL, s) is None


def test_literal_row_left_active_is_never_operator(db, monkeypatch):
    """Step 4 forgotten: the literal would fall back to a plain tenant key."""
    _rotate(monkeypatch)
    s, u = _who(db, LITERAL)
    assert u is not None and u.email != "system@api" and auth.is_privileged_admin(u, s) is False


def test_service_key_before_rotation_matches_today(db, monkeypatch):
    _rotate(monkeypatch, LITERAL)
    s, u = _who(db, GATEWAY)
    assert u.email == "system@api" and auth.is_privileged_admin(u, s) is False
    from storage.routes import _is_public_default_master
    assert _is_public_default_master(u) is True  # same restrictions as the literal today


def test_deactivated_service_key_authenticates_nobody(db, monkeypatch):
    _rotate(monkeypatch)
    s = db()
    s.query(TenantAPIKey).filter(TenantAPIKey.api_key == GATEWAY).update({"is_active": False})
    s.commit()
    assert auth.verify_api_key(GATEWAY, s) is None


def test_gateway_keeps_owner_access_over_the_route(db, monkeypatch, tmp_path):
    """assets_download reads metadata as owner: a private agent upload of
    system@api stays readable with the gateway key, and the operator-only
    tenant listing does not."""
    _rotate(monkeypatch)
    from database import get_db
    from storage import routes as R
    s = db()
    sys_id = s.query(User).filter(User.email == "system@api").one().id
    other = User(email="human@x", display_name="h", password_hash="", api_key="kh", trust_level="user", device_ids=[])
    s.add(other); s.commit()
    mine = StorageObject(object_key="u1_a", tenant_id="arkturian", owner_user_id=sys_id, is_public=False,
                         metadata_json={"private_media": True}, original_filename="a.pdf", mime_type="application/pdf", file_size_bytes=1)
    foreign = StorageObject(object_key="u2_b", tenant_id="arkturian", owner_user_id=other.id, is_public=False,
                            original_filename="b.pdf", mime_type="application/pdf", file_size_bytes=1)
    s.add_all([mine, foreign]); s.commit()
    ids = (mine.id, foreign.id)
    s.close()

    def _db():
        d = db()
        try:
            yield d
        finally:
            d.close()
    app = FastAPI()
    app.include_router(R.router, prefix="/storage")
    app.dependency_overrides[get_db] = _db
    c = TestClient(app)
    h = {"X-API-KEY": GATEWAY}
    assert c.get(f"/storage/objects/{ids[0]}", headers=h).status_code == 200
    assert c.get(f"/storage/objects/{ids[1]}", headers=h).status_code in (403, 404)
    hm = {"X-API-KEY": MASTER}
    assert c.get(f"/storage/objects/{ids[1]}", headers=hm).status_code == 200


TRANSCODER = "tc-key-0123456789abcdef0123456789abcd"


def test_hls_attach_right_is_narrow(db, monkeypatch):
    """transcoding's key may attach an HLS result to a foreign video (the
    owner guard of #2915 lets it through) and is still no operator."""
    _rotate(monkeypatch)
    from database import get_db
    from storage import routes as R
    s = db()
    s.add(TenantAPIKey(api_key=TRANSCODER, tenant_id="arkturian", label="transcoding", is_active=True,
                       acts_as_email="system@api", may_attach_hls=True))
    human = User(email="video-owner@x", display_name="v", password_hash="", api_key="kv", trust_level="user", device_ids=[])
    s.add(human); s.commit()
    video = StorageObject(object_key="u9_v.mp4", tenant_id="arkturian", owner_user_id=human.id, is_public=False,
                          original_filename="v.mp4", mime_type="video/mp4", file_size_bytes=1)
    s.add(video); s.commit()
    vid = video.id
    s.close()

    def _db():
        d = db()
        try:
            yield d
        finally:
            d.close()
    app = FastAPI()
    app.include_router(R.router, prefix="/storage")
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[R.get_tenant_id] = lambda: "arkturian"
    c = TestClient(app)

    def attach(key):
        return c.post("/storage/upload", headers={"X-API-KEY": key},
                      files={"file": ("hls.zip", b"PK\x05\x06" + b"\x00" * 18, "application/zip")},
                      data={"reference_id": str(vid), "hls_result": "true"})
    refused = attach(GATEWAY)
    assert refused.status_code in (404, 500) and "not found" in refused.text.lower()
    passed = attach(TRANSCODER)
    assert "not found" not in passed.text.lower(), passed.text
    s2, u = _who(db, TRANSCODER)
    assert auth.is_privileged_admin(u, s2) is False


AIAPI = "ai-key-0123456789abcdef0123456789abcd"


@pytest.mark.parametrize("key,allowed", [(GATEWAY, False), (CLOUDAPI, False), ("random-users-key", False),
                                         (AIAPI, True), (MASTER, True)])
def test_owner_email_needs_operator_or_may_set_owner(db, monkeypatch, tmp_path, key, allowed):
    """owner_email used to need only trust_level admin, which every key acting
    as system@api inherits. Now only the master or a may_set_owner key."""
    _rotate(monkeypatch)
    from database import get_db
    from storage import routes as R
    from storage.service import generic_storage
    monkeypatch.setattr(generic_storage, "upload_root", tmp_path)
    for attr in ("media_dir", "thumbnails_dir", "webview_dir"):
        (tmp_path / attr).mkdir()
        monkeypatch.setattr(generic_storage, attr, tmp_path / attr)
    s = db()
    s.add(TenantAPIKey(api_key=AIAPI, tenant_id="arkturian", label="api-ai", is_active=True,
                       acts_as_email="system@api", may_set_owner=True))
    s.commit(); s.close()

    def _db():
        d = db()
        try:
            yield d
        finally:
            d.close()
    app = FastAPI()
    app.include_router(R.router, prefix="/storage")
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[R.get_tenant_id] = lambda: "arkturian"
    r = TestClient(app).post("/storage/upload", headers={"X-API-KEY": key},
                             files={"file": ("a.txt", b"x", "text/plain")},
                             data={"owner_email": "someone-else@x", "ai_mode": "none"})
    if allowed:
        assert "Only admin may set owner_email" not in r.text, r.text
    else:
        assert r.status_code in (403, 500) and "Only admin may set owner_email" in r.text, r.text


def test_literal_master_keeps_owner_email_until_step3(db, monkeypatch):
    """Before rotation the literal is the master path: api-ai keeps working."""
    _rotate(monkeypatch, LITERAL)
    from storage import routes as R
    s, u = _who(db, LITERAL)
    assert R._may_set_owner_email(u, LITERAL, s) is True
    s2, g = _who(db, GATEWAY)
    assert R._may_set_owner_email(g, GATEWAY, s2) is False
