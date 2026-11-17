"""#3285: an object whose MIME type says image/* but which Pillow cannot open
(AutoCAD .dwg as image/vnd.dwg) answered 500 for every variant request from
the portal grid. Variants and format changes now answer 415
variant_unsupported; the original stays downloadable; real images are
unaffected."""
import io
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

DWG = b"AC1032" + b"\x00" * 64 + bytes(range(256)) * 40  # AutoCAD 2018 header, not an image


def _png() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (200, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def env(tmp_path, monkeypatch):
    from storage import routes as R
    from storage.service import generic_storage

    up = tmp_path / "up"
    media = up / "media" / "arkturian"
    media.mkdir(parents=True)
    for attr, sub in (("upload_root", ""), ("media_dir", "media"), ("thumbnails_dir", "thumbnails"),
                      ("webview_dir", "webview")):
        p = up / sub if sub else up
        p.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(generic_storage, attr, p)

    eng = create_engine(f"sqlite:///{tmp_path/'t.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    import database
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=eng))
    s = sessionmaker(bind=eng)()
    owner = User(email="tischler@x", display_name="t", password_hash="", api_key="kt", trust_level="user",
                 device_ids=[])
    s.add(owner); s.commit()
    objs = {}
    for name, data, mime in (("plan.dwg", DWG, "image/vnd.dwg"), ("rot.png", _png(), "image/png")):
        key = f"u{owner.id}_{name}"
        (media / key).write_bytes(data)
        o = StorageObject(object_key=key, tenant_id="arkturian", owner_user_id=owner.id, is_public=True,
                          original_filename=name, mime_type=mime, file_size_bytes=len(data),
                          file_url=f"/uploads/storage/media/arkturian/{key}", metadata_json={})
        s.add(o); s.commit()
        objs[name] = o.id

    from auth import get_current_user, get_current_user_optional
    from database import get_db
    app = FastAPI()
    app.include_router(R.router, prefix="/storage")
    app.dependency_overrides[get_db] = lambda: s
    app.dependency_overrides[get_current_user_optional] = lambda: s.get(User, owner.id)
    app.dependency_overrides[get_current_user] = lambda: s.get(User, owner.id)
    yield SimpleNamespace(c=TestClient(app, raise_server_exceptions=False), objs=objs)
    s.close()


@pytest.mark.parametrize("params", [
    {"format": "webp", "variant": "large"},
    {"variant": "thumbnail"},
    {"format": "jpg"},
    {"width": "200"},
])
def test_dwg_variant_is_415_not_500(env, params):
    r = env.c.get(f"/storage/media/{env.objs['plan.dwg']}", params=params)
    assert r.status_code == 415, (r.status_code, r.text[:300])
    assert r.json()["detail"]["code"] == "variant_unsupported"


def test_dwg_original_still_served(env):
    r = env.c.get(f"/storage/media/{env.objs['plan.dwg']}")
    assert r.status_code == 200 and r.content == DWG


def test_png_variant_unchanged(env):
    r = env.c.get(f"/storage/media/{env.objs['rot.png']}", params={"format": "webp", "variant": "large"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/webp")
