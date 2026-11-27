"""#4157 C (Cloud 27.11.2026): the cloud-node profile for cloud-api on a node
outside the core (studio). Replaces the instance key there, which acted as
system@api and owned every agent attachment.

Private and encrypted attachments bound to agents, history exports
(is_public=false), reading and deleting its own objects. Nothing public, no
foreign objects. The walk over every other route is in test_route_profiles_3631.py.
"""
import base64
import secrets
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from models import StorageObject, User  # noqa: E402
from tests.test_route_profiles_3631 import AGENT, CLOUD, OWNER, _count, env  # noqa: E402,F401

ACCOUNT = "cloud-node.david@api"
H = {"X-API-KEY": CLOUD}


def _attachment(c, headers=None, filename="anhang.pdf", **fields):
    # cloud-api backend/app/main.py _upload_to_storage, private attachment (48e39a29).
    data = {"context": "cloud-upload", "is_public": "false", "collection_id": "cloud-uploads",
            "ai_mode": "none", "ttl_hours": "720", "reuse_existing": "false", "private": "true",
            "grant_scope": AGENT, **fields}
    data = {k: v for k, v in data.items() if v is not None}
    return c.post("/storage/upload", headers={**H, **(headers or {})}, data=data,
                  files={"file": (filename, b"%PDF-1.4 x", "application/pdf")})


def _export(c):
    # cloud-api history export: is_public=false, no private, no grant.
    data = {"context": "history-export:Goliath", "is_public": "false", "collection_id": "cloud-history-exports",
            "ai_mode": "none", "ttl_hours": "168", "reuse_existing": "false"}
    return c.post("/storage/upload", headers=H, data=data,
                  files={"file": ("Goliath-verlauf.txt", b"verlauf", "text/plain")})


def _row(eng, oid):
    with eng.connect() as conn:
        return conn.execute(text("SELECT o.is_public, u.email, o.metadata_json FROM storage_objects o "
                                 "JOIN users u ON u.id=o.owner_user_id WHERE o.id=:i"), {"i": oid}).one()


def _objects(eng):
    return _count(eng, "SELECT count(*) FROM storage_objects")


def test_private_attachment_is_own_and_bound(env):
    c, _, eng = env
    r = _attachment(c)
    assert r.status_code == 200, r.text
    oid = r.json()["id"]
    is_public, email, md = _row(eng, oid)
    assert not is_public and email == ACCOUNT and '"private_media": true' in md
    assert _count(eng, "SELECT scope FROM media_grants WHERE object_id=:o", o=oid) == AGENT


def test_history_export_without_private_flag_stays_closed(env):
    c, _, eng = env
    r = _export(c)
    assert r.status_code == 200, r.text
    oid = r.json()["id"]
    is_public, email, _ = _row(eng, oid)
    assert not is_public and email == ACCOUNT
    # own read: metadata and bytes, as cloud-api's _storage_fetch does
    meta = c.get(f"/storage/objects/{oid}", headers=H)
    assert meta.status_code == 200 and meta.json()["context"] == "history-export:Goliath"
    body = c.get(f"/storage/media/{oid}", headers=H)
    assert body.status_code == 200 and body.content == b"verlauf"
    # nobody else
    assert c.get(f"/storage/media/{oid}").status_code in (401, 403)


def test_own_delete(env):
    c, _, eng = env
    oid = _attachment(c).json()["id"]
    r = c.delete(f"/storage/objects/{oid}", headers=H)
    assert r.status_code in (200, 204), r.text
    assert c.get(f"/storage/objects/{oid}", headers=H).status_code == 404


def _foreign(eng, **kw):
    from sqlalchemy.orm import Session
    with Session(eng) as s:
        owner = s.query(User).filter(User.email == OWNER).one()
        obj = StorageObject(object_key=kw.pop("key"), tenant_id="david", owner_user_id=owner.id,
                            mime_type="text/plain", original_filename="f.txt", **kw)
        s.add(obj)
        s.commit()
        return obj.id


@pytest.mark.parametrize("kind", ["private_media", "nonpublic", "public"])
def test_foreign_objects_are_never_deleted(env, kind):
    c, _, eng = env
    _attachment(c)  # machine account exists
    md = {"tenant_id": "david", "private_media": True} if kind == "private_media" else {"tenant_id": "david"}
    oid = _foreign(eng, key=f"{kind}.txt", is_public=(kind == "public"), metadata_json=md)
    before = _objects(eng)
    r = c.delete(f"/storage/objects/{oid}", headers=H)
    assert r.status_code in (403, 404), r.text
    assert _objects(eng) == before
    assert _count(eng, "SELECT count(*) FROM storage_objects WHERE id=:o", o=oid) == 1


