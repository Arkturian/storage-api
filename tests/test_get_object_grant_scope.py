"""GET /storage/objects/{id} zeigt gebundene Scopes (#2568).

Vorher lieferte der Abruf immer grant_scope=null, auch wenn media_grants
eine Bindung hatte (ELE, Objekt 98, Ticket-Upload). Agenten hielten die
Datei deshalb für ungebunden. Besitzer und echte Admins sehen die Scopes,
alle anderen weiter null.
"""
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import main  # noqa: E402
from database import get_db  # noqa: E402
from models import Base, MediaGrant, StorageObject  # noqa: E402
from storage import routes as storage_routes  # noqa: E402

BESITZER = 7
FREMDER = 8
SCOPE_X = "cloud-session-x:" + "ab" * 16


@pytest.fixture
def umgebung(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    for oid in (98, 99):
        s.add(StorageObject(
            id=oid, tenant_id="ele", owner_user_id=BESITZER, object_key=f"k{oid}",
            original_filename=f"d{oid}.pdf", file_url=f"/x/{oid}", mime_type="application/pdf",
            file_size_bytes=1, is_public=True,
        ))
    s.add(MediaGrant(object_id=98, scope=SCOPE_X, granted_by_user_id=BESITZER, granted_via="upload"))
    s.add(MediaGrant(object_id=98, scope="content-post:5226", granted_by_user_id=BESITZER, granted_via="admin"))
    s.commit()
    s.close()

    wer = {"id": BESITZER, "admin": False}

    def _db():
        d = Session()
        try:
            yield d
        finally:
            d.close()

    def _user():
        if wer["id"] is None:
            return None
        return types.SimpleNamespace(id=wer["id"], trust_level="user", api_key=None, email="x@y")

    monkeypatch.setattr(storage_routes, "_is_privileged_admin", lambda u, db: bool(u) and wer["admin"])
    main.app.dependency_overrides[get_db] = _db
    main.app.dependency_overrides[storage_routes.get_current_user_optional] = _user
    main.app.dependency_overrides[storage_routes.get_tenant_id_optional] = lambda: "ele"
    try:
        yield TestClient(main.app), wer
    finally:
        main.app.dependency_overrides.clear()


def test_besitzer_sieht_scopes(umgebung):
    c, _ = umgebung
    r = c.get("/storage/objects/98")
    assert r.status_code == 200, r.text
    assert r.json()["grant_scope"] == f"{SCOPE_X},content-post:5226"


def test_ohne_bindung_null(umgebung):
    c, _ = umgebung
    assert c.get("/storage/objects/99").json()["grant_scope"] is None


def test_admin_sieht_scopes(umgebung):
    c, wer = umgebung
    wer.update(id=FREMDER, admin=True)
    assert c.get("/storage/objects/98").json()["grant_scope"].startswith(SCOPE_X)


@pytest.mark.parametrize("uid", [FREMDER, None])
def test_fremde_und_anonym_sehen_nichts(umgebung, uid):
    c, wer = umgebung
    wer["id"] = uid
    r = c.get("/storage/objects/98")
    assert r.status_code == 200
    assert r.json()["grant_scope"] is None
