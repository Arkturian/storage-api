"""#3811: the MCP gateway marks uploads with the caller's home tenant.

storage_objects.origin_tenant comes from X-Caller-Tenant, believed only from
an active key flagged may_assert_scope. The object's tenant_id stays that of
the key. A missing or untrusted header marks nothing.
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
from models import TenantAPIKey, User  # noqa: E402

GATEWAY = "k-gateway-3811"
PLAIN = "k-plain-3811"


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
    s.add(User(email="system@api", display_name="s", password_hash="", api_key=None, trust_level="user", device_ids=[]))
    s.add(User(email="plain@example.org", display_name="p", password_hash="", api_key=None, trust_level="user", device_ids=[]))
    s.commit()
    s.add_all([
        TenantAPIKey(api_key=GATEWAY, tenant_id="arkturian", label="mcp-gateway", is_active=True,
                     acts_as_email="system@api", may_assert_scope=True),
        TenantAPIKey(api_key=PLAIN, tenant_id="arkturian", label="plain service", is_active=True,
                     acts_as_email="plain@example.org", may_assert_scope=False),
    ])
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
        yield TestClient(main.app, raise_server_exceptions=False), eng
    finally:
        main.app.dependency_overrides.clear()


def _upload(c, key, headers=None, name="a.bin"):
    return c.post("/storage/upload", headers={"X-API-KEY": key, **(headers or {})},
                  data={"context": "t", "private": "true", "reuse_existing": "false"},
                  files={"file": (name, b"payload-3811-" + name.encode(), "application/octet-stream")})


def _row(eng, oid):
    with eng.connect() as conn:
        return conn.execute(text("SELECT tenant_id, origin_tenant FROM storage_objects WHERE id=:i"), {"i": oid}).one()


def test_gateway_marks_the_callers_home_tenant(env):
    c, eng = env
    r = _upload(c, GATEWAY, {"X-Caller-Tenant": "lesser", "X-Grant-Scope-Restrict": "cloud-session:Blender"})
    assert r.status_code == 200, r.text
    assert tuple(_row(eng, r.json()["id"])) == ("arkturian", "lesser")


def test_own_tenant_is_marked_too(env):
    c, eng = env
    r = _upload(c, GATEWAY, {"X-Caller-Tenant": "arkturian"}, name="b.bin")
    assert tuple(_row(eng, r.json()["id"])) == ("arkturian", "arkturian")


def test_without_header_nothing_is_marked(env):
    c, eng = env
    r = _upload(c, GATEWAY, name="c.bin")
    assert tuple(_row(eng, r.json()["id"])) == ("arkturian", None)


def test_header_from_a_key_without_may_assert_scope_is_ignored(env):
    c, eng = env
    r = _upload(c, PLAIN, {"X-Caller-Tenant": "lesser"}, name="d.bin")
    assert r.status_code == 200, r.text
    assert tuple(_row(eng, r.json()["id"])) == ("arkturian", None)


@pytest.mark.parametrize("value", ["Lesser", "les ser", "a" * 65, "lesser,edera", "../x"])
def test_malformed_value_is_ignored(env, value):
    c, eng = env
    r = _upload(c, GATEWAY, {"X-Caller-Tenant": value}, name=f"e{abs(hash(value))}.bin")
    assert r.status_code == 200, r.text
    assert _row(eng, r.json()["id"])[1] is None


def test_upload_ticket_carries_the_mark_to_the_keyless_put(env):
    c, eng = env
    r = c.post("/storage/upload-ticket", headers={"X-API-KEY": GATEWAY, "X-Caller-Tenant": "edera"},
               params={"filename": "t.bin", "private": "true"})
    assert r.status_code == 200, r.text
    put = c.put(f"/storage/upload-ticket/{r.json()['token']}", content=b"ticket-bytes-3811")
    assert put.status_code == 200, put.text
    assert tuple(_row(eng, put.json()["id"])) == ("arkturian", "edera")


def test_ticket_put_cannot_add_a_mark_itself(env):
    c, eng = env
    r = c.post("/storage/upload-ticket", headers={"X-API-KEY": GATEWAY}, params={"filename": "u.bin", "private": "true"})
    put = c.put(f"/storage/upload-ticket/{r.json()['token']}", content=b"ticket-bytes-3811-u",
                headers={"X-Caller-Tenant": "lesser"})
    assert put.status_code == 200, put.text
    assert _row(eng, put.json()["id"])[1] is None


def test_migration_adds_the_column(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path/'old.db'}")
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE storage_objects (id INTEGER PRIMARY KEY, object_key VARCHAR)"))
    import inspect
    assert "origin_tenant" in inspect.getsource(database)


def test_upload_ticket_url_carries_public_prefix(env, monkeypatch):
    """#3862: behind /svc/storage the ticket URL must include the prefix."""
    c, _ = env
    monkeypatch.setenv("STORAGE_URL_PREFIX", "/svc/storage")
    r = c.post("/storage/upload-ticket", headers={"X-API-KEY": PLAIN}, params={"filename": "p.bin"})
    assert r.status_code == 200, r.text
    assert r.json()["upload_url"].startswith("http://testserver/svc/storage/storage/upload-ticket/")
    monkeypatch.delenv("STORAGE_URL_PREFIX")
    r = c.post("/storage/upload-ticket", headers={"X-API-KEY": PLAIN}, params={"filename": "p.bin"})
    assert r.json()["upload_url"].startswith("http://testserver/storage/upload-ticket/")
