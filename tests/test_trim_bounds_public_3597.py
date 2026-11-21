"""#3597: /media/{id}/trim-bounds answers for public objects without a key,
exactly like /media and /objects. Non-public objects stay 403 for anonymous
and foreign callers.

Before, the tenant check ran even for public objects, so every public object of
a real tenant (oneal, gsg) needed a tenant key. The productfinder sent one from
its browser bundle; once those public keys are switched off, its hero image
lost the text position."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from storage import routes as R  # noqa: E402
from auth import get_current_user_optional  # noqa: E402
from database import get_db  # noqa: E402
from tenancy.config import get_tenant_id_optional  # noqa: E402

TRIM = {"normalized": [0.1, 0.1, 0.9, 0.9], "x1": 10, "y1": 10, "x2": 90, "y2": 90,
        "width": 100, "height": 100, "applied": True}


def _obj(oid, is_public, owner=2):
    return SimpleNamespace(id=oid, tenant_id="oneal", is_public=is_public, owner_user_id=owner,
                           object_key=f"{oid}.jpg", external_uri=None, mime_type="image/jpeg",
                           ai_context_metadata={"trim_bounds": dict(TRIM)}, metadata_json={})


PUBLIC = _obj(11, True)
PRIVATE = _obj(12, False)
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

    def update(self, *a, **k):
        raise AssertionError("stored trim bounds must be read, not regenerated")


class _DB:
    def query(self, *a):
        return _Q([PUBLIC, PRIVATE])

    def commit(self):
        pass

    def rollback(self):
        pass


@pytest.fixture
def call(monkeypatch):
    monkeypatch.setattr(R, "_is_privileged_admin", lambda u, db: False)

    def do(oid, user=None, tenant=None):
        app = FastAPI()
        app.include_router(R.router, prefix="/storage")
        app.dependency_overrides[get_db] = lambda: _DB()
        app.dependency_overrides[get_tenant_id_optional] = lambda: tenant
        app.dependency_overrides[get_current_user_optional] = lambda: user
        return TestClient(app).get(f"/storage/media/{oid}/trim-bounds")
    return do


def test_public_without_key_is_200(call):
    r = call(PUBLIC.id)
    assert r.status_code == 200, r.text
    assert r.json()["normalized"] == TRIM["normalized"]


def test_public_with_foreign_tenant_key_is_200(call):
    # Same as /media: a public object is public for everyone.
    assert call(PUBLIC.id, user=STRANGER, tenant="arkturian").status_code == 200


def test_private_without_key_is_403(call):
    assert call(PRIVATE.id).status_code == 403


def test_private_foreign_tenant_is_403(call):
    assert call(PRIVATE.id, user=STRANGER, tenant="arkturian").status_code == 403


def test_private_owner_in_own_tenant_is_200(call):
    assert call(PRIVATE.id, user=OWNER, tenant="oneal").status_code == 200


def test_private_owner_with_foreign_tenant_is_403(call):
    # The tenant check still applies to non-public objects.
    assert call(PRIVATE.id, user=OWNER, tenant="arkturian").status_code == 403
