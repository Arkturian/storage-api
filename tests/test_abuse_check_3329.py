"""#3329 (c), Alex 18.11.: images/videos that go public or outside are
checked first (OpenAI Decisions via api-ai). Fail-closed: without a clean
verdict the file stays private. A hit locks it ("privat, fest"), revokes its
share links, blocks it for everyone but an admin and raises
storage.abuse_suspected (cloud-api turns csam into a card for Alex)."""
import asyncio
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from models import Base, ShareLink, StorageObject, TenantAPIKey, User  # noqa: E402

CLEAR = {"csam": 0.01, "sexuell_explizit": 0.02, "gewalt_missbrauch": 0.03}


def _png():
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (32, 24), (10, 120, 200)).save(b, format="PNG")
    return b.getvalue()


@pytest.fixture
def env(tmp_path, monkeypatch):
    from storage import abuse_check as A
    from storage import routes as R
    from storage import share_links as SL
    from storage.service import generic_storage
    monkeypatch.setenv("ABUSE_CHECK_ENABLED", "1")
    monkeypatch.setenv("STORAGE_SIGN_SECRETS", json.dumps({"k1": "x" * 40}))
    monkeypatch.setenv("STORAGE_SIGN_ACTIVE_KID", "k1")
    up = tmp_path / "up"
    for attr, sub in (("upload_root", ""), ("media_dir", "media"), ("thumbnails_dir", "thumbnails"), ("webview_dir", "webview")):
        p = up / sub if sub else up
        p.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(generic_storage, attr, p)
    eng = create_engine(f"sqlite:///{tmp_path/'t.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    import database
    S = sessionmaker(bind=eng)
    monkeypatch.setattr(database, "SessionLocal", S)
    s = S()
    owner = User(email="alex@x", display_name="a", password_hash="", api_key="ko", trust_level="user", device_ids=[])
    s.add(owner); s.commit()
    s.add(TenantAPIKey(api_key="k-svc", tenant_id="arkturian", label="svc", is_active=True, is_service=True))
    s.commit()
    state = {"user": True, "answer": CLEAR, "calls": 0}

    async def fake_ask(obj):
        state["calls"] += 1
        a = state["answer"]
        if isinstance(a, Exception):
            raise a
        return dict(a)

    monkeypatch.setattr(A, "_ask", fake_ask)
    events = []
    from storage import encrypted_io
    monkeypatch.setattr(encrypted_io, "emit_event", lambda k, p: events.append((k, p)))

    from auth import get_current_user, get_current_user_optional
    from database import get_db
    from tenancy.config import get_tenant_id, get_tenant_id_optional

    def _cur():
        u = s.get(User, owner.id)
        setattr(u, "_storage_on_behalf_of", True)
        return u

    app = FastAPI()
    app.include_router(R.router, prefix="/storage")
    app.include_router(SL.router, prefix="/storage")
    app.dependency_overrides[get_db] = lambda: s
    app.dependency_overrides[get_current_user] = _cur
    app.dependency_overrides[get_current_user_optional] = lambda: _cur() if state["user"] else None
    app.dependency_overrides[get_tenant_id] = lambda: "arkturian"
    app.dependency_overrides[get_tenant_id_optional] = lambda: "arkturian"
    yield SimpleNamespace(c=TestClient(app), s=s, S=S, state=state, events=events, A=A)
    s.close()


def _upload(env, data=None, name="bild.png", mime="image/png", public=True):
    r = env.c.post("/storage/upload", files={"file": (name, data or _png(), mime)},
                   data={"is_public": "true" if public else "false", "ai_mode": "none"})
    assert r.status_code == 200, r.text
    return r.json()


def _obj(env, oid):
    env.s.expire_all()
    return env.s.get(StorageObject, oid)


def test_clean_image_goes_public(env):
    j = _upload(env)
    o = _obj(env, j["id"])
    assert o.is_public is True and o.abuse_status == "clear" and env.state["calls"] == 1


def test_no_verdict_keeps_it_private(env):
    env.state["answer"] = env.A.CheckUnavailable("http 503")
    j = _upload(env)
    o = _obj(env, j["id"])
    assert o.is_public is False and o.public_pending is True and o.abuse_status == "failed"
    env.state["user"] = False
    assert env.c.get(f"/storage/media/{j['id']}").status_code == 403


def test_retry_makes_it_public_later(env):
    env.state["answer"] = env.A.CheckUnavailable("timeout")
    j = _upload(env)
    env.state["answer"] = CLEAR
    n = asyncio.get_event_loop().run_until_complete(env.A.retry_open(env.S))
    assert n == 1
    o = _obj(env, j["id"])
    assert o.is_public is True and o.public_pending is False and o.abuse_status == "clear"


def test_hit_locks_blocks_and_raises_event(env):
    env.state["answer"] = {"csam": 0.93, "sexuell_explizit": 0.9, "gewalt_missbrauch": 0.1}
    j = _upload(env)
    o = _obj(env, j["id"])
    assert o.abuse_status == "hit" and o.is_public is False and o.metadata_json.get("private_media") is True
    assert env.c.get(f"/storage/media/{j['id']}").status_code == 451          # even the owner
    kinds = [(k, p["kategorie"]) for k, p in env.events]
    assert kinds == [("abuse_suspected", "csam")]
    assert "original_filename" not in env.events[0][1] and "url" not in json.dumps(env.events[0][1])


def test_non_media_goes_public_without_check(env):
    j = _upload(env, data=b"hallo\n" * 10, name="n.txt", mime="text/plain")
    o = _obj(env, j["id"])
    assert o.is_public is True and o.abuse_status is None and env.state["calls"] == 0


def test_private_upload_never_checked(env):
    j = _upload(env, public=False)
    o = _obj(env, j["id"])
    assert o.is_public is False and o.abuse_status is None and env.state["calls"] == 0


def test_patch_public_goes_through_check(env):
    j = _upload(env, public=False)
    env.state["answer"] = env.A.CheckUnavailable("refusal")
    r = env.c.patch(f"/storage/objects/{j['id']}", json={"is_public": True})
    assert r.status_code == 200 and r.json()["is_public"] is False and r.json()["public_pending"] is True
    env.state["answer"] = CLEAR
    asyncio.get_event_loop().run_until_complete(env.A.retry_open(env.S))
    assert _obj(env, j["id"]).is_public is True


def _tok(url):
    return urlparse(url).path.split("/s/", 1)[1].split("/")[0]


def _link(env, oid):
    r = env.c.post(f"/storage/objects/{oid}/share-link", headers={"X-API-KEY": "k-svc"}, json={})
    return r


def test_share_link_of_image_waits_for_clear(env):
    j = _upload(env, public=False)
    env.state["answer"] = env.A.CheckUnavailable("503")
    r = _link(env, j["id"])
    assert r.status_code == 200 and r.json()["pruefung"] == "failed"
    tok = _tok(r.json()["url"])
    assert env.c.get(f"/storage/s/{tok}", follow_redirects=False).status_code == 404
    env.state["answer"] = CLEAR
    asyncio.get_event_loop().run_until_complete(env.A.retry_open(env.S))
    env.s.expire_all()  # the test shares one session; production opens a fresh one per request
    assert env.c.get(f"/storage/s/{tok}", follow_redirects=False).status_code == 302
    assert _obj(env, j["id"]).is_public is False          # a share link never makes it public


def test_hit_at_share_refused_and_existing_links_revoked(env):
    j = _upload(env, public=False)
    tok = _tok(_link(env, j["id"]).json()["url"])
    env.s.get(StorageObject, j["id"]).abuse_status = None   # pretend a later re-check
    env.s.commit()
    env.state["answer"] = {"csam": 0.0, "sexuell_explizit": 0.0, "gewalt_missbrauch": 0.95}
    r = _link(env, j["id"])
    assert r.status_code == 451
    assert env.s.query(ShareLink).filter(ShareLink.revoked_at.is_(None)).count() == 0
    assert env.c.get(f"/storage/s/{tok}", follow_redirects=False).status_code == 404


def test_flag_off_changes_nothing(env, monkeypatch):
    monkeypatch.setenv("ABUSE_CHECK_ENABLED", "0")
    j = _upload(env)
    o = _obj(env, j["id"])
    assert o.is_public is True and o.abuse_status is None and env.state["calls"] == 0
