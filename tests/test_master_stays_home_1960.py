"""#1960 step 3: the new master never leaves storage-api, and
AI_INTERNAL_API_KEY (outbound) opens nothing here."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import admin.routes as A  # noqa: E402
import auth  # noqa: E402


def test_admin_master_is_master_not_ai_internal(monkeypatch):
    monkeypatch.setattr(A.settings, "API_KEY", "real-master")
    monkeypatch.setenv("AI_INTERNAL_API_KEY", "outbound-only")
    monkeypatch.delenv("ADMIN_API_KEYS", raising=False)
    assert A.require_admin_key("real-master") == "master"
    with pytest.raises(Exception):
        A.require_admin_key("outbound-only")


def test_blast_radius_never_sends_master(monkeypatch):
    from tests.test_artrack_outbound_key import _Db, _fake_httpx
    monkeypatch.setattr(A.settings, "API_KEY", "real-master")
    monkeypatch.delenv("AI_INTERNAL_API_KEY", raising=False)
    monkeypatch.delenv("ARTRACK_API_KEY", raising=False)
    seen = {}
    _fake_httpx(monkeypatch, seen)
    A.blast_radius(7, downstream=True, db=_Db(), caller="test")
    assert seen and "real-master" not in seen.values()


def test_ai_outbound_key_has_no_master_fallback(monkeypatch):
    import importlib
    import config
    monkeypatch.setattr(config.settings, "API_KEY", "real-master")
    monkeypatch.delenv("AI_INTERNAL_API_KEY", raising=False)
    import ai_analysis.service as S
    S = importlib.reload(S)
    assert S.INTERNAL_API_KEY == ""


def test_unset_master_authenticates_nobody(monkeypatch):
    monkeypatch.setattr(auth.settings, "API_KEY", "")
    assert auth.verify_api_key("", None) is None
