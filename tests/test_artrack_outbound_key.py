"""#2385: storage-api schickt ARTrack einen eigenen Schlüssel (ARTRACK_API_KEY).

Nach dem Tausch des ARTrack-Hauptschlüssels gilt der geteilte Wert dort nicht
mehr. blast-radius und der Aufräum-Ruf nach dem Löschen nehmen deshalb
ARTRACK_API_KEY; knowledge-api bekommt weiter AI_INTERNAL_API_KEY. Ohne die
neue Variable bleibt alles wie bisher.
"""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import admin.routes as admin_routes  # noqa: E402
import storage.routes as storage_routes  # noqa: E402


class _Antwort:
    status_code = 200

    def json(self):
        return {"total": 0}


def _fake_httpx(monkeypatch, gesehen):
    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, headers=None):
            gesehen[url] = headers["X-API-KEY"]
            return _Antwort()

    monkeypatch.setitem(sys.modules, "httpx", types.SimpleNamespace(Client=_Client))


class _Db:
    def query(self, _model):
        return self

    def filter(self, *_a):
        return self

    def first(self):
        return types.SimpleNamespace(
            object_key="k", storage_mode="local", mime_type="image/png",
            hls_url=None, tombstoned_at=None,
        )


def _schluessel(gesehen):
    artrack = next(v for u, v in gesehen.items() if "/admin/storage/" in u)
    knowledge = next(v for u, v in gesehen.items() if "/admin/refs/storage/" in u)
    return artrack, knowledge


def test_blast_radius_nimmt_eigenen_artrack_schluessel(monkeypatch):
    monkeypatch.setenv("AI_INTERNAL_API_KEY", "geteilt")
    monkeypatch.setenv("ARTRACK_API_KEY", "nur-artrack")
    gesehen = {}
    _fake_httpx(monkeypatch, gesehen)
    admin_routes.blast_radius(7, downstream=True, db=_Db(), caller="test")
    assert _schluessel(gesehen) == ("nur-artrack", "geteilt")


def test_blast_radius_ohne_eigenen_schluessel_wie_bisher(monkeypatch):
    monkeypatch.setenv("AI_INTERNAL_API_KEY", "geteilt")
    monkeypatch.delenv("ARTRACK_API_KEY", raising=False)
    gesehen = {}
    _fake_httpx(monkeypatch, gesehen)
    admin_routes.blast_radius(7, downstream=True, db=_Db(), caller="test")
    assert _schluessel(gesehen) == ("geteilt", "geteilt")


def test_aufraeumen_nach_loeschen_nimmt_artrack_schluessel(monkeypatch):
    import urllib.request

    monkeypatch.setenv("ARTRACK_API_KEY", "nur-artrack")
    gesendet = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"refs_cleaned": {}}'

    def _urlopen(req, timeout=None):
        gesendet["url"] = req.full_url
        gesendet["key"] = req.get_header("X-api-key")
        return _Resp()

    class _SofortThread:
        def __init__(self, target, **k):
            self._t = target

        def start(self):
            self._t()

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    monkeypatch.setattr("threading.Thread", _SofortThread)
    storage_routes._notify_artrack_storage_deleted(7)
    assert gesendet["url"].endswith("/admin/storage/7/cleanup-refs")
    assert gesendet["key"] == "nur-artrack"
