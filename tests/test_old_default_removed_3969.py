"""#3969: the retired public default master is recognised by hash only, and no
code path sends it anywhere as a fallback credential."""
import re
from pathlib import Path

from public_default_fixture import PUBLIC_DEFAULT_TEST
from test_example_secrets_3597 import BURNT_KEY_HASHES

ROOT = Path(__file__).resolve().parent.parent


def test_module_hash_is_the_burnt_default():
    src = (ROOT / "storage" / "public_default.py").read_text()
    (h,) = re.findall(r'PUBLIC_DEFAULT_SHA256 = "([0-9a-f]{64})"', src)
    assert h in BURNT_KEY_HASHES


def test_recognition_and_rejection():
    from storage.public_default import is_public_default
    assert is_public_default(PUBLIC_DEFAULT_TEST)
    assert is_public_default(f"  {PUBLIC_DEFAULT_TEST} ".encode())
    for v in (None, "", PUBLIC_DEFAULT_TEST + "x", PUBLIC_DEFAULT_TEST.upper(), 9):
        assert not is_public_default(v)


def test_artrack_cleanup_sends_nothing_without_a_configured_key(monkeypatch):
    import urllib.request
    from storage import routes
    monkeypatch.delenv("ARTRACK_API_KEY", raising=False)
    monkeypatch.delenv("X_API_KEY", raising=False)
    calls = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: calls.append(a))
    routes._notify_artrack_storage_deleted(1)
    assert calls == []
