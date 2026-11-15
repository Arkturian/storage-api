"""#3043 (Befund 116): a stored reference_path is re-validated when read; a
file swapped for a symlink out of the allowed roots is not served."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage import routes as R  # noqa: E402


@pytest.fixture
def roots(tmp_path, monkeypatch):
    allowed = tmp_path / "sync"
    tenant_root = tmp_path / "anna"
    secret = tmp_path / "secret"
    for d in (allowed, tenant_root, secret):
        d.mkdir()
    (secret / "storage.db").write_text("ALL THE KEYS")
    monkeypatch.setenv("STORAGE_REFERENCE_ROOTS", str(allowed))
    monkeypatch.setenv("STORAGE_REFERENCE_TENANT_ROOTS", f"annasacher={tenant_root}")
    monkeypatch.setenv("OPENCLOUD_VIEWS_ROOT", str(tmp_path / "views"))
    monkeypatch.setenv("OPENCLOUD_PROJECTS_ROOT", str(tmp_path / "projects"))
    return SimpleNamespace(allowed=allowed, tenant=tenant_root, secret=secret, tmp=tmp_path)


def _obj(path, tenant="arkturian"):
    return SimpleNamespace(id=1, tenant_id=tenant, reference_path=str(path))


def test_regular_file_in_root_served(roots):
    f = roots.allowed / "a.jpg"
    f.write_bytes(b"x")
    assert R._reference_path_at_read(_obj(f)) == f.resolve()


def test_tenant_root_only_for_that_tenant(roots):
    f = roots.tenant / "b.jpg"
    f.write_bytes(b"x")
    assert R._reference_path_at_read(_obj(f, "annasacher")) == f.resolve()
    assert R._reference_path_at_read(_obj(f, "oneal")) is None


def test_swapped_symlink_out_of_root_refused(roots):
    f = roots.allowed / "c.jpg"
    f.write_bytes(b"x")
    assert R._reference_path_at_read(_obj(f)) is not None
    f.unlink()
    f.symlink_to(roots.secret / "storage.db")       # the TOCTOU swap
    assert R._reference_path_at_read(_obj(f)) is None


def test_symlink_inside_root_returns_real_file(roots):
    real = roots.allowed / "real.jpg"
    real.write_bytes(b"x")
    link = roots.allowed / "link.jpg"
    link.symlink_to(real)
    assert R._reference_path_at_read(_obj(link)) == real.resolve()


@pytest.mark.parametrize("what", ["dir", "missing", "dotdot"])
def test_non_files_refused(roots, what):
    p = {"dir": roots.allowed, "missing": roots.allowed / "nope.jpg",
         "dotdot": roots.allowed / ".." / "secret" / "storage.db"}[what]
    assert R._reference_path_at_read(_obj(p)) is None


def test_opencloud_views_allowed(roots):
    v = roots.tmp / "views" / "space"
    v.mkdir(parents=True)
    f = v / "doc.pdf"
    f.write_bytes(b"%PDF")
    assert R._reference_path_at_read(_obj(f)) == f.resolve()


def test_resolver_uses_read_check():
    src = (Path(__file__).resolve().parent.parent / "storage" / "routes.py").read_text()
    assert "real = _reference_path_at_read(obj)" in src
