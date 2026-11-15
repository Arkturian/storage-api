"""#3032 (Befund 107/108): external_uri is checked at upload, /data-uri fetches
through the guard and only returns real raster images, own public node
addresses and non-web ports are refused, and no raw client fetches a
user-supplied URL any more."""
import io
import re
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage import safe_fetch as SF  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def _fake_dns(monkeypatch, mapping):
    def gai(host, port, *a, **k):
        if host not in mapping:
            raise socket.gaierror("nx")
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))
                for ip in mapping[host]]
    monkeypatch.setattr(SF.socket, "getaddrinfo", gai)
    SF._own_ips_for.cache_clear()


@pytest.mark.parametrize("ip", ["188.65.73.164", "192.46.232.130", "62.225.187.68",
                                "::ffff:188.65.73.164", "64:ff9b::bc41:49a4", "2002:bc41:49a4::1",
                                "2a01:7e01::f03c:94ff:feb3:d807", "217.149.167.157", "217.149.160.166"])
def test_own_node_addresses_blocked(monkeypatch, ip):
    _fake_dns(monkeypatch, {"own.example": [ip]})
    with pytest.raises(SF.OutboundBlocked):
        SF.resolve_public("own.example", 443)


def test_public_still_allowed(monkeypatch):
    _fake_dns(monkeypatch, {"cdn.example": ["93.184.216.34"]})
    assert SF.resolve_public("cdn.example", 443) == ["93.184.216.34"]


@pytest.mark.parametrize("port", [22, 6379, 8080, 5432])
def test_non_web_ports_blocked(monkeypatch, port):
    _fake_dns(monkeypatch, {"cdn.example": ["93.184.216.34"]})
    with pytest.raises(SF.OutboundBlocked):
        SF.resolve_public("cdn.example", port)


@pytest.mark.parametrize("url", ["http://127.0.0.1:8080/storage/list", "http://169.254.169.254/latest/meta-data",
                                 "http://10.192.26.50/", "http://172.17.0.1/", "file:///etc/passwd",
                                 "https://cdn.example:6379/x"])
def test_check_url_resolvable_rejects(monkeypatch, url):
    _fake_dns(monkeypatch, {"cdn.example": ["93.184.216.34"], "127.0.0.1": ["127.0.0.1"],
                            "169.254.169.254": ["169.254.169.254"], "10.192.26.50": ["10.192.26.50"],
                            "172.17.0.1": ["172.17.0.1"]})
    with pytest.raises(SF.OutboundBlocked):
        SF.check_url_resolvable(url)


def test_upload_checks_external_uri():
    src = (ROOT / "storage" / "routes.py").read_text()
    assert "check_url_resolvable(external_uri)" in src


# --------------------------------------------------------------------------- #
# /data-uri
# --------------------------------------------------------------------------- #

def _png():
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (3, 2), (1, 2, 3)).save(b, format="PNG")
    return b.getvalue()


class _Resp:
    def __init__(self, body, status=200):
        self._body, self.status_code = body, status

    @property
    def is_success(self):
        return 200 <= self.status_code < 300

    async def aiter_bytes(self):
        for i in range(0, len(self._body), 4):
            yield self._body[i:i + 4]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Client:
    def __init__(self, body=b"", exc=None):
        self.body, self.exc = body, exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, method, url):
        if self.exc:
            raise self.exc
        return _Resp(self.body)


@pytest.fixture
def data_uri(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from models import Base, StorageObject, User
    from database import get_db
    from storage import routes as R

    eng = create_engine(f"sqlite:///{tmp_path/'t.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    u = User(email="o@x", display_name="o", password_hash="", api_key="k", trust_level="user", device_ids=[])
    s.add(u); s.commit()
    o = StorageObject(id=987654321, object_key="e.png", original_filename="e.png", owner_user_id=u.id, tenant_id="t1",
                      file_size_bytes=1, mime_type="image/png", is_public=True, ai_safety_status="completed",
                      external_uri="https://cdn.example/e.png", storage_mode="external")
    s.add(o); s.commit()
    state = {"client": _Client(_png())}
    monkeypatch.setattr(SF, "safe_async_client", lambda **k: state["client"])
    app = FastAPI()
    app.include_router(R.router, prefix="/storage")
    app.dependency_overrides[get_db] = lambda: s
    yield SimpleNamespace(c=TestClient(app), id=o.id, state=state)
    s.close()


def test_data_uri_returns_real_image(data_uri):
    r = data_uri.c.get(f"/storage/objects/{data_uri.id}/data-uri")
    assert r.status_code == 200, r.text
    assert r.json()["data_uri"].startswith("data:image/png;base64,")


def test_data_uri_refuses_non_image_body(data_uri):
    data_uri.state["client"] = _Client(b'{"internal": "secret"}')
    r = data_uri.c.get(f"/storage/objects/{data_uri.id}/data-uri")
    assert r.status_code == 415
    assert "secret" not in r.text


def test_data_uri_blocked_destination(data_uri):
    data_uri.state["client"] = _Client(exc=SF.OutboundBlocked("destination address is not public"))
    r = data_uri.c.get(f"/storage/objects/{data_uri.id}/data-uri")
    assert r.status_code == 400 and r.json()["detail"]["code"] == "outbound_blocked"


def test_data_uri_size_cap_while_streaming(data_uri):
    data_uri.state["client"] = _Client(b"x" * 5000)
    r = data_uri.c.get(f"/storage/objects/{data_uri.id}/data-uri", params={"max_size": 100})
    assert r.status_code == 413


# --------------------------------------------------------------------------- #
# Wächter: no raw client fetches a user-supplied URL
# --------------------------------------------------------------------------- #

_USER_URL = re.compile(r"(external_uri|\buri\b|payload\.url|\burl\b)")
_RAW = re.compile(r"httpx\.(Async)?Client\(")


@pytest.mark.parametrize("rel", ["storage/routes.py", "storage/async_pipeline.py", "ai_analysis/uri_handler.py",
                                 "storage/external_proxy.py", "knowledge_graph/pipeline.py"])
def test_no_raw_client_on_user_urls(rel):
    lines = (ROOT / rel).read_text().splitlines()
    for i, line in enumerate(lines):
        if _RAW.search(line):
            window = "\n".join(lines[i:i + 3])
            # the next lines after a raw client must not fetch a user-supplied URL
            assert not re.search(r"\.(get|head|stream|post)\((\"GET\", )?(obj\.external_uri|external_uri|uri)\b",
                                 window), f"{rel}:{i+1} raw httpx client on a user URL"


def test_former_pdrei_address_not_own(monkeypatch):
    # 172.104.235.73 belongs to someone else since #2595: an ordinary public host
    _fake_dns(monkeypatch, {"x.example": ["172.104.235.73"]})
    assert SF.resolve_public("x.example", 443) == ["172.104.235.73"]
