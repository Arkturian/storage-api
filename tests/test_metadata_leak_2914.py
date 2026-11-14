"""#2914: /like, /cache-status, /media/clear-cache, focal and trim-bounds no
longer expose or touch private objects for anonymous or foreign callers."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from storage import routes as R  # noqa: E402
from auth import get_current_user, get_current_user_optional  # noqa: E402
from database import get_db  # noqa: E402
from tenancy.config import get_tenant_id, get_tenant_id_optional  # noqa: E402

PRIVATE = SimpleNamespace(id=5, tenant_id="arkturian", is_public=False, owner_user_id=2, likes=0,
                          object_key="p.jpg", external_uri=None, mime_type="image/jpeg",
                          ai_context_metadata={"secret": "x"}, metadata_json={})
PUBLIC = SimpleNamespace(id=6, tenant_id="arkturian", is_public=True, owner_user_id=2, likes=0,
                         object_key="o.jpg", external_uri=None, mime_type="image/jpeg",
                         ai_context_metadata={}, metadata_json={})
OWNER = SimpleNamespace(id=2, trust_level="user", api_key="k-owner")
STRANGER = SimpleNamespace(id=9, trust_level="user", api_key="k-other")


class _Q:
    def __init__(self, objs):
        self.objs = objs

    def filter(self, *conds):
        for c in conds:
            try:
                val = c.right.value
                if isinstance(val, int):
                    self.objs = [o for o in self.objs if o.id == val]
            except Exception:
                pass
        return self

    def first(self):
        return self.objs[0] if self.objs else None

    def all(self):
        return self.objs


class _DB:
    def query(self, *a):
        return _Q([PRIVATE, PUBLIC])

    def commit(self):
        pass

    def refresh(self, o):
        pass


@pytest.fixture
def mk(monkeypatch):
    monkeypatch.setattr(R, "_is_privileged_admin", lambda u, db: False)
    PRIVATE.likes = PUBLIC.likes = 0

    def make(user):
        app = FastAPI()
        app.include_router(R.router, prefix="/storage")
        app.dependency_overrides[get_db] = lambda: _DB()
        app.dependency_overrides[get_tenant_id] = lambda: "arkturian"
        app.dependency_overrides[get_tenant_id_optional] = lambda: "arkturian"
        app.dependency_overrides[get_current_user_optional] = lambda: user

        def req():
            if user is None:
                raise HTTPException(status_code=401, detail="Invalid API key")
            return user
        app.dependency_overrides[get_current_user] = req
        return TestClient(app)
    return make


def test_like_private_anonymous_is_404_and_not_counted(mk):
    r = mk(None).post("/storage/objects/5/like")
    assert r.status_code == 404
    assert PRIVATE.likes == 0


def test_like_public_returns_only_counter(mk):
    r = mk(None).post("/storage/objects/6/like")
    assert r.status_code == 200
    assert r.json() == {"id": 6, "likes": 1}


def test_like_private_owner_ok(mk):
    assert mk(OWNER).post("/storage/objects/5/like").json() == {"id": 5, "likes": 1}


def test_cache_status_requires_key(mk):
    assert mk(None).get("/storage/cache-status", params={"object_id": 5}).status_code == 401


def test_cache_status_hides_foreign_private_and_paths(mk):
    r = mk(STRANGER).get("/storage/cache-status", params=[("object_id", 5), ("object_id", 6)])
    assert r.status_code == 200
    rows = {row["object_id"]: row for row in r.json()}
    assert rows[5]["message"] == "Object not found" and rows[5]["original_path"] is None
    assert rows[6]["original_path"] is None  # public object, but no server paths for non-operators
    assert all(d["path"] is None for d in rows[6]["derivatives"])


def test_clear_cache_requires_key_and_ownership(mk):
    assert mk(None).post("/storage/media/clear-cache", params={"object_id": 6}).status_code == 401
    r = mk(STRANGER).post("/storage/media/clear-cache", params={"object_id": 6})
    assert r.status_code == 200 and r.json()["results"][0]["success"] is False
    r = mk(OWNER).post("/storage/media/clear-cache", params={"object_id": 6})
    assert r.json()["results"][0]["success"] is True


def test_focal_check_denies_private(monkeypatch):
    monkeypatch.setattr(R, "_is_privileged_admin", lambda u, db: False)
    with pytest.raises(HTTPException) as e:
        R._focal_access_check(5, _DB(), None)
    assert e.value.status_code == 403


def test_batch_trim_bounds_no_500_and_skips_private(mk):
    r = mk(STRANGER).get("/storage/batch/trim-bounds", params=[("id", 5), ("id", 6)])
    assert r.status_code == 200, r.text
    assert "5" not in r.json()
