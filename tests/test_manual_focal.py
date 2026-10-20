"""Focal Point von Hand (PUT /storage/media/{id}/focal, OnealServ, 20.10.2026).

YuNet findet nur Gesichter; Banner mit Helm oder Brille haben keinen Punkt.
Der Besitzer setzt ihn von Hand. Er hat Vorrang vor YuNet, eine Neuberechnung
lässt ihn stehen, und die Batch-Route liefert ihn mit.
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
from models import Base, StorageObject  # noqa: E402
from storage import routes as storage_routes  # noqa: E402

BESITZER = 7
FREMDER = 8


@pytest.fixture
def umgebung(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    for oid, mime in ((35383, "image/jpeg"), (35384, "image/jpeg"), (35385, "video/mp4")):
        s.add(StorageObject(
            id=oid, tenant_id="oneal", owner_user_id=BESITZER, object_key=f"k{oid}",
            original_filename=f"b{oid}.jpg", file_url=f"/x/{oid}", mime_type=mime,
            file_size_bytes=1, is_public=True, ai_context_metadata={"collection_id": "c"},
        ))
    s.commit()
    s.close()

    wer = {"id": BESITZER}

    def _db():
        d = Session()
        try:
            yield d
        finally:
            d.close()

    def _user():
        return types.SimpleNamespace(id=wer["id"], trust_level="user", api_key=None, email="x@y")

    def _gesicht(obj, db, object_id):
        meta = dict(obj.ai_context_metadata or {})
        meta["focal_point"] = {"focal_point": {"x_pct": 10.0, "y_pct": 20.0}, "faces_detected": 1,
                               "model": "fake", "computed_at": "2026-10-20T00:00:00Z"}
        db.query(StorageObject).filter(StorageObject.id == object_id).update(
            {"ai_context_metadata": meta}, synchronize_session=False)
        db.commit()
        obj.ai_context_metadata = meta
        return meta["focal_point"]

    monkeypatch.setattr(storage_routes, "_compute_and_store_focal", _gesicht)
    monkeypatch.setattr(storage_routes, "_check_quarantine", lambda *a, **k: None)
    main.app.dependency_overrides[get_db] = _db
    main.app.dependency_overrides[storage_routes.get_current_user] = _user
    main.app.dependency_overrides[storage_routes.get_tenant_id] = lambda: "oneal"
    main.app.dependency_overrides[storage_routes.get_current_user_optional] = lambda: None
    main.app.dependency_overrides[storage_routes.get_tenant_id_optional] = lambda: None
    try:
        yield TestClient(main.app), wer
    finally:
        main.app.dependency_overrides.clear()


def test_put_ohne_gesicht_und_get_liefert_manuell(umgebung):
    c, _ = umgebung
    r = c.put("/storage/media/35383/focal", json={"x_pct": 50, "y_pct": 66, "note": "Brille"})
    assert r.status_code == 200, r.text
    assert r.json()["focal_point"] == {"x_pct": 50.0, "y_pct": 66.0, "source": "manual"}
    g = c.get("/storage/media/35383/focal").json()
    assert g["source"] == "manual" and g["note"] == "Brille"
    assert g["focal_point"]["y_pct"] == 66.0
    assert "faces_detected" not in g  # mit manuellem Punkt läuft keine Erkennung


def test_neuberechnung_laesst_manuell_stehen(umgebung):
    c, _ = umgebung
    c.put("/storage/media/35383/focal", json={"x_pct": 30, "y_pct": 40})
    p = c.post("/storage/media/35383/focal").json()
    assert p["focal_point"]["source"] == "manual" and p["faces_detected"] == 1
    assert c.get("/storage/media/35383/focal").json()["focal_point"]["x_pct"] == 30.0


def test_delete_zurueck_zur_automatik(umgebung):
    c, _ = umgebung
    c.post("/storage/media/35383/focal")
    c.put("/storage/media/35383/focal", json={"x_pct": 30, "y_pct": 40})
    d = c.delete("/storage/media/35383/focal/manual").json()
    assert d["manual_removed"] is True
    assert d["focal"]["focal_point"] == {"x_pct": 10.0, "y_pct": 20.0, "source": "face"}
    assert c.get("/storage/media/35383/focal").json()["source"] == "face"
    assert c.delete("/storage/media/35383/focal/manual").json()["manual_removed"] is False


def test_batch_liefert_manuell_auch_ohne_generate(umgebung):
    c, _ = umgebung
    c.put("/storage/media/35383/focal", json={"x_pct": 50, "y_pct": 66})
    b = c.get("/storage/batch/focal?id=35383&id=35384").json()
    assert b["focal_points"]["35383"]["focal_point"]["source"] == "manual"
    assert "35384" not in b["focal_points"]


@pytest.mark.parametrize("body", [{"x_pct": 101, "y_pct": 5}, {"x_pct": -1, "y_pct": 5}, {"x_pct": 5}])
def test_ungueltige_werte_422(umgebung, body):
    c, _ = umgebung
    assert c.put("/storage/media/35383/focal", json=body).status_code == 422


def test_fremder_darf_nicht(umgebung):
    c, wer = umgebung
    wer["id"] = FREMDER
    assert c.put("/storage/media/35383/focal", json={"x_pct": 5, "y_pct": 5}).status_code == 404
    assert c.delete("/storage/media/35383/focal/manual").status_code == 404


def test_nur_bilder(umgebung):
    c, _ = umgebung
    assert c.put("/storage/media/35385/focal", json={"x_pct": 5, "y_pct": 5}).status_code == 400