@pytest.mark.parametrize("kind", ["private_media", "nonpublic", "granted_to_agent"])
def test_foreign_private_objects_are_not_readable(env, kind):
    c, granted_oid, eng = env
    _attachment(c)
    if kind == "granted_to_agent":
        oid = granted_oid  # private_media, bound to cloud-session:Goliath, owned by someone else
    else:
        md = {"tenant_id": "david", "private_media": True} if kind == "private_media" else {"tenant_id": "david"}
        oid = _foreign(eng, key=f"{kind}.txt", is_public=False, metadata_json=md)
    assert c.get(f"/storage/objects/{oid}", headers=H).status_code in (403, 404)
    assert c.get(f"/storage/media/{oid}", headers=H).status_code in (403, 404)


@pytest.mark.parametrize("fields", [
    {"is_public": "true"}, {"is_public": "1"}, {"private": "false"},
    {"grant_scope": "content-post:7"}, {"grant_scope": "agent:Goliath"}, {"grant_scope": " , "},
    {"grant_scope": f"{AGENT},comm-message:1"},
    {"ai_mode": "safety"}, {"analyze": "true"}, {"reuse_existing": "true"}, {"ttl_hours": "-1"},
    {"encrypted": "yes"}, {"collection_id": "a/b"}, {"collection_id": ""},
    {"owner_email": OWNER}, {"storage_mode": "reference"}, {"reference_path": "/etc/passwd"},
    {"external_uri": "https://example.org/x"}, {"reference_id": "1"}, {"hls_result": "true"},
    {"visibility_level": "intern"}, {"link_id": "x"}, {"original_filename": "x.pdf"},
    {"ai_tasks": "vision"}, {"transcribe_audio": "true"}, {"metadata_json": "{}"},
])
def test_refuses_everything_beyond_private_cloud_files(env, fields):
    c, _, eng = env
    before = _objects(eng)
    r = _attachment(c, filename="second.pdf", **fields)
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "ingest_upload_refused"
    assert _objects(eng) == before


def test_unparsable_ttl_is_refused_before_anything(env):
    c, _, eng = env
    before = _objects(eng)
    assert _attachment(c, ttl_hours="x").status_code == 422  # FastAPI form parsing
    assert _objects(eng) == before


@pytest.mark.parametrize("header", ["X-On-Behalf-Of", "X-Compute-Focal"])
def test_refuses_extra_headers(env, header):
    c, _, eng = env
    before = _objects(eng)
    r = _attachment(c, headers={header: OWNER if header == "X-On-Behalf-Of" else "true"})
    assert r.status_code == 403
    assert _objects(eng) == before


@pytest.mark.parametrize("call", ["sign", "list", "grant", "patch", "head"])
def test_other_routes_hit_the_gate(env, call):
    c, oid, _ = env
    r = {
        "sign": lambda: c.post("/storage/sign", headers=H, json={"tenant": "david", "scope": AGENT,
                                                                 "ids": [oid], "ttl": 60}),
        "list": lambda: c.get("/storage/list", headers=H),
        "grant": lambda: c.post(f"/storage/objects/{oid}/grants", headers=H, json={"scope": AGENT}),
        "patch": lambda: c.patch(f"/storage/objects/{oid}", headers=H, json={"is_public": True}),
        "head": lambda: c.head(f"/storage/media/{oid}", headers=H),
    }[call]()
    assert r.status_code == 403


def test_encrypted_attachment(env, monkeypatch):
    from storage import encryption as E
    _, pub = E.generate_recovery_keypair()
    monkeypatch.setenv("STORAGE_ENCRYPTION_BACKEND", "test")
    monkeypatch.setenv("STORAGE_ENCRYPTION_ALLOW_TEST_BACKEND", "1")
    monkeypatch.setenv("STORAGE_ENCRYPTION_TEST_KEY", base64.b64encode(secrets.token_bytes(32)).decode())
    monkeypatch.setenv("STORAGE_ENCRYPTION_RECOVERY_PUBKEY", base64.b64encode(pub).decode())
    c, _, eng = env
    # cloud-api _upload_verschluesselt
    r = _attachment(c, encrypted="true", ttl_hours="0", context="cloud-upload-encrypted")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["encrypted"] is True and body["is_public"] is False
    assert AGENT in (body.get("grant_scope") or "")
    _, email, _ = _row(eng, body["id"])
    assert email == ACCOUNT
    assert c.delete(f"/storage/objects/{body['id']}", headers=H).status_code in (200, 204)
