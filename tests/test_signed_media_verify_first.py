"""Eine falsche Signatur verrät nicht, ob es eine Nummer gibt.

/storage/media/{id} fragte erst die Datenbank (404) und prüfte danach die
Signatur (403). Mit irgendeiner `sig` sah man von außen, welche Nummern
existieren (Befund ELE Cloud, 20.10.2026). Jetzt kommt die Signatur zuerst:
falsch oder abgelaufen heißt 403, ohne dass das Objekt gesucht wird.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
from database import get_db  # noqa: E402
from storage import routes as storage_routes  # noqa: E402


class _Db:
    """Merkt sich jede Abfrage; ein Objekt gibt es nie."""

    def __init__(self, abfragen):
        self._abfragen = abfragen

    def query(self, model):
        self._abfragen.append(getattr(model, "__name__", str(model)))
        return self

    def filter(self, *_a, **_k):
        return self

    def first(self):
        return None

    def close(self):
        pass


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("STORAGE_SIGN_SECRETS", '{"k1": "' + "x" * 40 + '"}')
    monkeypatch.setenv("STORAGE_SIGN_ACTIVE_KID", "k1")
    abfragen = []
    monkeypatch.setattr(main, "SessionLocal", lambda: _Db(abfragen))
    main.app.dependency_overrides[get_db] = lambda: _Db(abfragen)
    main.app.dependency_overrides[storage_routes.get_current_user_optional] = lambda: None
    main.app.dependency_overrides[storage_routes.get_tenant_id_optional] = lambda: None
    try:
        yield TestClient(main.app), abfragen
    finally:
        main.app.dependency_overrides.clear()


@pytest.mark.parametrize("methode", ["GET", "HEAD"])
def test_falsche_signatur_ist_403_ohne_suche(client, methode):
    c, abfragen = client
    r = c.request(methode, "/storage/media/999999999?exp=1&kid=k1&sig=falsch")
    assert r.status_code == 403
    assert "StorageObject" not in abfragen


@pytest.mark.parametrize("methode", ["GET", "HEAD"])
def test_ohne_signatur_bleibt_404(client, methode):
    c, abfragen = client
    r = c.request(methode, "/storage/media/999999999")
    assert r.status_code == 404
    assert "StorageObject" in abfragen
