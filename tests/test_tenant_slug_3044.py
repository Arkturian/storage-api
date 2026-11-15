"""#3044 (Befund 117): tenant ids are slugs before they become path
components; mkdir/rmtree stay inside their base dir."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tenancy.config import validate_tenant_id  # noqa: E402
from storage.service import generic_storage  # noqa: E402

GS = type(generic_storage)


@pytest.mark.parametrize("tid", ["arkturian", "oneal", "annasacher", "koralmbahn", "a", "p3_si-team", "ARKTURIAN"])
def test_valid_slugs(tid):
    assert validate_tenant_id(tid) == tid.lower()


@pytest.mark.parametrize("tid", ["../etc", "a/b", "/abs", "..", ".", "", " ", "a" * 65, "gmail.com", "-x", "x y", "tenant\x00"])
def test_invalid_slugs(tid):
    with pytest.raises(ValueError):
        validate_tenant_id(tid)


def test_tenant_subdir_confined(tmp_path):
    base = tmp_path / "media"
    base.mkdir()
    assert GS._tenant_subdir(base, "t1") == base / "t1"
    with pytest.raises(ValueError):
        GS._tenant_subdir(base, "../media2")
    (base / "evil").symlink_to(tmp_path)
    with pytest.raises(ValueError):
        GS._tenant_subdir(base, "evil")


def test_delete_dirs_never_leaves_base(tmp_path, monkeypatch):
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("x")
    for attr in ("media_dir", "thumbnails_dir", "webview_dir"):
        d = tmp_path / attr
        d.mkdir()
        monkeypatch.setattr(generic_storage, attr, d)
    with pytest.raises(ValueError):
        generic_storage.delete_tenant_directories("../victim")
    assert (victim / "keep.txt").exists()


def test_routes_validate():
    src = (Path(__file__).resolve().parent.parent / "tenancy" / "routes.py").read_text()
    assert src.count('"code": "invalid_tenant_id"') == 3


def test_bind_is_loopback_in_templates():
    root = Path(__file__).resolve().parent.parent
    assert '--host 127.0.0.1 --port $SERVICE_PORT' in (root / ".devops" / "scripts" / "setup-server.sh").read_text()
    assert 'HOST: "127.0.0.1"' in (root / ".devops" / "servers" / "arkserver.yaml").read_text()
