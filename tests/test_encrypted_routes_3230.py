"""#3230 end to end over the routes: upload (multipart, ticket, chunk), read
(full, Range, HEAD, 304), rights (owner, stranger, default master, operator,
signed), refusals (variants, /files, named URL, long signatures), decrypt log,
replace with a fresh DEK, delete."""
import base64
import json
import secrets
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from models import Base, StorageDecryptLog, StorageObject, User  # noqa: E402

DEFAULT = "Inetpass1"
PAYLOAD = b"Name;Gehalt\nMartina;4200\n" * 4000  # ~100 KB, two segments


@pytest.fixture
def env(tmp_path, monkeypatch):
    import auth
    import config
    from storage import encryption as E
    from storage import routes as R
    from storage.service import generic_storage

    monkeypatch.setattr(config.settings, "API_KEY", DEFAULT)
    monkeypatch.setattr(auth.settings, "API_KEY", DEFAULT)
    _, pub = E.generate_recovery_keypair()
    monkeypatch.setenv("STORAGE_ENCRYPTION_BACKEND", "test")
    monkeypatch.setenv("STORAGE_ENCRYPTION_ALLOW_TEST_BACKEND", "1")
    monkeypatch.setenv("STORAGE_ENCRYPTION_TEST_KEY", base64.b64encode(secrets.token_bytes(32)).decode())
    monkeypatch.setenv("STORAGE_ENCRYPTION_RECOVERY_PUBKEY", base64.b64encode(pub).decode())
    monkeypatch.setenv("STORAGE_SIGN_SECRETS", json.dumps({"k1": "x" * 40}))
    monkeypatch.setenv("STORAGE_SIGN_ACTIVE_KID", "k1")
    monkeypatch.setattr(generic_storage, "upload_root", tmp_path / "up")
    monkeypatch.setattr(generic_storage, "media_dir", tmp_path / "up" / "media")
    E.DEK_CACHE.clear()

    eng = create_engine(f"sqlite:///{tmp_path/'t.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    owner = User(email="martina@x", display_name="m", password_hash="", api_key="ko", trust_level="user", device_ids=[])
    other = User(email="other@x", display_name="o", password_hash="", api_key="kt", trust_level="user", device_ids=[])
    master = User(email="system@api", display_name="s", password_hash="", api_key=DEFAULT, trust_level="admin", device_ids=[])
    s.add_all([owner, other, master]); s.commit()

    from auth import get_current_user, get_current_user_optional
    from database import get_db
    from tenancy.config import get_tenant_id, get_tenant_id_optional
    state = {"uid": owner.id}
    _cur = lambda: s.get(User, state["uid"]) if state["uid"] else None  # noqa: E731
    app = FastAPI()
    app.include_router(R.router, prefix="/storage")
    app.dependency_overrides[get_db] = lambda: s
    app.dependency_overrides[get_current_user_optional] = _cur
    app.dependency_overrides[get_current_user] = _cur
    app.dependency_overrides[get_tenant_id_optional] = lambda: "arkturian"
    app.dependency_overrides[get_tenant_id] = lambda: "arkturian"
    yield SimpleNamespace(c=TestClient(app), s=s, state=state, owner=owner, other=other, master=master,
                          R=R, E=E, gs=generic_storage, tmp=tmp_path)
    s.close()


def _upload(env, data=PAYLOAD, name="Gehälter 2026.csv", **form):
    r = env.c.post("/storage/upload", files={"file": (name, data, "text/csv")},
                   data={"encrypted": "true", **form})
    assert r.status_code == 200, r.text
    return r.json()


def test_upload_stores_only_ciphertext(env):
    j = _upload(env)
    assert j["encrypted"] is True and j["is_public"] is False
    assert j["original_filename"] == env.E.FILENAME_PLACEHOLDER
    assert j["thumbnail_url"] is None and j["share_url"] is None
    obj = env.s.get(StorageObject, j["id"])
    assert obj.metadata_json.get("private_media") is True
    assert obj.object_key.startswith("enc_") and obj.plain_size == len(PAYLOAD)
    assert obj.ai_safety_status == "skipped_encrypted"
    files = [p for p in env.tmp.rglob("*") if p.is_file() and p.name != "t.db"]
    assert len(files) == 1 and "media-enc" in str(files[0])
    raw = files[0].read_bytes()
    assert b"Martina" not in raw and "Gehälter".encode() not in raw
    assert obj.checksum != __import__("hashlib").sha256(PAYLOAD).hexdigest()
    for col in ("enc_wrapped_dek", "enc_wrapped_dek_recovery", "enc_filename"):
        assert getattr(obj, col)
    assert "enc_wrapped_dek" not in j and "enc_filename" not in j


def test_owner_reads_full_and_range_and_name(env):
    oid = _upload(env)["id"]
    r = env.c.get(f"/storage/media/{oid}")
    assert r.status_code == 200 and r.content == PAYLOAD
    assert "Geh%C3%A4lter%202026.csv" in r.headers["content-disposition"]
    assert r.headers["cache-control"] == "private, no-store"
    r = env.c.get(f"/storage/media/{oid}", headers={"Range": "bytes=65530-65545"})
    assert r.status_code == 206 and r.content == PAYLOAD[65530:65546]
    assert r.headers["content-range"] == f"bytes 65530-65545/{len(PAYLOAD)}"
    r = env.c.get(f"/storage/media/{oid}", headers={"Range": "bytes=-10"})
    assert r.content == PAYLOAD[-10:]
    r = env.c.get(f"/storage/media/{oid}", headers={"Range": f"bytes={len(PAYLOAD)}-"})
    assert r.status_code == 416


def test_etag_304(env):
    oid = _upload(env)["id"]
    et = env.c.get(f"/storage/media/{oid}").headers["etag"]
    assert env.c.get(f"/storage/media/{oid}", headers={"If-None-Match": et}).status_code == 304


@pytest.mark.parametrize("who", ["other", "master"])
def test_stranger_and_default_master_refused(env, who):
    oid = _upload(env)["id"]
    env.state["uid"] = getattr(env, who).id
    assert env.c.get(f"/storage/media/{oid}").status_code == 403


def test_operator_has_no_bypass(env, monkeypatch):
    oid = _upload(env)["id"]
    env.state["uid"] = env.other.id
    monkeypatch.setattr(env.R, "_is_privileged_admin", lambda u, d: True)
    assert env.c.get(f"/storage/media/{oid}").status_code == 403


def test_signed_link_works_and_is_capped(env):
    from storage import signing
    oid = _upload(env)["id"]
    env.state["uid"] = None
    ok = signing.sign(oid, 300)
    r = env.c.get(f"/storage/media/{oid}", params=ok)
    assert r.status_code == 200 and r.content == PAYLOAD
    long = signing.sign(oid, 900)
    r = env.c.get(f"/storage/media/{oid}", params=long)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "encrypted_signature_too_long"


@pytest.mark.parametrize("q", [{"variant": "thumbnail"}, {"width": "200"}, {"format": "jpg"}, {"preset": "web"}])
def test_variants_refused(env, q):
    oid = _upload(env)["id"]
    r = env.c.get(f"/storage/media/{oid}", params=q)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "encrypted_no_variants"


def test_files_route_redirects(env):
    oid = _upload(env)["id"]
    r = env.c.get(f"/storage/files/{oid}")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "encrypted_use_media"


def test_head_route(env):
    oid = _upload(env)["id"]
    r = env.c.head(f"/storage/media/{oid}")
    assert r.status_code == 200 and int(r.headers["content-length"]) == len(PAYLOAD)


def test_decrypt_log_aggregates(env):
    oid = _upload(env)["id"]
    for rng in ("bytes=0-9", "bytes=70000-70010", None):
        env.c.get(f"/storage/media/{oid}", headers={"Range": rng} if rng else {})
    rows = env.s.query(StorageDecryptLog).filter_by(object_id=oid).all()
    assert len(rows) == 1 and rows[0].requests == 3 and rows[0].principal == "martina@x"
    assert rows[0].range_min == 0 and rows[0].range_max == len(PAYLOAD) - 1


def test_kms_unavailable_is_503_not_plaintext(env, monkeypatch):
    oid = _upload(env)["id"]
    env.E.DEK_CACHE.clear()
    monkeypatch.setenv("STORAGE_ENCRYPTION_BACKEND", "scaleway")
    monkeypatch.setenv("STORAGE_ENCRYPTION_SCW_KEY_ID", "kid")
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    r = env.c.get(f"/storage/media/{oid}")
    assert r.status_code == 503 and r.json()["detail"]["code"] == "encryption_key_unavailable"
    r = env.c.post("/storage/upload", files={"file": ("a.txt", b"x", "text/plain")}, data={"encrypted": "true"})
    assert r.status_code == 503
    assert not any(p.name.endswith(".txt") for p in env.tmp.rglob("*"))


@pytest.mark.parametrize("form", [{"storage_mode": "reference", "reference_path": "/x"}, {"owner_email": "a@b"},
                                  {"hls_result": "true", "reference_id": "1"}])
def test_upload_refusals(env, form):
    r = env.c.post("/storage/upload", files={"file": ("a.txt", b"x", "text/plain")}, data={"encrypted": "true", **form})
    assert r.status_code == 400


def test_ticket_upload_takes_name_from_header_only(env):
    r = env.c.post("/storage/upload-ticket", params={"encrypted": "true", "filename": "leak.xlsx"})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "encrypted_filename_in_query"
    r = env.c.post("/storage/upload-ticket", params={"encrypted": "true"},
                   headers={"X-Filename": "Geh%C3%A4lter.xlsx"})
    assert r.status_code == 200
    token = r.json()["token"]
    r = env.c.put(f"/storage/upload-ticket/{token}", content=PAYLOAD)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["encrypted"] is True
    got = env.c.get(f"/storage/media/{j['id']}")
    assert got.content == PAYLOAD and "Geh%C3%A4lter.xlsx" in got.headers["content-disposition"]


def test_chunk_upload_encrypted(env):
    b64 = base64.b64encode(PAYLOAD).decode()
    half = len(b64) // 2
    q = {"upload_id": "u1", "total_chunks": 2, "encrypted": "true"}
    h = {"X-Filename": "a.csv"}
    assert env.c.post("/storage/upload-chunk", params={**q, "index": 0}, content=b64[:half], headers=h).json()["complete"] is False
    r = env.c.post("/storage/upload-chunk", params={**q, "index": 1}, content=b64[half:], headers=h)
    assert r.status_code == 200 and r.json()["encrypted"] is True
    assert env.c.get(f"/storage/media/{r.json()['id']}").content == PAYLOAD


def test_replace_uses_fresh_dek_and_keeps_name(env):
    import asyncio
    from storage.domain import update_file_and_record
    oid = _upload(env)["id"]
    obj = env.s.get(StorageObject, oid)
    old_wrapped, old_key = obj.enc_wrapped_dek, obj.object_key
    asyncio.run(update_file_and_record(env.s, storage_obj=obj, data=b"neu"))
    env.s.refresh(obj)
    assert obj.object_key == old_key and obj.enc_wrapped_dek != old_wrapped and obj.plain_size == 3
    r = env.c.get(f"/storage/media/{oid}")
    assert r.content == b"neu" and "Geh%C3%A4lter%202026.csv" in r.headers["content-disposition"]


def test_plaintext_path_fails_closed(env):
    oid = _upload(env)["id"]
    obj = env.s.get(StorageObject, oid)
    with pytest.raises(env.E.EncryptedObjectError):
        env.gs.absolute_path_for_key(obj.object_key, "arkturian")


def test_delete_removes_ciphertext(env):
    oid = _upload(env)["id"]
    obj = env.s.get(StorageObject, oid)
    path = env.gs.encrypted_path_for_key(obj.object_key, "arkturian")
    assert path.exists()
    assert env.gs.delete(obj.object_key, "arkturian")
    assert not path.exists()
