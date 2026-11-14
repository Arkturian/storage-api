"""#2916: active documents get CSP sandbox + nosniff; X-Forwarded-Host is only
trusted when it matches Host or an explicit allowlist."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI  # noqa: E402
from fastapi.responses import PlainTextResponse, Response, StreamingResponse  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from storage.active_content import ActiveContentGuard, is_active_content_type  # noqa: E402
from storage.url_builder import get_base_url_from_request  # noqa: E402


@pytest.fixture
def client():
    app = FastAPI()

    @app.get("/storage/media/{kind}")
    def media(kind: str):
        types = {"html": "text/html; charset=utf-8", "svg": "image/svg+xml", "js": "application/javascript",
                 "png": "image/png", "json": "application/json", "atom": "application/atom+xml"}
        return Response(content=b"<x/>", media_type=types[kind])

    @app.get("/storage/stream")
    def stream():
        return StreamingResponse(iter([b"<svg>", b"</svg>"]), media_type="image/svg+xml")

    @app.get("/docs")
    def docs():
        return Response(content=b"<html></html>", media_type="text/html")

    app.add_middleware(ActiveContentGuard)
    return TestClient(app)


@pytest.mark.parametrize("kind", ["html", "svg", "js", "atom"])
def test_active_types_sandboxed(client, kind):
    r = client.get(f"/storage/media/{kind}")
    assert r.headers["content-security-policy"].startswith("sandbox")
    assert "allow-same-origin" not in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("kind", ["png", "json"])
def test_passive_types_untouched(client, kind):
    r = client.get(f"/storage/media/{kind}")
    assert "content-security-policy" not in r.headers


def test_streaming_response_sandboxed(client):
    r = client.get("/storage/stream")
    assert r.content == b"<svg></svg>"
    assert r.headers["content-security-policy"].startswith("sandbox")


def test_docs_exempt(client):
    assert "content-security-policy" not in client.get("/docs").headers


def _req(host, xfh=None, proto="https"):
    headers = {"host": host, "x-forwarded-proto": proto}
    if xfh:
        headers["x-forwarded-host"] = xfh
    return SimpleNamespace(url=SimpleNamespace(scheme="http"), headers=headers, client=SimpleNamespace(host="127.0.0.1"))


def test_spoofed_forwarded_host_ignored(monkeypatch):
    monkeypatch.delenv("STORAGE_TRUSTED_HOSTS", raising=False)
    assert get_base_url_from_request(_req("api-storage.arkturian.com", "evil.example")) == "https://api-storage.arkturian.com"


def test_matching_forwarded_host_kept(monkeypatch):
    monkeypatch.delenv("STORAGE_TRUSTED_HOSTS", raising=False)
    assert get_base_url_from_request(_req("api-storage.arkturian.com", "api-storage.arkturian.com")) == "https://api-storage.arkturian.com"


def test_trusted_list_enforced(monkeypatch):
    monkeypatch.setenv("STORAGE_TRUSTED_HOSTS", "api-storage.arkturian.com,files.arkturian.com")
    monkeypatch.setenv("STORAGE_BASE_URL", "https://api-storage.arkturian.com")
    assert get_base_url_from_request(_req("files.arkturian.com")) == "https://files.arkturian.com"
    assert get_base_url_from_request(_req("api-storage.arkturian.com", "files.arkturian.com")) == "https://files.arkturian.com"
    assert get_base_url_from_request(_req("evil.example")) == "https://api-storage.arkturian.com"


def test_internal_loopback_unchanged(monkeypatch):
    monkeypatch.delenv("STORAGE_TRUSTED_HOSTS", raising=False)
    assert get_base_url_from_request(_req("127.0.0.1:8080", proto="")) .endswith("127.0.0.1:8080")


def test_type_detection():
    assert is_active_content_type("image/svg+xml; charset=utf-8")
    assert not is_active_content_type("image/jpeg")


def test_template_context_no_longer_bypasses_safety_for_media():
    src = (Path(__file__).resolve().parent.parent / "storage" / "service.py").read_text()
    assert "is_markup = not _media_or_active and (" in src
