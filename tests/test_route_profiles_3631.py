"""#3631: a key with a route profile reaches its listed routes and nothing else.

Walks every route the app really has, so a route added later is refused without
touching this file. A refusal must carry the gate's own code: a 403 from a
handler would mean the key got that far.
"""
import re
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
from storage import route_profiles as P  # noqa: E402

PORTAL = "k-portal-3631"
POST = "k-post-3631"
INGEST = "k-ingest-3631"
UNKNOWN = "k-unknown-3631"
AGENT = "cloud-session:Goliath"
OTHER = "cloud-session:Martin"
OWNER = "owner@example.org"

EXPECTED = {
    P.PORTAL_SIGNER: {
        ("POST", "/storage/sign"), ("POST", "/storage/grants/move"), ("POST", "/storage/grants/share"),
        ("POST", "/storage/grants/unshare"), ("POST", "/storage/grants/lookup"),
        ("POST", "/storage/objects/1/share-link"), ("GET", "/storage/objects/1/share-links"),
        ("DELETE", "/storage/objects/1/share-links/1"),
    },
    P.POST_SIGNER: {
        ("POST", "/storage/sign"), ("POST", "/storage/grants/share"), ("POST", "/storage/grants/unshare"),
        ("POST", "/storage/objects/1/grants"), ("DELETE", "/storage/objects/1/grants"),
    },
    P.INGEST: {("POST", "/storage/upload")},
}
KEY_OF = {P.PORTAL_SIGNER: PORTAL, P.POST_SIGNER: POST, P.INGEST: INGEST}


def _all_endpoints():
    out = []
    for r in main.app.routes:
        if type(r).__name__ == "_IncludedRouter":
            entries = [(c.path, c.methods) for c in r.effective_route_contexts()]
        else:
            entries = [(getattr(r, "path", ""), getattr(r, "methods", None))]
        for path, methods in entries:
            concrete = re.sub(r"\{[^}]+\}", "1", path)
            out += [(m, concrete) for m in sorted(methods or []) if m != "OPTIONS"]
    return sorted(set(out))


