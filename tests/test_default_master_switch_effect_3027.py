"""#3027: what STORAGE_DEFAULT_MASTER_UNPRIVILEGED=1 changes for the callers the
usage tally found (16.11.2026) — pinned before the switch is flipped.

- knowledge-api lists ?collection_id=FloraFauna&mine=false with the default key:
  it keeps its own rows (system@api) and foreign public rows, loses foreign
  private rows.
- the MCP gateway and history_export read their own objects with the default
  key: GET /storage/objects/{id} of an own private object stays 200.
"""
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

DEFAULT = "Inetpass1"


@pytest.fixture
def env(tmp_path, monkeypatch):
    import auth
    import config
    monkeypatch.setattr(config.settings, "API_KEY", DEFAULT)
    monkeypatch.setattr(auth.settings, "API_KEY", DEFAULT)
    eng = create_engine(f"sqlite:///{tmp_path/'t.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    system = User(email="system@api", display_name="s", password_hash="", api_key=DEFAULT,
                  trust_level="admin", device_ids=[])
    other = User(email="human@x", display_name="h", password_hash="", api_key="kh",
                 trust_level="user", device_ids=[])
    s.add_all([system, other]); s.commit()

    def mk(key, owner, **kw):
        o = StorageObject(object_key=f"{key}.jpg", original_filename=f"{key}.jpg", owner_user_id=owner.id,
                          tenant_id="arkturian", file_size_bytes=1, mime_type="image/jpeg", **kw)
        s.add(o); s.commit()
        return o
    objs = SimpleNamespace(
        own_priv=mk("own_priv", system, is_public=False, collection_id="FloraFauna"),
        own_pub=mk("own_pub", system, is_public=True, collection_id="FloraFauna"),
        foreign_pub=mk("foreign_pub", other, is_public=True, collection_id="FloraFauna"),
        foreign_priv=mk("foreign_priv", other, is_public=False, collection_id="FloraFauna"),
    )
    from storage import routes as R
    from auth import get_current_user, get_current_user_optional
    from database import get_db
    from tenancy.config import get_tenant_id, get_tenant_id_optional
    app = FastAPI()
    app.include_router(R.router, prefix="/storage")
    app.dependency_overrides[get_db] = lambda: s
    def _master():  # what verify_api_key returns for the master key (#1960)
        u = s.get(User, system.id)
        u._storage_auth_via = "master"
        u._storage_privileged_admin = None
        return u
    app.dependency_overrides[get_current_user_optional] = _master
    app.dependency_overrides[get_current_user] = _master
    app.dependency_overrides[get_tenant_id_optional] = lambda: "arkturian"
    app.dependency_overrides[get_tenant_id] = lambda: "arkturian"
    yield SimpleNamespace(c=TestClient(app), s=s, o=objs)
    s.close()


def _florafauna(env):
    r = env.c.get("/storage/list", params={"collection_id": "FloraFauna", "limit": 500, "mine": "false"})
    assert r.status_code == 200
    body = r.json()
    items = body["items"] if isinstance(body, dict) else body
    return {i["original_filename"] for i in items}


def test_before_switch_operator_sees_all(env, monkeypatch):
    monkeypatch.delenv("STORAGE_DEFAULT_MASTER_UNPRIVILEGED", raising=False)
    assert _florafauna(env) == {"own_priv.jpg", "own_pub.jpg", "foreign_pub.jpg", "foreign_priv.jpg"}


def test_after_switch_knowledge_loses_only_foreign_private(env, monkeypatch):
    monkeypatch.setenv("STORAGE_DEFAULT_MASTER_UNPRIVILEGED", "1")
    assert _florafauna(env) == {"own_priv.jpg", "own_pub.jpg", "foreign_pub.jpg"}


def test_after_switch_own_private_metadata_still_readable(env, monkeypatch):
    monkeypatch.setenv("STORAGE_DEFAULT_MASTER_UNPRIVILEGED", "1")
    assert env.c.get(f"/storage/objects/{env.o.own_priv.id}").status_code == 200


def test_after_switch_foreign_private_metadata_closed(env, monkeypatch):
    monkeypatch.setenv("STORAGE_DEFAULT_MASTER_UNPRIVILEGED", "1")
    assert env.c.get(f"/storage/objects/{env.o.foreign_priv.id}").status_code in (403, 404)
