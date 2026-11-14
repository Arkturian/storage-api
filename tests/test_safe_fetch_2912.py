"""#2912: outbound fetches of user-supplied URLs reach public addresses only,
per redirect hop; /storage/proxy needs a key and only ever returns re-encoded
raster images."""
import asyncio
import io
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from storage import safe_fetch as SF  # noqa: E402


def _fake_dns(monkeypatch, mapping):
    def gai(host, port, *a, **k):
        if host not in mapping:
            raise socket.gaierror("nx")
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))
                for ip in mapping[host]]
    monkeypatch.setattr(SF.socket, "getaddrinfo", gai)


@pytest.mark.parametrize("ip", [
    "127.0.0.1", "10.192.26.50", "172.16.255.20", "192.168.1.1", "169.254.169.254",
    "100.64.0.1", "0.0.0.0", "::1", "fc00::1", "fe80::1", "::ffff:127.0.0.1", "224.0.0.1",
])
def test_private_addresses_blocked(monkeypatch, ip):
    _fake_dns(monkeypatch, {"evil.example": [ip]})
    with pytest.raises(SF.OutboundBlocked):
        SF.resolve_public("evil.example", 443)


def test_mixed_answer_blocked(monkeypatch):
    # one public + one private record: refuse (no "pick the good one")
    _fake_dns(monkeypatch, {"rebind.example": ["93.184.216.34", "127.0.0.1"]})
    with pytest.raises(SF.OutboundBlocked):
        SF.resolve_public("rebind.example", 443)


def test_public_allowed(monkeypatch):
    _fake_dns(monkeypatch, {"cdn.example": ["93.184.216.34"]})
    assert SF.resolve_public("cdn.example", 443) == ["93.184.216.34"]


@pytest.mark.parametrize("url", ["file:///etc/passwd", "gopher://x/", "https://u:p@cdn.example/x", "http:///x", "ftp://cdn.example/x"])
def test_check_url_rejects(url):
    with pytest.raises(SF.OutboundBlocked):
        SF.check_url(url)


class _H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/secret")
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"internal-secret")

    def log_message(self, *a):
        pass


@pytest.fixture
def local_server():
    srv = HTTPServer(("127.0.0.1", 0), _H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()


def test_real_connect_to_loopback_blocked(local_server):
    port = local_server.server_port

    async def go():
        async with SF.safe_async_client(timeout=5) as c:
            await c.get(f"http://127.0.0.1:{port}/secret")
    with pytest.raises(SF.OutboundBlocked):
        asyncio.run(go())
    with SF.safe_client(timeout=5) as c, pytest.raises(SF.OutboundBlocked):
        c.get(f"http://127.0.0.1:{port}/secret")


def test_redirect_hop_to_loopback_blocked(local_server, monkeypatch):
    # First hop allowed explicitly (stands in for a public host), the redirect
    # target 127.0.0.1 is not: the second connection must be refused.
    monkeypatch.setenv("STORAGE_OUTBOUND_ALLOW_HOSTS", "localhost")
    port = local_server.server_port
    with SF.safe_client(timeout=5) as c, pytest.raises(SF.OutboundBlocked):
        c.get(f"http://localhost:{port}/redirect")


def test_plain_httpx_would_have_read_it(local_server):
    # Control: the same request without the guard succeeds (what #2912 exploited).
    port = local_server.server_port
    r = httpx.get(f"http://127.0.0.1:{port}/redirect", follow_redirects=True, timeout=5)
    assert r.content == b"internal-secret"


def test_fetch_external_file_blocks_before_cache(monkeypatch):
    from storage import external_proxy as EP
    called = {}
    monkeypatch.setattr(EP.external_cache, "get_from_cache", lambda uri: called.setdefault("cache", (b"old", {})))
    with pytest.raises(SF.OutboundBlocked):
        asyncio.run(EP.fetch_external_file("http://127.0.0.1:8097/storage/list", use_cache=True))
    assert "cache" not in called


# --------------------------------------------------------------------------- #
# /storage/proxy
# --------------------------------------------------------------------------- #

@pytest.fixture
def proxy_client(monkeypatch):
    from storage import routes as R
    from auth import get_current_user
    from database import get_db
    app = FastAPI()
    app.include_router(R.router, prefix="/storage")
    app.dependency_overrides[get_db] = lambda: None
    state = {"user": object(), "data": b"", "ctype": "image/png"}

    def user():
        if state["user"] is None:
            from fastapi import HTTPException
            raise HTTPException(status_code=401, detail="Invalid API key")
        return state["user"]
    app.dependency_overrides[get_current_user] = user

    async def fake_fetch(url, use_cache=True):
        return state["data"], {"content-type": state["ctype"]}
    monkeypatch.setattr(R, "fetch_external_file", fake_fetch)
    return TestClient(app), state


def _png():
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (4, 3), (200, 10, 10)).save(b, format="PNG")
    return b.getvalue()


def test_proxy_requires_key(proxy_client):
    c, st = proxy_client
    st["user"] = None
    assert c.get("/storage/proxy", params={"url": "https://cdn.example/a.png"}).status_code == 401


def test_proxy_refuses_html_even_without_params(proxy_client):
    c, st = proxy_client
    st["data"], st["ctype"] = b"<html><script>alert(1)</script></html>", "text/html"
    r = c.get("/storage/proxy", params={"url": "https://cdn.example/x"})
    assert r.status_code == 415
    assert b"<script>" not in r.content


def test_proxy_reencodes_image_with_safe_headers(proxy_client):
    c, st = proxy_client
    st["data"], st["ctype"] = _png(), "text/html"  # lying content-type
    r = c.get("/storage/proxy", params={"url": "https://cdn.example/a.png"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in r.headers["content-security-policy"]
