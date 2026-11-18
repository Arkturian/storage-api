"""#3406: since #3040 the 3D-API converts only with a key from UPLOAD_API_KEYS.
storage-api must send its own key (THREED_API_KEY) as X-API-KEY on
POST /optimize/glb, and nothing when no key is configured."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from models import Base, StorageObject, User  # noqa: E402

GLB = b"glTF" + b"\x02\x00\x00\x00" + b"\x00" * 64


class _FakeResp:
    status_code = 200
    content = b"glTF-optimised"

    def json(self):
        return {}


@pytest.fixture
def env(tmp_path, monkeypatch):
    from storage import routes as R
    from storage.service import generic_storage
    up = tmp_path / "up"
    (up / "media" / "arkturian").mkdir(parents=True)
    for attr, sub in (("upload_root", ""), ("media_dir", "media"), ("thumbnails_dir", "thumbnails"), ("webview_dir", "webview")):
        p = up / sub if sub else up
        p.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(generic_storage, attr, p)
    eng = create_engine(f"sqlite:///{tmp_path/'t.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    import database
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=eng))
    s = sessionmaker(bind=eng)()
    owner = User(email="alex@x", display_name="a", password_hash="", api_key="ko", trust_level="user", device_ids=[])
    s.add(owner); s.commit()
    key = f"u{owner.id}_modell.glb"
    (up / "media" / "arkturian" / key).write_bytes(GLB)
    o = StorageObject(object_key=key, tenant_id="arkturian", owner_user_id=owner.id, is_public=True,
                      original_filename="modell.glb", mime_type="model/gltf-binary", file_size_bytes=len(GLB),
                      checksum="abc", file_url=f"/uploads/storage/media/arkturian/{key}", metadata_json={})
    s.add(o); s.commit()
    oid = o.id

    calls = []

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, params=None, headers=None, files=None):
            calls.append(SimpleNamespace(url=url, headers=dict(headers or {})))
            return _FakeResp()

    monkeypatch.setattr(R.httpx, "Client", _FakeClient)
    monkeypatch.setattr(R, "_get_threed_pipeline_version", lambda url: "test")
    monkeypatch.setenv("THREED_API_URL", "http://3d.test")

    from auth import get_current_user_optional
    from database import get_db
    from tenancy.config import get_tenant_id_optional
    app = FastAPI()
    app.include_router(R.router, prefix="/storage")
    app.dependency_overrides[get_db] = lambda: s
    app.dependency_overrides[get_current_user_optional] = lambda: None
    app.dependency_overrides[get_tenant_id_optional] = lambda: "arkturian"
    yield SimpleNamespace(c=TestClient(app), oid=oid, calls=calls)
    s.close()


def test_key_sent_as_header(env, monkeypatch):
    monkeypatch.setenv("THREED_API_KEY", "k-storage-3d")
    r = env.c.get(f"/storage/media/{env.oid}?preset=sculpture")
    assert r.status_code == 200, r.text
    assert r.content == b"glTF-optimised"
    assert len(env.calls) == 1
    assert env.calls[0].url == "http://3d.test/optimize/glb"
    assert env.calls[0].headers.get("X-API-KEY") == "k-storage-3d"


def test_no_key_no_header(env, monkeypatch):
    monkeypatch.delenv("THREED_API_KEY", raising=False)
    r = env.c.get(f"/storage/media/{env.oid}?preset=mobile")
    assert r.status_code == 200, r.text
    assert "X-API-KEY" not in env.calls[0].headers
