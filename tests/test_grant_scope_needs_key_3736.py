"""#3736: X-Grant-Scope-Restrict without a valid key was an anonymous oracle
(404 vs 401/403 told whether an object exists and is granted to a scope).
Now the header needs a key that authenticates, else one uniform 401."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from storage import grant_restrict as G  # noqa: E402

H = {"X-Grant-Scope-Restrict": "cloud-session:Alpha"}


@pytest.fixture
def client():
    seen = []
    app = FastAPI()

    @app.api_route("/storage/objects/{oid}/grants", methods=["GET", "HEAD"])
    def grants(oid: int):
        seen.append(oid)
        return {"ok": oid}

    @app.api_route("/storage/media/{oid}", methods=["GET", "HEAD"])
    def media(oid: int):
        seen.append(oid)
        return {"ok": oid}

    app.add_middleware(G.GrantScopeRestrict)
    app.add_middleware(G.GrantScopeNeedsKey, key_check=lambda k: k == "good")
    c = TestClient(app)
    c.seen = seen
    return c


def test_no_header_untouched(client):
    assert client.get("/storage/objects/1/grants").status_code == 200
    assert client.seen == [1]


@pytest.mark.parametrize("path", ["/storage/objects/1/grants", "/storage/objects/999999/grants",
                                  "/storage/media/1", "/storage/media/999999"])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("key", [None, "bad", ""])
def test_header_without_valid_key_uniform_401(client, path, method, key):
    h = dict(H)
    if key is not None:
        h["X-API-KEY"] = key
    r = client.request(method, path, headers=h)
    assert r.status_code == 401
    if method == "GET":
        assert r.json() == {"detail": "Invalid API key"}
    assert client.seen == []  # nothing below was asked


def test_header_with_valid_key_passes(client):
    r = client.head("/storage/media/7", headers={**H, "X-API-KEY": "good"})
    assert r.status_code == 200
    assert client.seen == [7]


def test_blank_header_is_no_header(client):
    assert client.get("/storage/media/3", headers={"X-Grant-Scope-Restrict": "  "}).status_code == 200


def test_key_check_fails_closed(monkeypatch):
    import database
    monkeypatch.setattr(database, "SessionLocal", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    assert G._key_authenticates("anything") is False
    assert G._key_authenticates(None) is False


def test_key_check_real_rules(tmp_path, monkeypatch):
    from models import Base, TenantAPIKey, User
    eng = create_engine(f"sqlite:///{tmp_path/'t.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    S = sessionmaker(bind=eng)
    s = S()
    s.add(User(email="u@x", display_name="u", password_hash="", api_key="userkey", trust_level="user", device_ids=[]))
    s.add(TenantAPIKey(tenant_id="t1", api_key="offkey", is_active=False))
    s.commit(); s.close()
    import database
    monkeypatch.setattr(database, "SessionLocal", S)
    from config import settings
    monkeypatch.setattr(settings, "API_KEY", None, raising=False)
    assert G._key_authenticates("userkey") is True
    assert G._key_authenticates("offkey") is False
    assert G._key_authenticates("nope") is False
