"""#2913: client file names never leave the tenant media dir, the tts_ cache
shortcut only takes plain names, and the legacy AI worker only reads files
that belong to the queued object."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage.service import generic_storage  # noqa: E402

GS = type(generic_storage)


@pytest.mark.parametrize("name", [
    "tts_minimax_06e9f0958c43.mp3",
    "tts_story-sage-waechter-s2-1786375590_elevenlabs.mp3",
    "tts_spatial-audio-1763294587.888688_openai.mp3",
    "tts_test-1_openai.mp3",
])
def test_existing_tts_cache_names_kept(name):
    assert generic_storage._create_filename(name, 1, None) == name


@pytest.mark.parametrize("name", [
    "tts_aaaaaaaaaaaaaaaaaaaaaa/../../../../etc/cron.d/x",
    "tts_aaaaaaaaaaaaaaaaaaaaaa/../../x.mp3",
    "tts_aaaaaaaaaaaaaaaa..mp3/../x",
    "tts_aaaaaaaaaaaaaaaaaaaa\\..\\..\\x.mp3",
    "tts_/etc/passwd_aaaaaaaaaaaaaa.mp3",
])
def test_tts_traversal_not_verbatim(name):
    out = generic_storage._create_filename(name, 1, None)
    assert out != name
    assert "/" not in out and "\\" not in out and ".." not in out


@pytest.mark.parametrize("name", ["../../evil.mp3", "/etc/x.jpg", "a/b/c.png", "x.jp g", "x.mp3/../../y"])
def test_generic_names_are_single_component(name):
    out = generic_storage._create_filename(name, 1, "ctx")
    assert Path(out).name == out and ".." not in out


def test_confined_refuses_escape(tmp_path):
    with pytest.raises(ValueError):
        GS._confined(tmp_path, "../x")
    with pytest.raises(ValueError):
        GS._confined(tmp_path, "a/b")
    (tmp_path / "link").symlink_to("/etc/passwd")
    with pytest.raises(ValueError):
        GS._confined(tmp_path, "link")
    assert GS._confined(tmp_path, "ok.mp3") == tmp_path.resolve() / "ok.mp3"


def test_no_client_name_in_external_ai_temp_path():
    src = (Path(__file__).resolve().parent.parent / "storage" / "service.py").read_text()
    assert 'f"/tmp/external_ai_{storage_obj.id}_{storage_obj.original_filename}"' not in src


# --------------------------------------------------------------------------- #
# scripts/ai_worker.py
# --------------------------------------------------------------------------- #

@pytest.fixture
def worker(tmp_path, monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import ai_worker as W
    media = tmp_path / "media"
    media.mkdir()
    own = media / "obj7.jpg"
    own.write_bytes(b"x")
    other = media / "obj8.jpg"
    other.write_bytes(b"y")
    scratch = tmp_path / "scratch"
    (scratch / "ai_thumbs_7").mkdir(parents=True)
    (scratch / "ai_thumbs_8").mkdir(parents=True)
    monkeypatch.setenv("AI_SCRATCH_DIR", str(scratch))
    monkeypatch.setattr(generic_storage, "absolute_path_for_key", lambda key, t: media / key)

    class _Q:
        def filter(self, *a, **k):
            return self

        def first(self):
            return SimpleNamespace(id=7, object_key="obj7.jpg", tenant_id="arkturian")

    class _DB:
        def query(self, *a):
            return _Q()

        def close(self):
            pass

    monkeypatch.setattr(W, "get_db", lambda: iter([_DB()]))
    return SimpleNamespace(W=W, own=own, other=other, scratch=scratch)


def test_worker_accepts_own_file_and_scratch(worker):
    assert worker.W._validated_content_path(7, worker.own) == worker.own.resolve()
    assert worker.W._validated_content_path(7, worker.scratch / "ai_thumbs_7").name == "ai_thumbs_7"


@pytest.mark.parametrize("pick", ["other", "etc", "foreign_scratch", "traversal"])
def test_worker_refuses_foreign_paths(worker, pick):
    path = {
        "other": worker.other,
        "etc": Path("/etc/hostname"),
        "foreign_scratch": worker.scratch / "ai_thumbs_8",
        "traversal": worker.scratch / "ai_thumbs_7" / ".." / "ai_thumbs_8",
    }[pick]
    with pytest.raises(ValueError):
        worker.W._validated_content_path(7, path)
