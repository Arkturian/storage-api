"""#4007: behind the MCP gateway, tenant A sees nothing of tenant B and
nothing of the operator.

The gateway uses one key of tenant arkturian (owner system@api) for every
caller. Before #4007 a narrowed agent of another tenant listed every public
object of system@api as "mine" and could rewrite objects the gateway account
owns. Counter-probes: the operator's own agents and the superadmin keep
their view; a key without may_assert_scope gets no wall from the headers.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import database  # noqa: E402
import main  # noqa: E402
import models  # noqa: E402
from database import get_db  # noqa: E402
from models import MediaGrant, StorageObject, TenantAPIKey, User  # noqa: E402

GATEWAY = "k-gateway-4007"
PLAIN = "k-plain-4007"
CLOUD = "k-cloud-4007"
LINK = "wall4007"

# name -> (origin_tenant, is_public, private_media, visibility_level)
ROWS = {
    "op_pub": (None, True, False, None),
    "op_priv": (None, False, True, None),
    "ark_pub": ("arkturian", True, False, None),
    "les_pub": ("lesser", True, False, None),
    "les_priv_blender": ("lesser", False, True, None),
    "les_priv_other": ("lesser", False, True, None),
    "edera_pub": ("edera", True, False, None),
    "edera_priv": ("edera", False, True, None),
    "op_shared_to_blender": (None, False, True, None),
    "op_intern": (None, False, False, "intern"),
}
GRANTS = {"les_priv_blender": "cloud-session:Blender", "op_shared_to_blender": "cloud-session:Blender",
          "edera_priv": "cloud-session:EderaBot"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    from storage.service import generic_storage

    monkeypatch.setattr(generic_storage, "upload_root", tmp_path / "up")
    monkeypatch.setattr(generic_storage, "media_dir", tmp_path / "up" / "media")
    (tmp_path / "up" / "media").mkdir(parents=True)
    models._hwm_ready.clear()
    eng = create_engine(f"sqlite:///{tmp_path/'s.db'}", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(eng)
    S = sessionmaker(bind=eng)
    monkeypatch.setattr(database, "SessionLocal", S)
    s = S()
    system = User(email="system@api", display_name="s", password_hash="", api_key=None, trust_level="user", device_ids=[])
    plain = User(email="plain@example.org", display_name="p", password_hash="", api_key=None, trust_level="user", device_ids=[])
    s.add_all([system, plain])
    s.commit()
    s.add_all([
        TenantAPIKey(api_key=GATEWAY, tenant_id="arkturian", label="mcp-gateway", is_active=True,
                     acts_as_email="system@api", may_assert_scope=True),
        TenantAPIKey(api_key=PLAIN, tenant_id="arkturian", label="plain service", is_active=True,
                     acts_as_email="system@api", may_assert_scope=False),
        TenantAPIKey(api_key=CLOUD, tenant_id="arkturian", label="cloud-api portal", is_active=True,
                     acts_as_email="system@api", may_assert_origin=True),
    ])
    s.commit()
    ids = {}
    for i, (name, (origin, public, pm, level)) in enumerate(ROWS.items()):
        o = StorageObject(object_key=f"k/{name}", original_filename=f"{name}.png", file_url=f"/u/{name}.png",
                          mime_type="image/png", file_size_bytes=10, checksum=f"c{i}", is_public=public,
                          owner_user_id=system.id, tenant_id="arkturian", context="t", link_id=LINK,
                          metadata_json={"private_media": True} if pm else {}, origin_tenant=origin,
                          visibility_level=level)
        s.add(o)
        s.flush()
        ids[name] = o.id
    for name, scope in GRANTS.items():
        s.add(MediaGrant(object_id=ids[name], scope=scope, granted_by_user_id=system.id))
    s.commit()
    s.close()

    def _db():
        d = S()
        try:
            yield d
        finally:
            d.close()

    main.app.dependency_overrides[get_db] = _db
    try:
        yield TestClient(main.app, raise_server_exceptions=False), eng, ids
    finally:
        main.app.dependency_overrides.clear()


def agent(name, tenant, key=GATEWAY):
    h = {"X-API-KEY": key, "X-Grant-Scope-Restrict": f"cloud-session:{name}"}
    if tenant:
        h["X-Caller-Tenant"] = tenant
        h["X-Grant-Scope-Tenants"] = tenant
    return h


def listed(c, headers, ids, **params):
    r = c.get("/storage/list", headers=headers, params={"limit": 500, **params})
    assert r.status_code == 200, r.text
    names = {v: k for k, v in ids.items()}
    return {names[i["id"]] for i in r.json()["items"] if i["id"] in names}


def test_lesser_agent_lists_only_its_tenant_its_grants_and_intern(env):
    c, _, ids = env
    assert listed(c, agent("Blender", "lesser"), ids) == {
        "les_pub", "les_priv_blender", "op_shared_to_blender", "op_intern"}


def test_grouping_filter_does_not_widen_the_wall(env):
    c, _, ids = env
    got = listed(c, agent("Blender", "lesser"), ids, link_id=LINK, mine="false")
    assert got <= {"les_pub", "les_priv_blender", "op_shared_to_blender", "op_intern"}
    assert not got & {"op_pub", "ark_pub", "edera_pub", "op_priv", "edera_priv"}


def test_tenant_b_sees_nothing_of_a_or_the_operator(env):
    c, _, ids = env
    assert listed(c, agent("EderaBot", "edera"), ids) == {"edera_pub", "edera_priv", "op_intern"}


def test_missing_caller_tenant_fails_closed(env):
    c, _, ids = env
    assert listed(c, agent("Blender", None), ids) <= {"les_priv_blender", "op_shared_to_blender", "op_intern"}


def test_operator_agent_keeps_its_view(env):
    """Counter-probe: own tenant, no wall; #3030 still hides others' private rows."""
    c, _, ids = env
    got = listed(c, agent("Cloud", "arkturian"), ids)
    assert {"op_pub", "ark_pub", "les_pub", "edera_pub", "op_intern"} <= got
    assert not got & {"op_priv", "les_priv_blender", "edera_priv"}


