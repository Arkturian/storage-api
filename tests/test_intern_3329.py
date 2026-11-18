"""#3329: Stufe "intern" — for everyone signed in to the object's tenant,
never anonymous. storage-api does not see portal cookies or agent JWTs:
cloud-api checks the reader and signs with a key flagged may_sign_intern,
which needs no grant for intern objects of its tenant."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from models import Base, MediaGrant, StorageObject, TenantAPIKey, User  # noqa: E402

CLOUD = "k-cloud-intern"     # sign_tenants + may_sign_intern
CONTENT = "k-content-intern"  # sign_tenants only
DATA = b"interne Notiz\n" * 40


@pytest.fixture
def env(tmp_path, monkeypatch):
    from storage import routes as R
    from storage.service import generic_storage
    monkeypatch.setenv("STORAGE_SIGN_SECRETS", json.dumps({"k1": "x" * 40}))
    monkeypatch.setenv("STORAGE_SIGN_ACTIVE_KID", "k1")
    up = tmp_path / "up"
    (up / "media" / "arkturian").mkdir(parents=True)
    for attr, sub in (("upload_root", ""), ("media_dir", "media"), ("thumbnails_dir", "thumbnails"), ("webview_dir", "webview")):
        p = up / sub if sub else up
        p.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(generic_storage, attr, p)
    eng = create_engine(f"sqlite:///{tmp_path/'t.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    import database
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=eng))
    s = sessionmaker(bind=eng)()
    owner = User(email="alex@x", display_name="a", password_hash="", api_key="ko", trust_level="user", device_ids=[])
    s.add(owner); s.commit()
    s.add_all([TenantAPIKey(api_key=CLOUD, tenant_id="arkturian", label="cloud", is_active=True,
                            sign_tenants="arkturian,oneal", may_sign_intern=True),
               TenantAPIKey(api_key=CONTENT, tenant_id="arkturian", label="content", is_active=True,
                            sign_tenants="arkturian")])
    objs = {}
    for k, kw in (("intern", dict(intern=True)), ("privat", {}), ("oeffentlich", dict(is_public=True)),
                  ("oneal", dict(intern=True, tenant_id="oneal")),
                  ("pm", dict(intern=True, metadata_json={"private_media": True}))):
        key = f"u{owner.id}_{k}.txt"
        tenant = kw.pop("tenant_id", "arkturian")
        (up / "media" / "arkturian" / key).write_bytes(DATA)
        o = StorageObject(object_key=key, tenant_id=tenant, owner_user_id=owner.id, is_public=kw.pop("is_public", False),
                          original_filename=f"{k}.txt", mime_type="text/plain", file_size_bytes=len(DATA),
                          file_url=f"/uploads/storage/media/arkturian/{key}",
                          metadata_json=kw.pop("metadata_json", {}), **kw)
        s.add(o); s.commit()
        objs[k] = o.id
    state = {"user": True}

    from auth import get_current_user, get_current_user_optional
    from database import get_db
    from tenancy.config import get_tenant_id, get_tenant_id_optional
    app = FastAPI()
    app.include_router(R.router, prefix="/storage")
    app.dependency_overrides[get_db] = lambda: s
    app.dependency_overrides[get_current_user] = lambda: s.get(User, owner.id)
    app.dependency_overrides[get_current_user_optional] = lambda: s.get(User, owner.id) if state["user"] else None
    app.dependency_overrides[get_tenant_id] = lambda: "arkturian"
    app.dependency_overrides[get_tenant_id_optional] = lambda: "arkturian"
    yield SimpleNamespace(c=TestClient(app), s=s, objs=objs, state=state, R=R)
    s.close()


def _sign(env, ids, key=CLOUD, tenant="arkturian", ttl=300, scope="cloud-user:7"):
    r = env.c.post("/storage/sign", headers={"X-API-KEY": key},
                   json={"tenant": tenant, "scope": scope, "ids": list(ids), "ttl": ttl})
    assert r.status_code == 200, r.text
    return {i["id"]: i for i in r.json()["items"]}


def test_intern_signed_without_grant_for_flagged_key(env):
    it = _sign(env, [env.objs["intern"]])[env.objs["intern"]]
    assert it["signed"] is True and it["intern"] is True
    got = env.c.get(urlparse(it["url"]).path + "?" + urlparse(it["url"]).query)
    assert got.status_code == 200 and got.content == DATA


def test_intern_url_stable_within_a_minute(env, monkeypatch):
    t = [1_800_000_010.0]
    monkeypatch.setattr(env.R.time, "time", lambda: t[0])
    a = _sign(env, [env.objs["intern"]])[env.objs["intern"]]["url"]
    t[0] += 40
    b = _sign(env, [env.objs["intern"]])[env.objs["intern"]]["url"]
    assert a == b
    exp = int(parse_qs(urlparse(a).query)["exp"][0])
    assert exp - t[0] >= 300 - 60


def test_key_without_flag_needs_grant(env):
    assert _sign(env, [env.objs["intern"]], key=CONTENT)[env.objs["intern"]]["error"] == "not_granted"


def test_flag_does_not_open_plain_private_objects(env):
    assert _sign(env, [env.objs["privat"]])[env.objs["privat"]]["error"] == "not_granted"


def test_flag_respects_tenant(env):
    it = _sign(env, [env.objs["oneal"]], tenant="arkturian")[env.objs["oneal"]]
    assert it["error"] == "wrong_tenant"


def test_private_media_wins_over_intern(env):
    assert _sign(env, [env.objs["pm"]])[env.objs["pm"]]["error"] == "not_granted"


def test_intern_not_anonymous_by_id(env):
    env.state["user"] = False
    assert env.c.get(f"/storage/media/{env.objs['intern']}").status_code in (401, 403)


def _patch(env, oid, **body):
    return env.c.patch(f"/storage/objects/{oid}", json=body)


def test_patch_public_to_intern_and_back(env):
    oid = env.objs["oeffentlich"]
    j = _patch(env, oid, intern=True).json()
    assert j["intern"] is True and j["is_public"] is False
    j = _patch(env, oid, is_public=True).json()
    assert j["intern"] is False and j["is_public"] is True


def test_patch_intern_and_public_together_refused(env):
    r = _patch(env, env.objs["privat"], intern=True, is_public=True)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "intern_conflict"


def test_patch_private_media_cannot_become_intern(env):
    r = _patch(env, env.objs["privat"], intern=True, metadata_json={"private_media": True})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "intern_conflict"


def test_patch_setting_private_media_drops_intern(env):
    j = _patch(env, env.objs["intern"], metadata_json={"private_media": True}).json()
    assert j["intern"] is False and j["is_public"] is False


def test_upload_intern_forces_not_public(env):
    r = env.c.post("/storage/upload", files={"file": ("n.txt", DATA, "text/plain")},
                   data={"intern": "true", "is_public": "true", "ai_mode": "none"})
    assert r.status_code == 200, r.text
    assert r.json()["intern"] is True and r.json()["is_public"] is False


def test_upload_intern_with_private_refused(env):
    r = env.c.post("/storage/upload", files={"file": ("n.txt", DATA, "text/plain")},
                   data={"intern": "true", "private": "true"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "intern_conflict"


def test_ticket_intern(env):
    r = env.c.post("/storage/upload-ticket", params={"filename": "t.txt", "intern": "true", "is_public": "true"})
    assert r.status_code == 200, r.text
    tok = r.json()["token"]
    put = env.c.put(f"/storage/upload-ticket/{tok}", content=DATA)
    assert put.status_code == 200, put.text
    assert put.json()["intern"] is True and put.json()["is_public"] is False
