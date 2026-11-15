"""#3042 (Befund 115): HLS-ZIP playlists cannot reference files outside the
extract dir, extraction has entry/byte/ratio budgets and refuses unsafe
entries, and a rejected ZIP leaves the video's existing HLS output alone."""
import io
import stat
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage import service as S  # noqa: E402

MASTER = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360\n{q}\n"
VARIANT = "#EXTM3U\n#EXTINF:4.0,\n{ts}\n#EXT-X-ENDLIST\n"


def _zip(path: Path, files: dict, symlink: str = None):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            z.writestr(info, "/etc/passwd")
    return path


def _good(tmp_path, q="360p.m3u8", ts="seg0.ts", extra=None):
    files = {"master.m3u8": MASTER.format(q=q), "360p.m3u8": VARIANT.format(ts=ts), "seg0.ts": b"\x47" * 188}
    files.update(extra or {})
    return _zip(tmp_path / "hls.zip", files)


def test_valid_zip_extracts(tmp_path):
    target = tmp_path / "media" / "vid"
    r = S.validate_and_extract_hls_zip(_good(tmp_path), target)
    assert r["valid"], r
    assert (target / "master.m3u8").is_file() and r["quality_streams"] == ["360p.m3u8"]
    assert not any(p.name.startswith(".vid.") for p in target.parent.iterdir())


@pytest.mark.parametrize("q", ["/etc/passwd.m3u8", "../../../../etc/x.m3u8", "a/../../x.m3u8", "/var/lib/storage-api/x.ts"])
def test_master_reference_traversal_rejected(tmp_path, q):
    r = S.validate_and_extract_hls_zip(_good(tmp_path, q=q), tmp_path / "media" / "vid")
    assert not r["valid"] and "Invalid playlist reference" in r["error"]


@pytest.mark.parametrize("ts", ["/etc/hostname.ts", "../../x.ts", "../seg0.ts"])
def test_segment_reference_traversal_rejected(tmp_path, ts):
    r = S.validate_and_extract_hls_zip(_good(tmp_path, ts=ts), tmp_path / "media" / "vid")
    assert not r["valid"] and "Invalid segment reference" in r["error"]


def test_symlink_entry_rejected(tmp_path):
    z = _zip(tmp_path / "s.zip", {"master.m3u8": MASTER.format(q="360p.m3u8")}, symlink="360p.m3u8")
    r = S.validate_and_extract_hls_zip(z, tmp_path / "media" / "vid")
    assert not r["valid"] and "special file" in r["error"]


def test_entry_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_HLS_ZIP_MAX_ENTRIES", "3")
    r = S.validate_and_extract_hls_zip(_good(tmp_path, extra={"x.ts": b"1"}), tmp_path / "media" / "vid")
    assert not r["valid"] and "too many entries" in r["error"]


def test_byte_budget_and_ratio(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_HLS_ZIP_MAX_BYTES", "1000")
    r = S.validate_and_extract_hls_zip(_good(tmp_path, extra={"big.ts": b"\x01\x02" * 2000}), tmp_path / "media" / "v1")
    assert not r["valid"] and "expands beyond" in r["error"]
    monkeypatch.delenv("STORAGE_HLS_ZIP_MAX_BYTES")
    bomb = _zip(tmp_path / "b.zip", {"master.m3u8": "x", "z.ts": b"\x00" * 5_000_000})
    r = S.validate_and_extract_hls_zip(bomb, tmp_path / "media" / "v2")
    assert not r["valid"] and "compression ratio" in r["error"]


def test_failure_keeps_existing_hls_and_leaves_no_staging(tmp_path):
    target = tmp_path / "media" / "vid"
    target.mkdir(parents=True)
    (target / "master.m3u8").write_text("old good playlist")
    r = S.validate_and_extract_hls_zip(_good(tmp_path, q="../../etc/x.m3u8"), target)
    assert not r["valid"]
    assert (target / "master.m3u8").read_text() == "old good playlist"
    assert [p.name for p in target.parent.iterdir()] == ["vid"]


def test_success_replaces_existing(tmp_path):
    target = tmp_path / "media" / "vid"
    target.mkdir(parents=True)
    (target / "stale.ts").write_text("old")
    assert S.validate_and_extract_hls_zip(_good(tmp_path), target)["valid"]
    assert not (target / "stale.ts").exists() and (target / "seg0.ts").exists()
    assert [p.name for p in target.parent.iterdir()] == ["vid"]


def test_route_cleans_up_rejected_zip():
    src = (Path(__file__).resolve().parent.parent / "storage" / "routes.py").read_text()
    assert '"code": "hls_invalid"' in src
    assert "absolute_path_for_key(temp_zip.object_key, temp_zip.tenant_id).unlink(missing_ok=True)" in src
