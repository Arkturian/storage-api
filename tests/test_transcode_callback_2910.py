"""#2910: /storage/transcode/callback needs a secret, a live transcoding object
and an output_dir strictly below the allowed roots; it never deletes a parent."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from storage import routes as R  # noqa: E402
from database import get_db  # noqa: E402

SECRET = "cb-secret-2910"


class _Q:
    def __init__(self, obj):
        self.obj = obj

    def filter(self, *a, **k):
        return self

    def first(self):
        return self.obj


class _DB:
    def __init__(self, obj):
        self.obj = obj
        self.commits = 0

    def query(self, *a):
        return _Q(self.obj)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "work"
    job = root / "sync_abc" / "output"
    job.mkdir(parents=True)
    (job / "master.m3u8").write_text("#EXTM3U\n")
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "storage.db").write_text("precious")
    media = tmp_path / "media"
    media.mkdir()
    monkeypatch.setenv("TRANSCODE_CALLBACK_OUTPUT_ROOTS", str(root))
    monkeypatch.setenv("TRANSCODE_CALLBACK_SECRET", SECRET)

    obj = SimpleNamespace(id=7, object_key="abc.mp4", tenant_id="arkturian",
                          transcoding_status="processing", transcoding_progress=0,
                          transcoding_error=None, hls_url=None, metadata_json={})
    db = _DB(obj)
    import storage.service as S
    monkeypatch.setattr(S.generic_storage, "absolute_path_for_key", lambda key, t: media / key)
    monkeypatch.setattr(R, "build_hls_url", lambda t, b: f"https://x/{b}/master.m3u8")
    monkeypatch.setattr("sqlalchemy.orm.attributes.flag_modified", lambda o, k: None)

    app = FastAPI()
    app.include_router(R.router)
    app.dependency_overrides[get_db] = lambda: db
    prefix = R.router.prefix or ""
    return SimpleNamespace(client=TestClient(app), url=f"{prefix}/transcode/callback",
                           root=root, job=job, victim=victim, media=media, obj=obj, db=db)


def _body(**kw):
    b = {"job_id": "sync_abc", "status": "completed", "storage_object_id": 7}
    b.update(kw)
    return b


def test_without_secret_config_disabled(env, monkeypatch):
    monkeypatch.delenv("TRANSCODE_CALLBACK_SECRET")
    r = env.client.post(env.url, json=_body(), headers={"X-Transcode-Callback-Secret": SECRET})
    assert r.status_code == 403
    assert r.json()["detail"] == "transcode_callback_disabled"


@pytest.mark.parametrize("hdr", [None, "wrong"])
def test_wrong_or_missing_secret(env, hdr):
    headers = {"X-Transcode-Callback-Secret": hdr} if hdr else {}
    r = env.client.post(env.url, json=_body(), headers=headers)
    assert r.status_code == 401
    assert env.db.commits == 0


def test_output_dir_outside_root_refused_and_nothing_deleted(env):
    target = env.victim  # rmtree(parent) used to wipe env.victim's parent
    r = env.client.post(env.url, json=_body(output_dir=str(target)),
                        headers={"X-Transcode-Callback-Secret": SECRET})
    assert r.status_code == 403
    assert r.json()["detail"] == "output_dir_not_allowed"
    assert (env.victim / "storage.db").read_text() == "precious"
    assert env.obj.transcoding_status == "processing"


@pytest.mark.parametrize("rel", ["../victim", "sync_abc/output/../../../victim"])
def test_traversal_out_of_root_refused(env, rel):
    r = env.client.post(env.url, json=_body(output_dir=str(env.root / rel)),
                        headers={"X-Transcode-Callback-Secret": SECRET})
    assert r.status_code == 403
    assert (env.victim / "storage.db").exists()


def test_symlink_out_of_root_refused(env):
    (env.root / "flucht").symlink_to(env.victim)
    r = env.client.post(env.url, json=_body(output_dir=str(env.root / "flucht")),
                        headers={"X-Transcode-Callback-Secret": SECRET})
    assert r.status_code == 403
    assert (env.victim / "storage.db").exists()


def test_root_itself_refused(env):
    r = env.client.post(env.url, json=_body(output_dir=str(env.root)),
                        headers={"X-Transcode-Callback-Secret": SECRET})
    assert r.status_code == 403


def test_object_not_transcoding_refused(env):
    env.obj.transcoding_status = "completed"
    r = env.client.post(env.url, json=_body(output_dir=str(env.job)),
                        headers={"X-Transcode-Callback-Secret": SECRET})
    assert r.status_code == 409


def test_invalid_job_id(env):
    r = env.client.post(env.url, json=_body(job_id="../x"),
                        headers={"X-Transcode-Callback-Secret": SECRET})
    assert r.status_code == 400


def test_valid_callback_copies_and_removes_only_output_dir(env):
    r = env.client.post(env.url, json=_body(output_dir=str(env.job)),
                        headers={"X-Transcode-Callback-Secret": SECRET})
    assert r.status_code == 200, r.text
    assert (env.media / "abc" / "master.m3u8").exists()
    assert not env.job.exists()               # the output dir itself is cleaned
    assert (env.root / "sync_abc").exists()   # its parent is left alone
    assert env.obj.transcoding_status == "completed"
    assert env.obj.hls_url.endswith("/abc/master.m3u8")