def test_superadmin_without_restriction_sees_all(env):
    c, _, ids = env
    got = listed(c, {"X-API-KEY": GATEWAY, "X-Caller-Tenant": "lesser"}, ids)
    assert got == set(ROWS)


def test_headers_from_a_plain_key_build_no_wall(env):
    """Counter-probe: only the gateway key turns X-Caller-Tenant into a wall."""
    c, _, ids = env
    got = listed(c, agent("Blender", "lesser", key=PLAIN), ids)
    assert {"op_pub", "ark_pub", "edera_pub"} <= got


def test_asset_refs_respect_the_wall(env):
    c, _, ids = env
    r = c.get("/storage/asset-refs", headers=agent("Blender", "lesser"), params={"link_id": LINK})
    assert r.status_code == 200, r.text
    names = {v: k for k, v in ids.items()}
    got = {names[i["id"]] for i in r.json().get("results", []) if i.get("id") in names}
    assert "les_pub" in got
    assert not got & {"op_pub", "ark_pub", "edera_pub", "op_priv", "edera_priv"}


def test_read_by_id_stays_what_anyone_may_read(env):
    c, _, ids = env
    h = agent("Blender", "lesser")
    assert c.get(f"/storage/objects/{ids['op_pub']}").status_code == 200  # keyless, as before
    assert c.get(f"/storage/objects/{ids['op_pub']}", headers=h).status_code == 200
    assert c.get(f"/storage/objects/{ids['op_priv']}", headers=h).status_code == 404
    assert c.get(f"/storage/objects/{ids['edera_priv']}", headers=h).status_code == 404
    assert c.get(f"/storage/objects/{ids['op_shared_to_blender']}", headers=h).status_code == 200


def _exists(eng, oid):
    with eng.connect() as conn:
        return conn.execute(text("SELECT tombstoned_at IS NULL FROM storage_objects WHERE id=:i"),
                            {"i": oid}).scalar()


def test_foreign_agent_cannot_change_operator_or_shared_objects(env):
    c, eng, ids = env
    h = agent("Blender", "lesser")
    for name in ("op_pub", "ark_pub", "edera_pub", "op_shared_to_blender", "op_intern"):
        r = c.delete(f"/storage/objects/{ids[name]}", headers=h)
        assert r.status_code == 404, (name, r.status_code, r.text)
        assert _exists(eng, ids[name]), name
        r = c.put(f"/storage/objects/{ids[name]}/embedding-text", headers=h, json={"embedding_text": "x"})
        assert r.status_code == 404, (name, r.status_code)


