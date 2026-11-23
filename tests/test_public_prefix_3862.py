"""#3862: storage-api behind a path prefix (/svc/storage on customer instances).

The prefix comes from STORAGE_URL_PREFIX only, never from a request header.
Without the setting every URL stays exactly as before.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage.url_builder import get_base_url_from_request, public_prefix, with_public_prefix  # noqa: E402
from tests.test_sign_scopes import DAILY, NARROW, _sign, env  # noqa: E402,F401

PREFIX = "/svc/storage"


def _req(host, headers=None, proto="https"):
    h = {"host": host, "x-forwarded-proto": proto}
    h.update(headers or {})
    return SimpleNamespace(url=SimpleNamespace(scheme="http"), headers=h, client=SimpleNamespace(host="127.0.0.1"))


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("STORAGE_URL_PREFIX", "STORAGE_TRUSTED_HOSTS", "STORAGE_BASE_URL"):
        monkeypatch.delenv(k, raising=False)


def test_without_prefix_unchanged():
    assert get_base_url_from_request(_req("inst.example")) == "https://inst.example"


def test_with_prefix(monkeypatch):
    monkeypatch.setenv("STORAGE_URL_PREFIX", PREFIX)
    assert get_base_url_from_request(_req("inst.example")) == "https://inst.example/svc/storage"


@pytest.mark.parametrize("raw", ["svc/storage", "/svc/storage/", " /svc/storage "])
def test_prefix_normalised(monkeypatch, raw):
    monkeypatch.setenv("STORAGE_URL_PREFIX", raw)
    assert public_prefix() == PREFIX


@pytest.mark.parametrize("raw", ["/svc/../x", "//evil.example", "/a b", "/x?y=1", "/x#y", "https://evil.example"])
def test_malformed_prefix_ignored(monkeypatch, raw):
    monkeypatch.setenv("STORAGE_URL_PREFIX", raw)
    assert public_prefix() == ""
    assert get_base_url_from_request(_req("inst.example")) == "https://inst.example"


@pytest.mark.parametrize("hdr", ["x-forwarded-prefix", "x-script-name", "x-forwarded-path", "x-storage-url-prefix"])
def test_client_prefix_header_ignored(hdr):
    assert get_base_url_from_request(_req("inst.example", {hdr: "/evil"})) == "https://inst.example"


def test_client_prefix_header_does_not_override_config(monkeypatch):
    monkeypatch.setenv("STORAGE_URL_PREFIX", PREFIX)
    got = get_base_url_from_request(_req("inst.example", {"x-forwarded-prefix": "/evil"}))
    assert got == "https://inst.example/svc/storage"


def test_fallback_base_not_doubled(monkeypatch):
    monkeypatch.setenv("STORAGE_URL_PREFIX", PREFIX)
    monkeypatch.setenv("STORAGE_TRUSTED_HOSTS", "inst.example")
    monkeypatch.setenv("STORAGE_BASE_URL", "https://inst.example/svc/storage")
    assert get_base_url_from_request(_req("evil.example")) == "https://inst.example/svc/storage"
    monkeypatch.setenv("STORAGE_BASE_URL", "https://inst.example")
    assert get_base_url_from_request(_req("evil.example")) == "https://inst.example/svc/storage"


def test_with_public_prefix_idempotent(monkeypatch):
    monkeypatch.setenv("STORAGE_URL_PREFIX", PREFIX)
    once = with_public_prefix("http://inst.example/")
    assert once == "http://inst.example/svc/storage"
    assert with_public_prefix(once) == once


def test_signed_link_carries_prefix(env, monkeypatch):
    c, (ref, _) = env
    monkeypatch.setenv("STORAGE_URL_PREFIX", PREFIX)
    item = _sign(c, NARROW, DAILY, [ref]).json()["items"][0]
    assert item["signed"] is True
    assert item["url"].startswith(f"http://testserver{PREFIX}/storage/media/{ref}?")


def test_signed_link_without_prefix_unchanged(env):
    c, (ref, _) = env
    item = _sign(c, NARROW, DAILY, [ref]).json()["items"][0]
    assert item["url"].startswith(f"http://testserver/storage/media/{ref}?")


def test_signed_link_ignores_client_prefix_header(env):
    c, (ref, _) = env
    r = c.post("/storage/sign", headers={"X-API-KEY": NARROW, "X-Forwarded-Prefix": "/evil"},
               json={"tenant": "arkturian", "scope": DAILY, "ids": [ref], "ttl": 60})
    assert r.json()["items"][0]["url"].startswith(f"http://testserver/storage/media/{ref}?")


def test_share_link_base_carries_prefix(monkeypatch):
    from storage.share_links import _share_base
    monkeypatch.delenv("STORAGE_SHARE_BASE_URL", raising=False)
    req = SimpleNamespace(base_url="https://inst.example/")
    assert _share_base(req) == "https://inst.example/storage/s"
    monkeypatch.setenv("STORAGE_URL_PREFIX", PREFIX)
    assert _share_base(req) == "https://inst.example/svc/storage/storage/s"
