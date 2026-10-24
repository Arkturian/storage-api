"""Ausdruecklich gesetzter STORAGE_UPLOAD_DIR weicht nie in den Code-Baum aus.

P3 24.10.2026: Die Nutzdaten zogen auf ein eingehaengtes Datenvolumen. Ist es
nicht beschreibbar (macOS-TCC, nicht eingehaengt), fiel GenericStorageService
still auf ./uploads/storage zurueck und schrieb neben den Code.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _unbeschreibbar(tmp_path):
    sperre = tmp_path / "keinordner"
    sperre.write_text("x")          # mkdir darunter scheitert
    return sperre / "storage"


def test_gesetzter_ort_scheitert_laut(tmp_path, monkeypatch):
    from storage import service
    ziel = _unbeschreibbar(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STORAGE_UPLOAD_DIR", str(ziel))
    monkeypatch.setattr(service.settings, "STORAGE_UPLOAD_DIR", str(ziel))
    monkeypatch.delenv("DISABLE_STORAGE_INIT", raising=False)
    with pytest.raises(RuntimeError, match="kein Rueckfall"):
        service.GenericStorageService()
    assert not (tmp_path / "uploads").exists()


def test_vorgabe_ohne_variable_bleibt_wie_bisher(tmp_path, monkeypatch):
    from storage import service
    ziel = _unbeschreibbar(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("STORAGE_UPLOAD_DIR", raising=False)
    monkeypatch.setattr(service.settings, "STORAGE_UPLOAD_DIR", str(ziel))
    monkeypatch.delenv("DISABLE_STORAGE_INIT", raising=False)
    s = service.GenericStorageService()
    assert s.upload_root == Path("./uploads/storage")
