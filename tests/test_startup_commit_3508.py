"""#3508: GET /health reports the running commit, read once at start, never guessed."""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from startup_commit import read_startup_commit  # noqa: E402

SHA = "4d400b4" + "a" * 33


def _git(d, *a):
    subprocess.run(["git", "-C", str(d), *a], check=True, capture_output=True)


def _repo(d):
    d.mkdir(parents=True, exist_ok=True)
    _git(d, "init", "-q")
    _git(d, "config", "user.email", "t@t")
    _git(d, "config", "user.name", "t")
    (d / "main.py").write_text("v1\n")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "c1")
    return subprocess.run(["git", "-C", str(d), "rev-parse", "HEAD"], capture_output=True,
                          text=True, check=True).stdout.strip()


def test_marker_wins(tmp_path):
    (tmp_path / ".deployed-rev").write_text(SHA + "\n")
    assert read_startup_commit(tmp_path) == SHA


def test_marker_beats_stale_git_head(tmp_path):
    """arkserver: live dir is a checkout at an old HEAD, files are the release."""
    _repo(tmp_path)
    (tmp_path / "main.py").write_text("released\n")
    (tmp_path / ".deployed-rev").write_text(SHA)
    assert read_startup_commit(tmp_path) == SHA


def test_bad_marker_is_null_without_git_fallback(tmp_path):
    head = _repo(tmp_path)
    for junk in ("", "4d400b4", "latest", SHA.upper(), SHA + "0"):
        (tmp_path / ".deployed-rev").write_text(junk)
        assert read_startup_commit(tmp_path) is None, junk
    assert head  # git would have answered, it must not be asked


def test_clean_checkout_without_marker(tmp_path):
    head = _repo(tmp_path)
    assert read_startup_commit(tmp_path) == head


def test_modified_checkout_is_null(tmp_path):
    _repo(tmp_path)
    (tmp_path / "main.py").write_text("changed\n")
    assert read_startup_commit(tmp_path) is None


def test_subdir_of_other_repo_is_null(tmp_path):
    _repo(tmp_path)
    sub = tmp_path / "live"
    sub.mkdir()
    assert read_startup_commit(sub) is None


def test_no_marker_no_git_is_null(tmp_path):
    assert read_startup_commit(tmp_path / "nowhere") is None


def test_health_reports_startup_commit(monkeypatch):
    import main
    from fastapi.testclient import TestClient
    monkeypatch.setattr(main, "STARTUP_COMMIT", SHA)
    r = TestClient(main.app).get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "healthy", "service": "storage-api", "commit": SHA}
    monkeypatch.setattr(main, "STARTUP_COMMIT", None)
    assert TestClient(main.app).get("/health").json()["commit"] is None
