"""#3030 (Befund 105): X-Grant-Scope-Restrict narrows a request (MCP gateway,
one agent) to plainly public rows plus rows granted to that scope; /fetch
takes private=true."""
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

H = {"X-Grant-Scope-Restrict": "cloud-session:Alpha"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    eng = create_engine(f"sqlite:///{tmp_path/'t.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    S = sessionmaker(bind=eng)
    s = S()
    system = User(email="system@api", display_name="s", password_hash="", api_key="k", trust_level="user", device_ids=[])
    s.add(system); s.commit()

    def mk(key, **kw):
        o = StorageObject(object_key=f"{key}.jpg", original_filename=f"{key}.jpg", owner_user_id=system.id,
                          tenant_id="t1", file_size_bytes=1, mime_type="image/jpeg", collection_id="C", **kw)
        s.add(o); s.commit()
        return o
    o = SimpleNamespace(
        alpha=mk("alpha", is_public=False, metadata_json={"private_media": True}),
        beta=mk("beta", is_public=False, metadata_json={"private_media": True}),
        nonpub=mk("nonpub", is_public=False),
        pub=mk("pub", is_public=True),
        pub_priv=mk("pub_priv", is_public=True, metadata_json={"private_media": True}),
    )
    s.add(MediaGrant(object_id=o.alpha.id, scope="cloud-session:Alpha", granted_by_user_id=system.id))
    s.add(MediaGrant(object_id=o.beta.id, scope="cloud-session:Beta", granted_by_user_id=system.id))
    s.commit()

    import database
    from storage import routes as R
    from auth import get_current_user, get_current_user_optional
    from database import get_db
    from tenancy.config import get_tenant_id, get_tenant_id_optional
    monkeypatch.setattr(database, "SessionLocal", S)
    monkeypatch.setattr(R, "_is_privileged_admin", lambda u, d: False)
    # the gateway is the owner of everything: exactly the #3030 shape
    monkeypatch.setattr(R, "_is_public_default_master", lambda u: False)
    app = FastAPI()
    app.include_router(R.router, prefix="/storage")
    app.add_middleware(GrantScopeRestrict)
    app.dependency_overrides[get_db] = lambda: s
    app.dependency_overrides[get_current_user_optional] = lambda: system
    app.dependency_overrides[get_current_user] = lambda: system
    app.dependency_overrides[get_tenant_id_optional] = lambda: "t1"
    app.dependency_overrides[get_tenant_id] = lambda: "t1"
    yield SimpleNamespace(c=TestClient(app), s=s, o=o)
    s.close()


def _names(r):
    assert r.status_code == 200, r.text
    return sorted(i["original_filename"].rsplit(".", 1)[0] for i in r.json()["items"])


def test_list_unrestricted_owner_sees_all(env):
    assert _names(env.c.get("/storage/list")) == ["alpha", "beta", "nonpub", "pub", "pub_priv"]


def test_list_restricted_to_grant(env):
    assert _names(env.c.get("/storage/list", headers=H)) == ["alpha", "pub"]
    assert _names(env.c.get("/storage/list", params={"collection_id": "C"}, headers=H)) == ["alpha", "pub"]


@pytest.mark.parametrize("name,code", [("alpha", 200), ("beta", 404), ("nonpub", 404), ("pub", 200), ("pub_priv", 404)])
def test_object_routes_restricted(env, name, code):
    oid = getattr(env.o, name).id
    assert env.c.get(f"/storage/objects/{oid}", headers=H).status_code == code


@pytest.mark.parametrize("path", ["/storage/objects/{id}/embedding-text", "/storage/objects/{id}/annotations",
                                  "/storage/objects/{id}/processing-status"])
def test_foreign_object_subroutes_404(env, path):
    r = env.c.get(path.format(id=env.o.beta.id), headers=H)
    assert r.status_code == 404


def test_foreign_object_mutations_404(env):
    oid = env.o.beta.id
    assert env.c.patch(f"/storage/objects/{oid}", json={"title": "x"}, headers=H).status_code == 404
    assert env.c.put(f"/storage/objects/{oid}/embedding-text", json={"text": "x"}, headers=H).status_code == 404
    assert env.c.delete(f"/storage/objects/{oid}", headers=H).status_code == 404
    env.s.expire_all()
    assert env.s.get(StorageObject, oid) is not None


def test_header_without_value_is_no_restriction(env):
    r = env.c.get(f"/storage/objects/{env.o.beta.id}", headers={"X-Grant-Scope-Restrict": " "})
    assert r.status_code == 200


def test_row_visible_honours_scope(env):
    from storage import grant_restrict as G
    token = G._scope.set("cloud-session:Alpha")
    try:
        from storage import routes as R
        assert R._row_visible(env.o.alpha, None, env.s) is False  # not public, anonymous
        assert G.allowed(env.o.alpha, env.s) and not G.allowed(env.o.beta, env.s)
    finally:
        G._scope.reset(token)


def test_fetch_accepts_private():
    from storage.routes import RemoteFetchRequest
    assert RemoteFetchRequest(url="https://x.example/a.png", private=True).private is True
