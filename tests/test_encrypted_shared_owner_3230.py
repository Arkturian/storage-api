"""#3230 follow-up: an encrypted object owned by a shared service account.

Every key with acts_as_email=system@api (gateway, knowledge-api, tools, ...)
and the master authenticate as the same User. Owning through that account
must not mean "may read": only the MCP gateway's key (may_assert_scope),
narrowed to one agent by X-Grant-Scope-Restrict and holding that agent's
grant, reads bytes and the real name directly. Everyone else needs a signed
link. A personal owner keeps reading directly (tests in
test_encrypted_routes_3230)."""
import base64
import json
import secrets
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from models import Base, StorageDecryptLog, StorageObject, TenantAPIKey, User  # noqa: E402

MASTER = "fresh-master-0123456789abcdef0123456789"
AGENT = "cloud-session:EDERA-EDWARD"
PAYLOAD = b"vertrauliches Rendering\n" * 5000
NAME = "ONEAL Helm 2027 vertraulich.png"


@pytest.fixture
def env(tmp_path, monkeypatch):
    import auth
    import config
    from storage import encryption as E
    from storage import grant_restrict
    from storage import routes as R
    from storage.service import generic_storage

    monkeypatch.setattr(config.settings, "API_KEY", MASTER)
    monkeypatch.setattr(auth.settings, "API_KEY", MASTER)
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
    import database
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=eng))  # #3030 gate opens its own session
    s = sessionmaker(bind=eng)()
    system = User(email="system@api", display_name="s", password_hash="", api_key="rnd-sys", trust_level="admin",
                  device_ids=[])
    bot = User(email="bot-account@example.org", display_name="b", password_hash="", api_key="rnd-bot",
               trust_level="user", device_ids=[])
    s.add_all([system, bot]); s.commit()
    s.add(TenantAPIKey(api_key="k-bot", tenant_id="arkturian", label="bot", is_active=True,
                       acts_as_email="bot-account@example.org"))
    s.commit()

    from auth import get_current_user, get_current_user_optional
    from database import get_db
    from tenancy.config import get_tenant_id, get_tenant_id_optional
    state = {"uid": system.id, "via": "service:mcp-gateway", "assert": True}

    def _cur():
        if not state["uid"]:
            return None
        u = s.get(User, state["uid"])
        setattr(u, "_storage_auth_via", state["via"])
        setattr(u, "_storage_may_assert_scope", state["assert"])
        return u

    app = FastAPI()
    app.include_router(R.router, prefix="/storage")
    app.dependency_overrides[get_db] = lambda: s
    app.dependency_overrides[get_current_user_optional] = _cur
    app.dependency_overrides[get_current_user] = _cur
    app.dependency_overrides[get_tenant_id_optional] = lambda: "arkturian"
    app.dependency_overrides[get_tenant_id] = lambda: "arkturian"
    client = TestClient(grant_restrict.GrantScopeRestrict(app))
    yield SimpleNamespace(c=client, s=s, state=state, system=system, bot=bot, E=E)
    s.close()


def _upload(env):
    r = env.c.post("/storage/upload", headers={"X-Grant-Scope-Restrict": AGENT},
                   files={"file": (NAME, PAYLOAD, "image/png")},
                   data={"encrypted": "true", "grant_scope": AGENT})
    assert r.status_code == 200, r.text
    assert r.json()["grant_scope"] == AGENT
    return r.json()["id"]


def _as(env, via, assert_scope=False, uid=None):
    env.state.update(uid=uid or env.system.id, via=via, **{"assert": assert_scope})


def test_gateway_narrowed_to_the_agent_reads(env):
    oid = _upload(env)
    r = env.c.get(f"/storage/media/{oid}", headers={"X-Grant-Scope-Restrict": AGENT})
    assert r.status_code == 200 and r.content == PAYLOAD


def test_gateway_narrowed_to_another_agent_404(env):
    oid = _upload(env)
    r = env.c.get(f"/storage/media/{oid}", headers={"X-Grant-Scope-Restrict": "cloud-session:Someone"})
    assert r.status_code == 404


def test_gateway_without_narrowing_refused(env):
    oid = _upload(env)
    r = env.c.get(f"/storage/media/{oid}")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "encrypted_shared_owner"


@pytest.mark.parametrize("header", [None, AGENT])
def test_other_service_acting_as_system_refused(env, header):
    """knowledge-api & co. own the row too, and naming the agent's scope in
    the header must not turn into access for them."""
    oid = _upload(env)
    _as(env, "service:knowledge-api", assert_scope=False)
    h = {"X-Grant-Scope-Restrict": header} if header else {}
    r = env.c.get(f"/storage/media/{oid}", headers=h)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "encrypted_shared_owner"


def test_master_refused(env):
    oid = _upload(env)
    _as(env, "master")
    assert env.c.get(f"/storage/media/{oid}").status_code == 403


def test_flag_alone_is_not_enough_without_service_path(env):
    oid = _upload(env)
    _as(env, "user-key", assert_scope=True)
    r = env.c.get(f"/storage/media/{oid}", headers={"X-Grant-Scope-Restrict": AGENT})
    assert r.status_code == 403


def test_signed_link_still_works_for_anyone(env):
    from storage import signing
    oid = _upload(env)
    env.state["uid"] = None
    r = env.c.get(f"/storage/media/{oid}", params=signing.sign(oid, 300))
    assert r.status_code == 200 and r.content == PAYLOAD


def test_real_name_only_for_the_narrowed_gateway(env):
    oid = _upload(env)
    j = env.c.get(f"/storage/objects/{oid}", headers={"X-Grant-Scope-Restrict": AGENT}).json()
    assert j["original_filename"] == NAME
    _as(env, "service:knowledge-api", assert_scope=False)
    j = env.c.get(f"/storage/objects/{oid}").json()
    assert j["original_filename"] == env.E.FILENAME_PLACEHOLDER


def test_log_names_the_key(env):
    oid = _upload(env)
    env.c.get(f"/storage/media/{oid}", headers={"X-Grant-Scope-Restrict": AGENT})
    principals = [r.principal for r in env.s.query(StorageDecryptLog).filter_by(object_id=oid)]
    assert principals == [f"system@api[service:mcp-gateway]|{AGENT}"]


def test_read_event_carries_scope(env, monkeypatch):
    from storage import encrypted_io
    events = []
    monkeypatch.setattr(encrypted_io, "emit_event", lambda kind, payload: events.append((kind, payload)))
    oid = _upload(env)
    env.c.get(f"/storage/media/{oid}", headers={"X-Grant-Scope-Restrict": AGENT})
    reads = [p for k, p in events if k == "encrypted_read"]
    assert len(reads) == 1 and reads[0]["scope"] == AGENT and reads[0]["object_id"] == oid


def test_acts_as_target_with_personal_looking_email_is_shared(env):
    """Not only *@api: any account a key acts as is shared."""
    from storage import routes as R
    assert R._is_shared_identity(env.bot.id, env.s) is True
    assert R._is_shared_identity(env.system.id, env.s) is True
    human = User(email="martina@x", display_name="m", password_hash="", api_key="kh", trust_level="user",
                 device_ids=[])
    env.s.add(human); env.s.commit()
    assert R._is_shared_identity(human.id, env.s) is False
    assert R._is_shared_identity(None, env.s) is True


def test_object_row_stays_owned_by_system(env):
    oid = _upload(env)
    assert env.s.get(StorageObject, oid).owner_user_id == env.system.id
