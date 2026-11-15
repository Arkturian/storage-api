"""#2998 (Befund 84): grouping filters on /storage/list (collection_id,
collection_like, link_id) no longer switch the owner filter off, and
/storage/similar no longer treats an e-mail domain (@gmail.com) as a tenant."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from models import Base, StorageObject, User  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    eng = create_engine(f"sqlite:///{tmp_path/'t.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    alice = User(email="alice@gmail.com", display_name="a", password_hash="", api_key="ka", trust_level="user", device_ids=[])
    bob = User(email="bob@gmail.com", display_name="b", password_hash="", api_key="kb", trust_level="user", device_ids=[])
    op = User(email="op@x", display_name="o", password_hash="", api_key="ko", trust_level="admin", device_ids=[])
    s.add_all([alice, bob, op]); s.commit()
    rows = [
        ("a_priv", alice, False, "C", "L", None),
        ("a_pub", alice, True, "C", "L", None),
        ("b_priv", bob, False, "C", "L", None),
        ("b_pub", bob, True, "C", "L", None),
        ("b_pub_private_media", bob, True, "C", "L", {"private_media": 1}),
        ("b_pub_hidden", bob, True, "cloud-uploads", None, None),
    ]
    for key, owner, pub, col, link, meta in rows:
        s.add(StorageObject(object_key=f"{key}.jpg", original_filename=f"{key}.jpg", owner_user_id=owner.id,
                            tenant_id="t1", is_public=pub, collection_id=col, link_id=link, metadata_json=meta,
                            mime_type="image/jpeg", file_size_bytes=1))
    s.commit()

    from storage import routes as R
    from auth import get_current_user, get_current_user_optional
    from database import get_db
    from tenancy.config import get_tenant_id, get_tenant_id_optional
    import auth

    state = {"user": alice}
    monkeypatch.setattr(R, "_is_privileged_admin", lambda u, d: u is not None and u.email == "op@x")
    monkeypatch.setattr(auth, "is_privileged_admin", lambda u, d: u is not None and u.email == "op@x")
    app = FastAPI()
    app.include_router(R.router, prefix="/storage")
    app.dependency_overrides[get_db] = lambda: s
    app.dependency_overrides[get_current_user_optional] = lambda: state["user"]
    app.dependency_overrides[get_current_user] = lambda: state["user"]
    app.dependency_overrides[get_tenant_id_optional] = lambda: "t1"
    app.dependency_overrides[get_tenant_id] = lambda: "t1"
    yield SimpleNamespace(c=TestClient(app), state=state, s=s, alice=alice, bob=bob, op=op, R=R)
    s.close()


def _keys(r):
    assert r.status_code == 200, r.text
    return sorted(i["original_filename"].rsplit(".", 1)[0] for i in r.json()["items"])


@pytest.mark.parametrize("params", [
    {"collection_like": "%"},
    {"collection_like": "C"},
    {"collection_id": "C"},
    {"link_id": "L"},
    {"collection_id": "C", "mine": "false"},
])
def test_grouping_never_shows_foreign_private(env, params):
    got = _keys(env.c.get("/storage/list", params=params))
    assert "b_priv" not in got
    assert "b_pub_private_media" not in got
    assert {"a_priv", "a_pub"} <= set(got)   # own rows, private included
    assert "b_pub" in got                    # foreign public stays listed


def test_wildcard_keeps_hidden_collections_foreign_only(env):
    got = _keys(env.c.get("/storage/list", params={"collection_like": "%"}))
    assert "b_pub_hidden" not in got
    env.state["user"] = env.bob
    assert "b_pub_hidden" in _keys(env.c.get("/storage/list", params={"collection_like": "%"}))


def test_foreign_rows_lose_owner(env):
    items = env.c.get("/storage/list", params={"collection_id": "C"}).json()["items"]
    by = {i["original_filename"]: i for i in items}
    assert by["b_pub.jpg"]["owner_email"] is None and by["b_pub.jpg"]["owner_user_id"] is None
    assert by["a_priv.jpg"]["owner_email"] == "alice@gmail.com"


def test_operator_still_sees_everything(env):
    env.state["user"] = env.op
    got = _keys(env.c.get("/storage/list", params={"collection_like": "%"}))
    assert {"a_priv", "b_priv", "b_pub_private_media", "b_pub_hidden"} <= set(got)


def test_without_grouping_unchanged(env):
    assert _keys(env.c.get("/storage/list")) == ["a_priv", "a_pub"]
    assert _keys(env.c.get("/storage/list", params={"mine": "false"})) == ["a_priv", "a_pub"]


def test_anonymous_unchanged(env):
    env.state["user"] = None
    got = _keys(env.c.get("/storage/list", params={"collection_id": "C"}))
    assert got == ["a_pub", "b_pub"]


def test_similar_no_freemail_conflation(env, monkeypatch):
    ids = {o.original_filename.rsplit(".", 1)[0]: o.id for o in env.s.query(StorageObject).all()}
    seen = {}

    async def fake_find(object_id, limit, where=None, tenant_id=None):
        seen["where"] = where
        return [{"object_id": i, "distance": 0.1} for k, i in ids.items() if k != "a_pub"]
    stub = SimpleNamespace(kg_pipeline=SimpleNamespace(
        find_similar_objects=fake_find, get_stats=lambda tenant_id=None: {"total_embeddings": 0}))
    monkeypatch.setitem(sys.modules, "knowledge_graph.pipeline", stub)
    r = env.c.get(f"/storage/similar/{ids['a_pub']}", headers={"X-API-KEY": "ka"})
    assert r.status_code == 200, r.text
    got = sorted(o["original_filename"].rsplit(".", 1)[0] for o in r.json()["similar_objects"])
    assert "b_priv" not in got            # bob is @gmail.com too — not the same tenant
    assert "a_priv" in got and "b_pub" in got
    assert "gmail.com" not in str(seen["where"])
