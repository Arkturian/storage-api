"""#3329: the MCP gateway names the narrowed agent's verified tenants
(X-Grant-Scope-Tenants). Believed only from a key flagged may_assert_scope
and only with X-Grant-Scope-Restrict; then personal objects of those tenants
and intern objects of any tenant pass the grant restriction, so metadata and
lists work before assets_download fetches its signed link."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from models import Base, StorageObject, TenantAPIKey, User  # noqa: E402

GW = "k-gateway-3329"
OTHER = "k-other-3329"
AGENT = "cloud-session:Probe"


@pytest.fixture
def env(tmp_path, monkeypatch):
    from storage import grant_restrict
    from storage import routes as R
    eng = create_engine(f"sqlite:///{tmp_path/'t.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    import database
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=eng))
    s = sessionmaker(bind=eng)()
    system = User(email="system@api", display_name="s", password_hash="", api_key="rnd", trust_level="admin", device_ids=[])
    human = User(email="mensch@x", display_name="m", password_hash="", api_key="rnd2", trust_level="user", device_ids=[])
    s.add_all([system, human]); s.commit()
    s.add_all([TenantAPIKey(api_key=GW, tenant_id="arkturian", label="mcp-gateway", is_active=True,
                            acts_as_email="system@api", may_assert_scope=True),
               TenantAPIKey(api_key=OTHER, tenant_id="arkturian", label="knowledge", is_active=True,
                            acts_as_email="system@api")])
    ids = {}
    for k, tenant, level, owner in (("pers_ark", "arkturian", "personal", human.id),
                                    ("pers_ark_sys", "arkturian", "personal", system.id),
                                    ("pers_ed", "edera", "personal", human.id),
                                    ("int_ed", "edera", "intern", human.id),
                                    ("priv_ark", "arkturian", None, human.id)):
        o = StorageObject(object_key=k, tenant_id=tenant, owner_user_id=owner, is_public=False,
                          original_filename=f"{k}.png", mime_type="image/png", file_size_bytes=1,
                          metadata_json={}, visibility_level=level)
        s.add(o); s.commit()
        ids[k] = o.id
    from auth import get_current_user, get_current_user_optional
    from database import get_db
    from tenancy.config import get_tenant_id, get_tenant_id_optional

    def _cur():
        u = s.get(User, system.id)
        setattr(u, "_storage_auth_via", "service:mcp-gateway")
        return u

    app = FastAPI()
    app.include_router(R.router, prefix="/storage")
    app.dependency_overrides[get_db] = lambda: s
    app.dependency_overrides[get_current_user] = _cur
    app.dependency_overrides[get_current_user_optional] = _cur
    app.dependency_overrides[get_tenant_id] = lambda: "arkturian"
    app.dependency_overrides[get_tenant_id_optional] = lambda: "arkturian"
    yield SimpleNamespace(c=TestClient(grant_restrict.GrantScopeRestrict(app)), ids=ids)
    s.close()


def _get(env, k, key=GW, tenants="arkturian", restrict=AGENT):
    h = {"X-API-KEY": key}
    if restrict:
        h["X-Grant-Scope-Restrict"] = restrict
    if tenants is not None:
        h["X-Grant-Scope-Tenants"] = tenants
    return env.c.get(f"/storage/objects/{env.ids[k]}", headers=h).status_code


def test_agent_of_the_tenant_reads_personal(env):
    assert _get(env, "pers_ark") == 200
    assert _get(env, "pers_ark_sys") == 200


def test_intern_of_another_tenant_readable(env):
    assert _get(env, "int_ed") == 200


def test_personal_of_another_tenant_not(env):
    assert _get(env, "pers_ed") == 404


def test_agent_of_edera_does_not_read_personal_arkturian(env):
    assert _get(env, "pers_ark", tenants="edera") in (403, 404)
    assert _get(env, "pers_ark_sys", tenants="edera") == 404      # even where the gateway is owner


def test_header_from_other_key_ignored(env):
    assert _get(env, "pers_ark", key=OTHER) in (403, 404)
    assert _get(env, "int_ed", key=OTHER) == 404


def test_header_without_restrict_ignored(env):
    assert _get(env, "pers_ark", restrict=None) == 403           # plain rules: not owner


@pytest.mark.parametrize("bad", ["Arkturian", "ark turian", "a/b", ",".join(f"t{i}" for i in range(101)), ""])
def test_malformed_tenants_ignored(env, bad):
    assert _get(env, "pers_ark", tenants=bad) in (403, 404)


def test_private_object_stays_closed(env):
    assert _get(env, "priv_ark") in (403, 404)


def test_list_shows_level_objects_for_the_agent(env):
    h = {"X-API-KEY": GW, "X-Grant-Scope-Restrict": AGENT, "X-Grant-Scope-Tenants": "arkturian"}
    got = {x["id"] for x in env.c.get("/storage/list", params={"mine": "true", "limit": 50}, headers=h).json()["items"]}
    assert env.ids["pers_ark_sys"] in got and env.ids["priv_ark"] not in got
