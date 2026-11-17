"""#3243: storage-api has its own /tmp since #3230, the workers do not.
Progress files and external-mode hand-offs go through the shared scratch root."""
import os
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_status_dir_under_scratch_root(monkeypatch, tmp_path):
    from storage.ai_status import ai_status_dir
    monkeypatch.delenv("AI_STATUS_DIR", raising=False)
    monkeypatch.setenv("AI_SCRATCH_DIR", str(tmp_path))
    assert ai_status_dir(42) == tmp_path / "status" / "ai_analysis_status_42"
    assert not str(ai_status_dir(42)).startswith("/tmp/ai_analysis_status_")
    monkeypatch.setenv("AI_STATUS_DIR", str(tmp_path / "x"))
    assert ai_status_dir("7") == tmp_path / "x" / "ai_analysis_status_7"


def _cleanup():
    from storage.ai_status import cleanup_scratch_dir
    return cleanup_scratch_dir


def test_cleanup_removes_handed_over_file_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("AI_SCRATCH_DIR", str(tmp_path))
    d = tmp_path / "ai_images_5"; d.mkdir()
    f = d / "external_abc.txt"; f.write_text("x")
    _cleanup()(str(f))
    assert not d.exists()


def test_cleanup_never_touches_stored_file(monkeypatch, tmp_path):
    monkeypatch.setenv("AI_SCRATCH_DIR", str(tmp_path / "scratch"))
    media = tmp_path / "media" / "arkturian"; media.mkdir(parents=True)
    f = media / "u1_doc.txt"; f.write_text("keep")
    _cleanup()(str(f))
    assert f.exists()
    # an ai_images_ dir outside the scratch root is not ours either
    other = tmp_path / "elsewhere" / "ai_images_9"; other.mkdir(parents=True)
    g = other / "x.txt"; g.write_text("keep")
    _cleanup()(str(g))
    assert g.exists()


def test_cleanup_dir_case_unchanged(monkeypatch, tmp_path):
    monkeypatch.setenv("AI_SCRATCH_DIR", str(tmp_path))
    d = tmp_path / "ai_thumbs_3"; d.mkdir(); (d / "a.jpg").write_text("x")
    _cleanup()(str(d))
    assert not d.exists()