@pytest.fixture
def env(tmp_path, monkeypatch):
    from storage.service import generic_storage

    monkeypatch.setenv("STORAGE_SIGN_SECRETS", '{"k1": "' + "x" * 40 + '"}')
    monkeypatch.setenv("STORAGE_SIGN_ACTIVE_KID", "k1")
    monkeypatch.setattr(generic_storage, "upload_root", tmp_path / "up")
    monkeypatch.setattr(generic_storage, "media_dir", tmp_path / "up" / "media")
    (tmp_path / "up" / "media").mkdir(parents=True)
    models._hwm_ready.clear()
    eng = create_engine(f"sqlite:///{tmp_path/'s.db'}", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(eng)
    S = sessionmaker(bind=eng)
    monkeypatch.setattr(database, "SessionLocal", S)
    s = S()
    owner = User(email=OWNER, display_name="o", password_hash="", api_key="k-owner-user",
                 trust_level="user", device_ids=[])
    s.add(owner)
    s.commit()
    s.add_all([
        TenantAPIKey(api_key=PORTAL, tenant_id="david", label="portal", is_active=True, sign_tenants="david",
                     route_profile=P.PORTAL_SIGNER, may_create_share_links=True),
        TenantAPIKey(api_key=POST, tenant_id="david", label="post", is_active=True, sign_tenants="david",
                     is_service=True, may_publish_to_post=True, route_profile=P.POST_SIGNER),
        TenantAPIKey(api_key=INGEST, tenant_id="david", label="ingest", is_active=True, route_profile=P.INGEST),
        TenantAPIKey(api_key=UNKNOWN, tenant_id="david", label="x", is_active=True, sign_tenants="david",
                     route_profile="no-such-profile"),
    ])
    obj = StorageObject(object_key="o.bin", tenant_id="david", is_public=False, owner_user_id=owner.id,
                        mime_type="application/octet-stream",
                        metadata_json={"tenant_id": "david", "private_media": True})
    s.add(obj)
    s.commit()
    s.add(MediaGrant(object_id=obj.id, scope=AGENT, granted_by_user_id=owner.id, granted_via="upload"))
    s.commit()
    oid = obj.id
    s.close()

    def _db():
        d = S()
        try:
            yield d
        finally:
            d.close()

    main.app.dependency_overrides[get_db] = _db
    try:
        yield TestClient(main.app, raise_server_exceptions=False), oid, eng
    finally:
        main.app.dependency_overrides.clear()


def _snapshot(eng):
    with eng.connect() as conn:
        return {t.name: sorted(map(repr, conn.execute(t.select()).all()))
                for t in models.Base.metadata.sorted_tables}


def _count(eng, sql, **params):
    with eng.connect() as conn:
        return conn.execute(text(sql), params).scalar()


def _ingest(c, key=INGEST, headers=None, **fields):
    data = {"context": "telegram:1", "is_public": "false", "private": "true", "reuse_existing": "false",
            "original_filename": "a.bin", **fields}
    data = {k: v for k, v in data.items() if v is not None}
    return c.post("/storage/upload", headers={"X-API-KEY": key, **(headers or {})}, data=data,
                  files={"file": ("a.bin", b"payload-3631", "application/octet-stream")})


@pytest.mark.parametrize("profile", sorted(EXPECTED))
def test_profile_lets_through_exactly_its_routes(profile):
    # Also fails when a listed route was renamed and the entry no longer matches.
    assert {r for r in _all_endpoints() if P.route_allowed(profile, *r)} == EXPECTED[profile]


@pytest.mark.parametrize("profile", sorted(EXPECTED))
def test_key_is_refused_on_every_other_route(env, profile):
    c, _, eng = env
    before = _snapshot(eng)
    wrong, refused = [], 0
    for method, path in _all_endpoints():
        if (method, path) in EXPECTED[profile]:
            continue
        r = c.request(method, path, headers={"X-API-KEY": KEY_OF[profile]})
        if r.status_code == 403 and (method == "HEAD" or r.json()["detail"]["code"] == "route_profile_denied"):
            refused += 1
        else:
            wrong.append((method, path, r.status_code))
    assert wrong == []
    assert refused > 60
    assert _snapshot(eng) == before, "a refused call changed the database"


def test_unknown_profile_allows_nothing(env):
    c, oid, _ = env
    r = c.post("/storage/sign", headers={"X-API-KEY": UNKNOWN},
               json={"tenant": "david", "scope": AGENT, "ids": [oid], "ttl": 60})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "route_profile_denied"


@pytest.mark.parametrize("path", [
    "/storage/grants/lookup/", "//storage/grants/lookup", "/storage/objects/1/share-link/",
    "/storage/objects/1x/share-links", "/storage/objects/1/share-links/1/x",
])
def test_near_misses_of_a_listed_path_are_refused(env, path):
    c, _, _ = env
    r = c.post(path, headers={"X-API-KEY": PORTAL}, json={})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "route_profile_denied"


def test_forged_host_does_not_open_the_gate(env):
    c, _, _ = env
    r = c.get("/storage/kg/stats", headers={"X-API-KEY": PORTAL, "Host": "x/storage/sign?"})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "route_profile_denied"


def test_portal_signer_does_its_real_work(env):
    c, oid, eng = env
    h = {"X-API-KEY": PORTAL}
    r = c.post("/storage/sign", headers=h, json={"tenant": "david", "scope": AGENT, "ids": [oid], "ttl": 60})
    assert r.status_code == 200 and r.json()["items"][0]["signed"] is True

    r = c.post("/storage/grants/share", headers=h,
               json={"tenant": "david", "object_id": oid, "from_scope": AGENT, "to_scopes": [OTHER]})
    assert r.status_code == 200, r.text
    assert r.json()["shared"] == [OTHER]
    # Grants need a real user id: the profile's own machine account.
    assert _count(eng, "SELECT u.email FROM media_grants g JOIN users u ON u.id=g.granted_by_user_id "
                       "WHERE g.scope=:s", s=OTHER) == "portal-signer.david@api"

    r = c.post("/storage/grants/lookup", headers=h, json={"tenant": "david", "scope": OTHER, "object_ids": [oid]})
    assert r.status_code == 200, r.text
    r = c.post("/storage/grants/unshare", headers=h,
               json={"tenant": "david", "object_id": oid, "from_scope": AGENT, "scopes": [OTHER]})
    assert r.status_code == 200, r.text
    assert _count(eng, "SELECT count(*) FROM media_grants WHERE scope=:s", s=OTHER) == 0
    r = c.post("/storage/grants/move", headers=h,
               json={"tenant": "david", "from_scope": AGENT, "to_scope": OTHER})
    assert r.status_code == 200, r.text

    r = c.post(f"/storage/objects/{oid}/share-link", headers=h, json={"scope": OTHER})
    assert r.status_code == 200, r.text
    link = r.json()["id"]
    assert c.get(f"/storage/objects/{oid}/share-links", headers=h, params={"scope": OTHER}).status_code == 200
    assert c.delete(f"/storage/objects/{oid}/share-links/{link}", headers=h,
                    params={"scope": OTHER}).status_code == 200


def test_post_signer_binds_for_the_owner_and_publishes_an_agent_file(env):
    c, oid, eng = env
    h = {"X-API-KEY": POST}
    body = {"tenant": "david", "object_id": oid, "from_scope": AGENT, "to_scopes": ["content-post:7"], "reshare": False}
    assert c.post("/storage/grants/share", headers=h, json=body).status_code == 200

    obo = {**h, "X-On-Behalf-Of": OWNER}
    r = c.post(f"/storage/objects/{oid}/grants", headers=obo, json={"scope": "content-post:8"})
    assert r.status_code == 200, r.text
    assert c.delete(f"/storage/objects/{oid}/grants", headers=obo,
                    params={"scope": "content-post:8"}).status_code == 200
    assert _count(eng, "SELECT count(*) FROM media_grants WHERE scope='content-post:8'") == 0


def test_post_signer_without_on_behalf_cannot_grant(env):
    c, oid, eng = env
    r = c.post(f"/storage/objects/{oid}/grants", headers={"X-API-KEY": POST}, json={"scope": "content-post:9"})
    assert r.status_code == 404
    assert _count(eng, "SELECT count(*) FROM media_grants WHERE scope='content-post:9'") == 0


@pytest.mark.parametrize("scope", [AGENT, "comm-message:discord-17.0", f"{AGENT},comm-message:42.1"])
def test_ingest_stores_a_private_file_and_binds_it(env, scope):
    c, _, eng = env
    r = _ingest(c, grant_scope=scope)
    assert r.status_code == 200, r.text
    new = r.json()["id"]
    with eng.connect() as conn:
        row = conn.execute(text("SELECT o.is_public, o.tenant_id, u.email, o.metadata_json FROM storage_objects o "
                                "JOIN users u ON u.id=o.owner_user_id WHERE o.id=:i"), {"i": new}).one()
    assert not row[0]
    assert row[1] == "david"
    assert row[2] == "ingest.david@api"
    assert '"private_media": true' in row[3]
    for s in scope.split(","):
        assert _count(eng, "SELECT count(*) FROM media_grants WHERE object_id=:o AND scope=:s", o=new, s=s) == 1


def test_ingest_without_a_scope_is_allowed(env):
    # comm binds only when it knows the receiving agent.
    c, _, _ = env
    assert _ingest(c).status_code == 200


@pytest.mark.parametrize("fields", [
    {"is_public": "true"}, {"is_public": "1"}, {"private": "false"}, {"private": None},
    {"reuse_existing": "true"}, {"reuse_existing": None},
    {"owner_email": OWNER}, {"storage_mode": "reference"}, {"reference_path": "/etc/passwd"},
    {"external_uri": "https://example.org/x"}, {"reference_id": "1"}, {"hls_result": "true"},
    {"visibility_level": "intern"}, {"encrypted": "true"}, {"ai_mode": "vision"}, {"analyze": "true"},
    {"collection_id": "c"}, {"link_id": "l"}, {"ttl_hours": "1"}, {"metadata_json": "{}"},
    {"grant_scope": "content-post:7"}, {"grant_scope": "cloud-user:1"}, {"grant_scope": " , "},
    {"grant_scope": f"{AGENT},content-post:7"},
])
def test_ingest_refuses_everything_beyond_a_private_file(env, fields):
    c, _, eng = env
    assert _ingest(c).status_code == 200  # the machine account exists from here on
    objects = _count(eng, "SELECT count(*) FROM storage_objects")
    grants = _count(eng, "SELECT count(*) FROM media_grants")
    r = _ingest(c, **fields)
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "ingest_upload_refused"
    assert _count(eng, "SELECT count(*) FROM storage_objects") == objects
    assert _count(eng, "SELECT count(*) FROM media_grants") == grants


@pytest.mark.parametrize("header", ["X-Compute-Focal", "X-On-Behalf-Of"])
def test_ingest_refuses_extra_headers(env, header):
    c, _, eng = env
    objects = _count(eng, "SELECT count(*) FROM storage_objects")
    r = _ingest(c, headers={header: OWNER if header == "X-On-Behalf-Of" else "true"})
    assert r.status_code == 403
    assert _count(eng, "SELECT count(*) FROM storage_objects") == objects


def test_ingest_refuses_a_field_sent_twice(env):
    c, _, eng = env
    objects = _count(eng, "SELECT count(*) FROM storage_objects")
    r = c.post("/storage/upload", headers={"X-API-KEY": INGEST},
               data={"private": ["false", "true"], "is_public": "false", "reuse_existing": "false"},
               files={"file": ("a.bin", b"x", "application/octet-stream")})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "ingest_upload_refused"
    assert _count(eng, "SELECT count(*) FROM storage_objects") == objects


def test_upload_without_a_profile_is_not_narrowed(env):
    c, _, _ = env
    r = c.post("/storage/upload", headers={"X-API-KEY": "k-owner-user"},
               data={"context": "x", "original_filename": "b.bin"},
               files={"file": ("b.bin", b"plain", "application/octet-stream")})
    assert r.status_code == 200, r.text


def test_key_value_is_never_copied_and_dies_with_its_row(env):
    c, oid, eng = env
    h = {"X-API-KEY": PORTAL}
    body = {"tenant": "david", "scope": AGENT, "ids": [oid], "ttl": 60}
    assert c.post("/storage/sign", headers=h, json=body).status_code == 200
    assert _count(eng, "SELECT count(*) FROM users WHERE api_key=:k", k=PORTAL) == 0
    assert _count(eng, "SELECT api_key FROM users WHERE email='portal-signer.david@api'") is None
    with eng.begin() as conn:
        conn.execute(text("DELETE FROM tenant_api_keys WHERE api_key=:k"), {"k": PORTAL})
    assert c.post("/storage/sign", headers=h, json=body).status_code in (401, 403)
    assert c.post("/storage/grants/lookup", headers=h,
                  json={"tenant": "david", "scope": AGENT, "object_ids": [oid]}).status_code == 401


def test_machine_account_cannot_be_impersonated(env):
    c, oid, _ = env
    assert c.post("/storage/sign", headers={"X-API-KEY": PORTAL},
                  json={"tenant": "david", "scope": AGENT, "ids": [oid], "ttl": 60}).status_code == 200
    r = c.post(f"/storage/objects/{oid}/grants",
               headers={"X-API-KEY": POST, "X-On-Behalf-Of": "portal-signer.david@api"},
               json={"scope": "content-post:9"})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "principal_not_allowed"


def test_key_without_profile_keeps_its_routes(env):
    c, _, _ = env
    assert c.get("/storage/kg/stats", headers={"X-API-KEY": "k-owner-user"}).status_code != 403


def test_migration_adds_the_column(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path/'old.db'}")
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE tenant_api_keys (api_key VARCHAR PRIMARY KEY, tenant_id VARCHAR, "
                          "label VARCHAR, is_active BOOLEAN, created_at DATETIME, last_used_at DATETIME)"))
        conn.execute(text("INSERT INTO tenant_api_keys (api_key, tenant_id, is_active) VALUES ('k', 'david', 1)"))
        database.migrate_tenant_api_keys(conn)
        database.migrate_tenant_api_keys(conn)   # idempotent
        assert conn.execute(text("SELECT route_profile FROM tenant_api_keys")).scalar() is None
