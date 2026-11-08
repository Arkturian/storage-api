"""Reference mode darf keine beliebige Serverdatei registrieren (storage.db, Schlüssel)."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import HTTPException  # noqa: E402

from storage import routes as R  # noqa: E402


@pytest.fixture
def roots(tmp_path, monkeypatch):
    erlaubt = tmp_path / "erlaubt"
    anna = tmp_path / "anna"
    geheim = tmp_path / "geheim"
    for d in (erlaubt, anna, geheim):
        d.mkdir()
    (erlaubt / "foto.jpg").write_bytes(b"x")
    (anna / "bild.jpg").write_bytes(b"x")
    (geheim / "storage.db").write_bytes(b"keys")
    (erlaubt / "flucht").symlink_to(geheim)
    monkeypatch.setenv("STORAGE_REFERENCE_ROOTS", str(erlaubt))
    monkeypatch.setenv("STORAGE_REFERENCE_TENANT_ROOTS", f"annasacher={anna}")
    return SimpleNamespace(erlaubt=erlaubt, anna=anna, geheim=geheim)


def _admin(monkeypatch, ja: bool):
    monkeypatch.setattr(R, "_is_privileged_admin", lambda u, db: ja)


def _tenant(monkeypatch, name):
    monkeypatch.setattr(R, "tenant_id_for_api_key", lambda key, db: name)


USER = SimpleNamespace(id=5, api_key="k", trust_level="user")


def _code(fn):
    with pytest.raises(HTTPException) as e:
        fn()
    assert e.value.status_code == 403
    return e.value.detail["code"]


def test_copy_upload_unberuehrt(monkeypatch):
    _admin(monkeypatch, False)
    _tenant(monkeypatch, None)
    R._check_reference_upload("copy", None, USER, object())


def test_admin_unter_erlaubter_wurzel(roots, monkeypatch):
    _admin(monkeypatch, True)
    R._check_reference_upload("reference", str(roots.erlaubt / "foto.jpg"), USER, object())


@pytest.mark.parametrize("pfad", ["geheim/storage.db", "erlaubt/../geheim/storage.db", "erlaubt/flucht/storage.db"])
def test_admin_ausserhalb_oder_ausbruch(roots, monkeypatch, tmp_path, pfad):
    _admin(monkeypatch, True)
    assert _code(lambda: R._check_reference_upload("reference", str(tmp_path / pfad), USER, object())) \
        == "reference_path_forbidden"


def test_admin_systempfad(roots, monkeypatch):
    _admin(monkeypatch, True)
    assert _code(lambda: R._check_reference_upload("reference", "/var/lib/storage-api/storage.db", USER, object())) \
        == "reference_path_forbidden"


def test_fremder_tenantschluessel_ohne_wurzel(roots, monkeypatch):
    _admin(monkeypatch, False)
    _tenant(monkeypatch, "arkturian")
    assert _code(lambda: R._check_reference_upload("reference", str(roots.erlaubt / "foto.jpg"), USER, object())) \
        == "reference_forbidden"


def test_reference_path_ohne_modus_zaehlt_auch(roots, monkeypatch):
    _admin(monkeypatch, False)
    _tenant(monkeypatch, "arkturian")
    assert _code(lambda: R._check_reference_upload("copy", str(roots.geheim / "storage.db"), USER, object())) \
        == "reference_forbidden"


def test_annasacher_nur_eigene_wurzel(roots, monkeypatch):
    _admin(monkeypatch, False)
    _tenant(monkeypatch, "annasacher")
    R._check_reference_upload("reference", str(roots.anna / "bild.jpg"), USER, object())
    assert _code(lambda: R._check_reference_upload("reference", str(roots.erlaubt / "foto.jpg"), USER, object())) \
        == "reference_path_forbidden"


def test_on_behalf_nie(roots, monkeypatch):
    _admin(monkeypatch, False)
    _tenant(monkeypatch, "annasacher")
    obo = SimpleNamespace(id=2, api_key="k", trust_level="user", _storage_on_behalf_of=True)
    assert _code(lambda: R._check_reference_upload("reference", str(roots.anna / "bild.jpg"), obo, object())) \
        == "reference_forbidden"


def test_route_weist_ab_bevor_gelesen_wird(roots, monkeypatch, tmp_path):
    """Ende-zu-Ende über die Route: ein normaler Schlüssel bekommt 403, nichts wird angelegt."""
    import main
    from fastapi.testclient import TestClient
    from database import get_db
    _admin(monkeypatch, False)
    _tenant(monkeypatch, "arkturian")
    main.app.dependency_overrides[get_db] = lambda: iter([None])
    main.app.dependency_overrides[R.get_current_user] = lambda: USER
    main.app.dependency_overrides[R.get_tenant_id] = lambda: "arkturian"
    try:
        r = TestClient(main.app).post("/storage/upload", data={
            "storage_mode": "reference", "reference_path": str(roots.geheim / "storage.db"), "original_filename": "x"})
    finally:
        main.app.dependency_overrides.clear()
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "reference_forbidden"