def test_foreign_agent_changes_its_own_tenants_object(env):
    c, eng, ids = env
    r = c.delete(f"/storage/objects/{ids['les_pub']}", headers=agent("Blender", "lesser"))
    assert r.status_code == 200, r.text


def test_operator_agent_may_still_change_public_operator_objects(env):
    """Counter-probe: the wall changes nothing inside the operator tenant."""
    c, _, ids = env
    r = c.delete(f"/storage/objects/{ids['op_pub']}", headers=agent("Cloud", "arkturian"))
    assert r.status_code == 200, r.text


def _upload(c, headers, name, **extra):
    return c.post("/storage/upload", headers=headers,
                  data={"context": "t", "private": "true", "reuse_existing": "false", **extra},
                  files={"file": (name, b"payload-4007-" + name.encode(), "application/octet-stream")})


def _origin(eng, oid):
    with eng.connect() as conn:
        return conn.execute(text("SELECT origin_tenant FROM storage_objects WHERE id=:i"), {"i": oid}).scalar()


def test_cloud_portal_key_marks_the_humans_tenant(env):
    c, eng, _ = env
    r = _upload(c, {"X-API-KEY": CLOUD, "X-Caller-Tenant": "lesser"}, "portal.bin")
    assert r.status_code == 200, r.text
    assert _origin(eng, r.json()["id"]) == "lesser"
    assert r.json()["origin_tenant"] == "lesser"  # cloud-api checks this fail-closed


def test_plain_key_cannot_mark_a_tenant(env):
    c, eng, _ = env
    r = _upload(c, {"X-API-KEY": PLAIN, "X-Caller-Tenant": "lesser"}, "plain.bin")
    assert r.status_code == 200, r.text  # ignored, not refused
    assert _origin(eng, r.json()["id"]) is None
    assert r.json()["origin_tenant"] is None


def test_origin_key_gets_no_wall_and_no_extra_reads(env):
    """may_assert_origin only marks; with restrict headers it is narrowed like any key."""
    c, _, ids = env
    got = listed(c, agent("Blender", "lesser", key=CLOUD), ids)
    assert not got & {"op_priv", "edera_priv"}
    assert {"op_pub", "edera_pub"} <= got


def test_lesser_upload_through_gateway_is_listed_to_lesser_only(env):
    c, _, ids = env
    # as the gateway sends it: the agent's own scope is bound on upload
    r = _upload(c, agent("Blender", "lesser"), "mine.bin", grant_scope="cloud-session:Blender")
    assert r.status_code == 200, r.text
    new = r.json()["id"]
    lst = c.get("/storage/list", headers=agent("Blender", "lesser"), params={"limit": 500}).json()["items"]
    assert new in {i["id"] for i in lst}
    lst = c.get("/storage/list", headers=agent("EderaBot", "edera"), params={"limit": 500}).json()["items"]
    assert new not in {i["id"] for i in lst}


def test_member_of_the_operator_tenant_is_no_stranger(env):
    """Counter-probe: a person with memberships lesser and arkturian keeps the operator view."""
    c, _, ids = env
    h = {"X-API-KEY": GATEWAY, "X-Grant-Scope-Restrict": "mcp-gateway:ohne storage-scope",
         "X-Caller-Tenant": "lesser", "X-Grant-Scope-Tenants": "lesser,arkturian"}
    assert {"op_pub", "ark_pub", "edera_pub"} <= listed(c, h, ids)


def test_tenants_header_of_a_stranger_does_not_open_the_wall(env):
    c, _, ids = env
    h = {"X-API-KEY": GATEWAY, "X-Grant-Scope-Restrict": "mcp-gateway:ohne storage-scope",
         "X-Caller-Tenant": "lesser", "X-Grant-Scope-Tenants": "lesser"}
    assert not listed(c, h, ids) & {"op_pub", "ark_pub", "edera_pub"}


def test_keyless_read_does_not_show_the_origin_tenant(env):
    c, _, ids = env
    r = c.get(f"/storage/objects/{ids['les_pub']}")
    assert r.status_code == 200 and r.json().get("origin_tenant") is None
    r = c.get(f"/storage/objects/{ids['les_pub']}", headers=agent("Blender", "lesser"))
    assert r.json().get("origin_tenant") == "lesser"
