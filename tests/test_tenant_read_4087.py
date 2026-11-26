"""#4087 (Alex 26.11.): an upload belongs to its tenant, every agent of that
tenant reads it, other tenants only with an explicit share or when it is
public. Public stays findable for everyone, "mine" shows only the own tenant,
and changes stay with the grant and the own tenant (edeb544).

Counter-probes in every test: the other tenant (edera) gets 404, the operator
tenant keeps the per-grant rule of #3030, encrypted objects never open by
tenant, and a key without the gateway / signing flags opens nothing.
"""
import json
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

GATEWAY = "k-gateway-4087"
PLAIN = "k-plain-4087"
SIGNER = "k-cloud-sign-4087"
SIGNER_NO_LEVEL = "k-content-sign-4087"

# name -> (origin_tenant, is_public, private_media, encrypted)
ROWS = {
    "les_by_admin": ("lesser", False, True, False),   # r-cubic admin's screenshot
    "les_pub": ("lesser", True, False, False),
    "les_enc": ("lesser", False, True, True),
    "edera_priv": ("edera", False, True, False),
    "op_pub": ("arkturian", True, False, False),
    "op_priv": ("arkturian", False, True, False),     # another operator agent's file
    "legacy_priv": (None, False, True, False),        # no origin recorded
}
GRANTS = {"les_by_admin": "cloud-session-x:" + "r-cubic admin".encode().hex(),
          "les_enc": "cloud-session-x:" + "r-cubic admin".encode().hex(),
          "edera_priv": "cloud-session:EderaBot",
          "op_priv": "cloud-session:Business"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    from storage.service import generic_storage

    monkeypatch.setenv("STORAGE_SIGN_SECRETS", json.dumps({"k1": "x" * 40}))
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
    system = User(email="system@api", display_name="s", password_hash="", api_key=None, trust_level="user", device_ids=[])
    s.add(system)
    s.commit()
    s.add_all([
        TenantAPIKey(api_key=GATEWAY, tenant_id="arkturian", label="mcp-gateway", is_active=True,
                     acts_as_email="system@api", may_assert_scope=True),
        TenantAPIKey(api_key=PLAIN, tenant_id="arkturian", label="plain service", is_active=True,
                     acts_as_email="system@api", may_assert_scope=False),
        TenantAPIKey(api_key=SIGNER, tenant_id="arkturian", label="cloud-api signing", is_active=True,
                     sign_tenants="arkturian", may_sign_intern=True),
        TenantAPIKey(api_key=SIGNER_NO_LEVEL, tenant_id="arkturian", label="content signing", is_active=True,
                     sign_tenants="arkturian", may_sign_intern=False),
    ])
    s.commit()
    ids = {}
    for i, (name, (origin, public, pm, enc)) in enumerate(ROWS.items()):
        o = StorageObject(object_key=f"k/{name}", original_filename=f"{name}.png", file_url=f"/u/{name}.png",
                          mime_type="image/png", file_size_bytes=10, checksum=f"c{i}", is_public=public,
                          owner_user_id=system.id, tenant_id="arkturian", context="cloud-session-upload",
                          metadata_json={"private_media": True} if pm else {}, origin_tenant=origin,
                          encrypted=enc)
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


def agent(name, tenant, key=GATEWAY, scope=None):
    h = {"X-API-KEY": key, "X-Grant-Scope-Restrict": scope or f"cloud-session:{name}"}
    if tenant:
        h["X-Caller-Tenant"] = tenant
        h["X-Grant-Scope-Tenants"] = tenant
    return h


ADMIN = agent("r-cubic admin", "lesser", scope="cloud-session-x:" + "r-cubic admin".encode().hex())
SOLIDWORKS = agent("SolidWorks", "lesser")
EDERA = agent("EderaBot", "edera")
OPERATOR = agent("Cloud", "arkturian")


def get(c, oid, h):
    return c.get(f"/storage/objects/{oid}", headers=h).status_code


def listed(c, headers, ids, **params):
    r = c.get("/storage/list", headers=headers, params={"limit": 500, **params})
    assert r.status_code == 200, r.text
    names = {v: k for k, v in ids.items()}
    return {names[i["id"]] for i in r.json()["items"] if i["id"] in names}


def sign(c, ids, readers, key=SIGNER, scope="cloud-session:SolidWorks"):
    body = {"tenant": "arkturian", "scope": scope, "ids": ids, "ttl": 300}
    if readers is not None:
        body["leser_mandanten"] = list(readers)
    r = c.post("/storage/sign", headers={"X-API-KEY": key}, json=body)
    assert r.status_code == 200, r.text
    return {i["id"]: i for i in r.json()["items"]}


# ── Read by id (gateway, assets_get) ─────────────────────────────────────────

def test_other_agent_of_the_tenant_reads_the_upload(env):
    c, _, ids = env
    assert get(c, ids["les_by_admin"], ADMIN) == 200      # the bound agent, as before
    assert get(c, ids["les_by_admin"], SOLIDWORKS) == 200  # #4087: any agent of lesser


def test_other_tenant_gets_404(env):
    c, _, ids = env
    assert get(c, ids["les_by_admin"], EDERA) == 404
    assert get(c, ids["edera_priv"], SOLIDWORKS) == 404


def test_public_reads_for_every_tenant(env):
    c, _, ids = env
    for h in (SOLIDWORKS, EDERA):
        assert get(c, ids["op_pub"], h) == 200
        assert get(c, ids["les_pub"], h) == 200


def test_operator_tenant_keeps_the_per_grant_rule(env):
    """Counter-probe: Cloud (arkturian) does not read Business's private file."""
    c, _, ids = env
    assert get(c, ids["op_priv"], OPERATOR) == 404
    assert get(c, ids["op_priv"], agent("Business", "arkturian")) == 200


def test_encrypted_and_unmarked_objects_do_not_open_by_tenant(env):
    c, _, ids = env
    assert get(c, ids["les_enc"], SOLIDWORKS) == 404
    assert get(c, ids["legacy_priv"], SOLIDWORKS) == 404


def test_tenant_headers_from_a_plain_key_open_nothing(env):
    c, _, ids = env
    assert get(c, ids["les_by_admin"], agent("SolidWorks", "lesser", key=PLAIN)) == 404


def test_share_to_another_tenant_opens_it(env):
    c, eng, ids = env
    assert get(c, ids["les_by_admin"], EDERA) == 404
    with eng.begin() as conn:
        conn.execute(text("INSERT INTO media_grants (object_id, scope, granted_by_user_id) VALUES (:o, :s, 1)"),
                     {"o": ids["les_by_admin"], "s": "cloud-session:EderaBot"})
    assert get(c, ids["les_by_admin"], EDERA) == 200


# ── Changes stay with the grant and the own tenant ──────────────────────────

def _alive(eng, oid):
    with eng.connect() as conn:
        return conn.execute(text("SELECT tombstoned_at IS NULL FROM storage_objects WHERE id=:i"),
                            {"i": oid}).scalar()


def test_lesser_cannot_change_an_operator_object(env):
    c, eng, ids = env
    r = c.put(f"/storage/objects/{ids['op_pub']}/embedding-text", headers=SOLIDWORKS,
              json={"embedding_text": "x"})
    assert r.status_code == 404
    assert c.delete(f"/storage/objects/{ids['op_pub']}", headers=SOLIDWORKS).status_code == 404
    assert _alive(eng, ids["op_pub"])


def test_tenant_read_is_no_licence_to_delete_a_colleagues_file(env):
    """Reading opens by tenant; deleting r-cubic admin's file still needs his binding."""
    c, eng, ids = env
    assert c.delete(f"/storage/objects/{ids['les_by_admin']}", headers=SOLIDWORKS).status_code == 404
    assert _alive(eng, ids["les_by_admin"])


# ── Listing: mine = own tenant, beyond mine public is findable ─────────────

def test_mine_shows_only_the_own_tenant(env):
    c, _, ids = env
    assert listed(c, SOLIDWORKS, ids) == {"les_by_admin", "les_pub"}
    assert listed(c, EDERA, ids) == {"edera_priv"}


def test_beyond_mine_public_is_findable(env):
    c, _, ids = env
    got = listed(c, SOLIDWORKS, ids, mine="false")
    assert {"op_pub", "les_pub", "les_by_admin"} <= got
    assert not got & {"op_priv", "edera_priv", "les_enc", "legacy_priv"}


# ── Bytes: /storage/sign with leser_mandanten (cloud-api media-urls) ────────

def test_sign_for_a_reader_of_the_tenant(env):
    c, _, ids = env
    it = sign(c, [ids["les_by_admin"]], ["lesser"])[ids["les_by_admin"]]
    assert it.get("signed") is True and "sig=" in it["url"]
    assert it.get("origin_tenant") == "lesser"


def test_sign_refuses_other_readers_and_missing_proof(env):
    c, _, ids = env
    oid = ids["les_by_admin"]
    assert sign(c, [oid], ["edera"])[oid]["error"] == "not_granted"
    assert sign(c, [oid], None)[oid]["error"] == "not_granted"           # no verified readers
    assert sign(c, [oid], ["lesser"], key=SIGNER_NO_LEVEL)[oid]["error"] == "not_granted"


def test_sign_keeps_operator_and_encrypted_closed(env):
    c, _, ids = env
    assert sign(c, [ids["op_priv"]], ["arkturian"])[ids["op_priv"]]["error"] == "not_granted"
    assert sign(c, [ids["les_enc"]], ["lesser"])[ids["les_enc"]]["error"] == "not_granted"


def test_sign_with_grant_carries_no_tenant_marker(env):
    """The bound agent's own link looks as before."""
    c, _, ids = env
    scope = "cloud-session-x:" + "r-cubic admin".encode().hex()
    it = sign(c, [ids["les_by_admin"]], ["lesser"], scope=scope)[ids["les_by_admin"]]
    assert it.get("signed") is True and "origin_tenant" not in it
