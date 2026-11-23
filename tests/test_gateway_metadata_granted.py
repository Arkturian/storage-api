"""The MCP gateway, narrowed to one agent, reads the metadata of an object bound
to that agent although another account owns it (Project Canvas, 23.11.2026:
project-uploader owns the files, agents read them with assets_download)."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from models import Base, MediaGrant, StorageObject, User  # noqa: E402
from storage.grant_restrict import GrantScopeRestrict  # noqa: E402

ALPHA = {"X-Grant-Scope-Restrict": "cloud-session:Alpha"}
BETA = {"X-Grant-Scope-Restrict": "cloud-session:Beta"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    eng = create_engine(f"sqlite:///{tmp_path/'t.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    S = sessionmaker(bind=eng)
    s = S()
    gateway = User(email="system@api", display_name="s", password_hash="", api_key=None, trust_level="user", device_ids=[])
    uploader = User(email="project-uploader.t1@api", display_name="p", password_hash="", api_key=None,
                    trust_level="user", device_ids=[])
    s.add_all([gateway, uploader]); s.commit()
    obj = StorageObject(object_key="a.png", original_filename="a.png", owner_user_id=uploader.id, tenant_id="t1",
                        file_size_bytes=1, mime_type="image/png", is_public=False,
                        metadata_json={"private_media": True})
    s.add(obj); s.commit()
    s.add(MediaGrant(object_id=obj.id, scope="cloud-session:Alpha", granted_by_user_id=uploader.id, granted_via="upload"))
    s.commit()

    import database
    from storage import routes as R
    from auth import get_current_user, get_current_user_optional
    from database import get_db
    from tenancy.config import get_tenant_id, get_tenant_id_optional
    monkeypatch.setattr(database, "SessionLocal", S)
    monkeypatch.setattr(R, "_is_privileged_admin", lambda u, d: False)
    monkeypatch.setattr(R, "_is_public_default_master", lambda u: False)
    state = SimpleNamespace(may_assert=True)

    def _caller():
        setattr(gateway, "_storage_may_assert_scope", state.may_assert)
        return gateway
    app = FastAPI()
    app.include_router(R.router, prefix="/storage")
    app.add_middleware(GrantScopeRestrict)
    app.dependency_overrides[get_db] = lambda: s
    app.dependency_overrides[get_current_user_optional] = _caller
    app.dependency_overrides[get_current_user] = _caller
    app.dependency_overrides[get_tenant_id_optional] = lambda: "t1"
    app.dependency_overrides[get_tenant_id] = lambda: "t1"
    yield SimpleNamespace(c=TestClient(app), oid=obj.id, state=state)
    s.close()


def test_narrowed_gateway_reads_metadata_of_a_granted_foreign_object(env):
    r = env.c.get(f"/storage/objects/{env.oid}", headers=ALPHA)
    assert r.status_code == 200, r.text
    assert r.json()["original_filename"] == "a.png"


def test_other_agent_gets_nothing(env):
    assert env.c.get(f"/storage/objects/{env.oid}", headers=BETA).status_code == 404


def test_without_restriction_the_owner_rule_holds(env):
    assert env.c.get(f"/storage/objects/{env.oid}").status_code == 403


def test_key_without_may_assert_scope_cannot_use_the_header(env):
    env.state.may_assert = False
    assert env.c.get(f"/storage/objects/{env.oid}", headers=ALPHA).status_code == 403


def test_bytes_still_need_a_signed_link(env):
    assert env.c.get(f"/storage/media/{env.oid}", headers=ALPHA).status_code in (403, 404)
