"""#3029 (Befund 104): private_media beats is_public on every read path,
tombstoned and expired objects are gone everywhere, and a signed read of a
private object does not hand out the permanent HLS path."""
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from models import Base, StorageObject, User  # noqa: E402

SRC = (Path(__file__).resolve().parent.parent / "storage" / "routes.py").read_text()
MAIN = (Path(__file__).resolve().parent.parent / "main.py").read_text()


@pytest.fixture
def env(tmp_path, monkeypatch):
    eng = create_engine(f"sqlite:///{tmp_path/'t.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    owner = User(email="owner@x", display_name="o", password_hash="", api_key="ko", trust_level="user", device_ids=[])
    other = User(email="other@x", display_name="t", password_hash="", api_key="kt", trust_level="user", device_ids=[])
    s.add_all([owner, other]); s.commit()

    def mk(key, **kw):
        o = StorageObject(object_key=f"{key}.jpg", original_filename=f"{key}.jpg", owner_user_id=owner.id,
                          tenant_id="t1", file_size_bytes=1, mime_type="image/jpeg", **kw)
        s.add(o); s.commit()
        return o
    objs = SimpleNamespace(
        pub_priv=mk("pub_priv", is_public=True, metadata_json={"private_media": True}),
        pub=mk("pub", is_public=True),
        tomb=mk("tomb", is_public=True, tombstoned_at=datetime.utcnow()),
        expired=mk("expired", is_public=True, expires_at=datetime.utcnow() - timedelta(hours=1)),
    )
    from storage import routes as R
    from auth import get_current_user, get_current_user_optional
    from database import get_db
    from tenancy.config import get_tenant_id, get_tenant_id_optional
    monkeypatch.setattr(R, "_is_privileged_admin", lambda u, d: False)
    state = {"user": other}
    app = FastAPI()
    app.include_router(R.router, prefix="/storage")
    app.dependency_overrides[get_db] = lambda: s
    app.dependency_overrides[get_current_user_optional] = lambda: state["user"]
    app.dependency_overrides[get_current_user] = lambda: state["user"]
    app.dependency_overrides[get_tenant_id_optional] = lambda: "t1"
    app.dependency_overrides[get_tenant_id] = lambda: "t1"
    yield SimpleNamespace(c=TestClient(app), s=s, o=objs, state=state, owner=owner, other=other, R=R)
    s.close()


def test_objects_private_media_beats_is_public(env):
    r = env.c.get(f"/storage/objects/{env.o.pub_priv.id}")
    assert r.status_code == 403
    assert "ocr_text" not in r.text


def test_objects_owner_still_reads_private(env):
    env.state["user"] = env.owner
    assert env.c.get(f"/storage/objects/{env.o.pub_priv.id}").status_code == 200


@pytest.mark.parametrize("name", ["tomb", "expired"])
def test_objects_gone(env, name):
    env.state["user"] = env.owner
    assert env.c.get(f"/storage/objects/{getattr(env.o, name).id}").status_code == 404


def test_objects_plain_public_unchanged(env):
    assert env.c.get(f"/storage/objects/{env.o.pub.id}").status_code == 200


@pytest.mark.parametrize("name,ok", [("pub", True), ("pub_priv", False), ("tomb", False), ("expired", False)])
def test_public_route(env, name, ok):
    r = env.c.get(f"/storage/objects/{getattr(env.o, name).id}/public")
    assert (r.status_code == 200) == ok


@pytest.mark.parametrize("name,ok", [("pub", True), ("pub_priv", False), ("tomb", False), ("expired", False)])
def test_row_visible_for_stranger(env, name, ok):
    assert env.R._row_visible(getattr(env.o, name), env.other, env.s) is ok


def test_row_visible_owner_private(env):
    assert env.R._row_visible(env.o.pub_priv, env.owner, env.s) is True
    assert env.R._row_visible(env.o.tomb, env.owner, env.s) is False


def test_similar_drops_private_media(env, monkeypatch):
    ids = {k: getattr(env.o, k).id for k in ("pub_priv", "pub", "tomb", "expired")}
    query = env.o.pub.id

    async def fake_find(object_id, limit, where=None, tenant_id=None):
        return [{"object_id": i, "distance": 0.1} for k, i in ids.items() if i != query]
    stub = SimpleNamespace(kg_pipeline=SimpleNamespace(
        find_similar_objects=fake_find, get_stats=lambda tenant_id=None: {"total_embeddings": 0}))
    monkeypatch.setitem(sys.modules, "knowledge_graph.pipeline", stub)
    r = env.c.get(f"/storage/similar/{query}", headers={"X-API-KEY": "kt"})
    assert r.status_code == 200, r.text
    assert r.json()["similar_objects"] == []


def test_kg_searches_use_row_visible():
    assert SRC.count("if not _row_visible(so, current_user, db):") == 2


def test_hls_header_suppressed_on_signed_private():
    assert "not (_signed and _hls_private)" in SRC
    assert "_head_signed and (not obj.is_public or _md_priv.get(\"private_media\"))" in MAIN


def test_head_checks_tombstone_and_expiry():
    assert 'getattr(obj, "tombstoned_at", None) is not None or _expired(obj)' in MAIN


def test_hls_url_uses_object_tenant():
    assert "build_hls_url(storage_obj.tenant_id or tenant_id, basename)" in SRC
    assert "build_hls_url(obj.tenant_id or tenant_id, basename)" in SRC


def test_default_master_owner_keeps_metadata_of_private(env, monkeypatch):
    # regression 15.11.: assets_download reads metadata with the gateway key
    env.state["user"] = env.owner
    monkeypatch.setattr(env.R, "_is_public_default_master", lambda u: True)
    assert env.c.get(f"/storage/objects/{env.o.pub_priv.id}").status_code == 200


def test_default_master_still_no_bytes_gate(env, monkeypatch):
    monkeypatch.setattr(env.R, "_is_public_default_master", lambda u: True)
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        env.R._check_presence_and_privacy(env.o.pub_priv, env.owner, env.s)
