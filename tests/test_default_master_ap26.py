"""AP 2.6: the built-in default master is no operator once switched off, its
use is tallied without ever recording the key, and private_media is a one-way
flag on PATCH /storage/objects/{id}."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import auth  # noqa: E402
from config import settings  # noqa: E402
from models import Base, StorageObject, User  # noqa: E402
from storage import default_master_tally as T  # noqa: E402


def _system_user():
    return SimpleNamespace(email="system@api", trust_level="admin", api_key="generated-not-the-master")


def test_default_master_still_operator_until_switched(monkeypatch):
    monkeypatch.setattr(settings, "API_KEY", "Inetpass1")
    monkeypatch.setattr(auth, "tenant_key_state", lambda k, d: None)
    monkeypatch.delenv("STORAGE_DEFAULT_MASTER_UNPRIVILEGED", raising=False)
    assert auth.is_privileged_admin(_system_user(), object()) is True


def test_default_master_never_operator_when_switched(monkeypatch):
    monkeypatch.setattr(settings, "API_KEY", "Inetpass1")
    monkeypatch.setattr(auth, "tenant_key_state", lambda k, d: None)
    monkeypatch.setenv("STORAGE_DEFAULT_MASTER_UNPRIVILEGED", "1")
    assert auth.is_privileged_admin(_system_user(), object()) is False


def test_real_master_unaffected_by_switch(monkeypatch):
    monkeypatch.setattr(settings, "API_KEY", "a-real-rotated-secret")
    monkeypatch.setattr(auth, "tenant_key_state", lambda k, d: None)
    monkeypatch.setenv("STORAGE_DEFAULT_MASTER_UNPRIVILEGED", "1")
    assert auth.is_privileged_admin(_system_user(), object()) is True


# --------------------------------------------------------------------------- #
# tally
# --------------------------------------------------------------------------- #

@pytest.fixture
def tally_client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "API_KEY", "Inetpass1")
    monkeypatch.setattr(auth, "tenant_key_state", lambda k, d: None)
    monkeypatch.delenv("STORAGE_DEFAULT_MASTER_UNPRIVILEGED", raising=False)
    out = tmp_path / "tally.jsonl"
    monkeypatch.setattr(T, "TALLY_PATH", str(out))
    app = FastAPI()

    @app.get("/storage/objects/{oid}")
    def obj(oid: int):
        auth.is_privileged_admin(_system_user(), object())
        return {"ok": True}

    @app.get("/plain")
    def plain():
        return {"ok": True}

    app.add_middleware(T.DefaultMasterTally)
    return TestClient(app), out


def _flush(client):
    T._TALLY._flush(force=True)


def test_tally_counts_default_key_without_recording_it(tally_client):
    c, out = tally_client
    c.get("/storage/objects/123", headers={"X-API-KEY": "Inetpass1", "User-Agent": "probe/1"})
    c.get("/storage/objects/456", headers={"X-API-KEY": "Inetpass1", "User-Agent": "probe/1"})
    c.get("/plain", headers={"X-API-KEY": "Inetpass1", "User-Agent": "probe/1"})
    c.get("/plain", headers={"X-API-KEY": "some-other-key"})
    c.get("/plain")
    _flush(c)
    text = out.read_text()
    assert "Inetpass1" not in text and "some-other-key" not in text
    rows = [json.loads(line) for line in text.splitlines()]
    plain = {(r["path"], r["priv"]): r["n"] for r in rows}
    assert plain[("/storage/objects/{id}", False)] == 2
    assert plain[("/storage/objects/{id}", True)] == 2   # operator right was used
    assert plain[("/plain", False)] == 1
    assert ("/plain", True) not in plain
    assert all(r["ua"] == "probe/1" for r in rows)


def test_tally_silent_once_master_is_real(tally_client, monkeypatch):
    c, out = tally_client
    monkeypatch.setattr(settings, "API_KEY", "a-real-rotated-secret")
    c.get("/plain", headers={"X-API-KEY": "Inetpass1"})
    _flush(c)
    assert not out.exists() or out.read_text() == ""


# --------------------------------------------------------------------------- #
# private_media one-way
# --------------------------------------------------------------------------- #

@pytest.fixture
def patch_env(tmp_path, monkeypatch):
    eng = create_engine(f"sqlite:///{tmp_path/'t.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    u = User(email="owner@x", display_name="o", password_hash="", api_key="ko", trust_level="user", device_ids=[])
    s.add(u); s.commit()
    priv = StorageObject(object_key="p.pdf", original_filename="p.pdf", owner_user_id=u.id, tenant_id="t1",
                         is_public=False, file_size_bytes=1, mime_type="application/pdf",
                         metadata_json={"private_media": True, "note": "a"})
    plain = StorageObject(object_key="q.pdf", original_filename="q.pdf", owner_user_id=u.id, tenant_id="t1",
                          is_public=False, file_size_bytes=1, mime_type="application/pdf", metadata_json={"note": "a"})
    s.add_all([priv, plain]); s.commit()

    from storage import routes as R
    from auth import get_current_user
    from database import get_db
    from tenancy.config import get_tenant_id
    # the operator case must not matter: nobody may clear the flag
    monkeypatch.setattr(R, "_is_privileged_admin", lambda u, d: True)
    app = FastAPI()
    app.include_router(R.router, prefix="/storage")
    app.dependency_overrides[get_db] = lambda: s
    app.dependency_overrides[get_current_user] = lambda: u
    app.dependency_overrides[get_tenant_id] = lambda: "t1"
    yield SimpleNamespace(c=TestClient(app), s=s, priv=priv, plain=plain)
    s.close()


def _md(env, obj):
    env.s.expire_all()
    return env.s.get(StorageObject, obj.id).metadata_json


def test_explicit_removal_refused(patch_env):
    r = patch_env.c.patch(f"/storage/objects/{patch_env.priv.id}", json={"metadata_json": {"private_media": False}})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "private_media_one_way"
    assert _md(patch_env, patch_env.priv)["private_media"] is True


@pytest.mark.parametrize("body", [{"metadata_json": {"note": "b"}}, {"metadata_json": None}, {"metadata_json": {}}])
def test_replacing_metadata_keeps_flag(patch_env, body):
    r = patch_env.c.patch(f"/storage/objects/{patch_env.priv.id}", json=body)
    assert r.status_code == 200, r.text
    assert _md(patch_env, patch_env.priv)["private_media"] is True


def test_setting_flag_allowed(patch_env):
    r = patch_env.c.patch(f"/storage/objects/{patch_env.plain.id}", json={"metadata_json": {"note": "a", "private_media": True}})
    assert r.status_code == 200
    assert _md(patch_env, patch_env.plain)["private_media"] is True


def test_other_fields_untouched_flag(patch_env):
    r = patch_env.c.patch(f"/storage/objects/{patch_env.priv.id}", json={"title": "x"})
    assert r.status_code == 200
    assert _md(patch_env, patch_env.priv) == {"private_media": True, "note": "a"}


def test_admin_key_gate_follows_switch(monkeypatch):
    from admin import routes as A
    monkeypatch.delenv("AI_INTERNAL_API_KEY", raising=False)
    monkeypatch.delenv("ADMIN_API_KEYS", raising=False)
    monkeypatch.setattr(A.settings, "API_KEY", "Inetpass1")
    monkeypatch.delenv("STORAGE_DEFAULT_MASTER_UNPRIVILEGED", raising=False)
    assert A.require_admin_key("Inetpass1") == "master"
    monkeypatch.setenv("STORAGE_DEFAULT_MASTER_UNPRIVILEGED", "1")
    with pytest.raises(Exception):
        A.require_admin_key("Inetpass1")
    monkeypatch.setattr(A.settings, "API_KEY", "real-secret")
    assert A.require_admin_key("real-secret") == "master"


# ── Befund 137 (#3072): Adresse nicht aus dem linken X-Forwarded-For ─────────
def _tally_lauf(monkeypatch, headers, peer):
    import asyncio

    async def innen(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    mw = T.DefaultMasterTally(innen)
    monkeypatch.setattr(mw, "_active", lambda: True)
    monkeypatch.setattr(mw, "_flush", lambda force=False: None)
    monkeypatch.delenv("TRUSTED_PROXY_IPS", raising=False)
    scope = {"type": "http", "method": "GET", "path": "/plain", "client": (peer, 1234),
             "headers": [(b"x-api-key", T.DEFAULT_LITERAL.encode())] + [(k.encode(), v.encode()) for k, v in headers]}

    async def empfangen():
        return {"type": "http.request", "body": b""}

    async def senden(_):
        return None
    asyncio.run(mw(scope, empfangen, senden))
    return [b[1] for b in mw._counts]


def test_tally_nimmt_den_vom_proxy_angehaengten_eintrag(monkeypatch):
    assert _tally_lauf(monkeypatch, [("x-forwarded-for", "198.51.100.9, 203.0.113.7")], "127.0.0.1") == ["203.0.113.7"]


def test_tally_direkte_verbindung_ignoriert_xff(monkeypatch):
    assert _tally_lauf(monkeypatch, [("x-forwarded-for", "198.51.100.9")], "203.0.113.7") == ["203.0.113.7"]


def test_tally_unsinn_und_x_real_ip(monkeypatch):
    assert _tally_lauf(monkeypatch, [("x-forwarded-for", "a;b"), ("x-real-ip", "203.0.113.7")], "127.0.0.1") == ["203.0.113.7"]
