"""#2915: HLS results, link_id cascades, tasks, embedding text and the safety
verdict stay inside the owner's (or an operator's) reach."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import storage.service as S  # noqa: E402
from models import Base, StorageObject, User  # noqa: E402

SRC = (Path(__file__).resolve().parent.parent / "storage" / "routes.py").read_text()


@pytest.fixture
def db(tmp_path, monkeypatch):
    eng = create_engine(f"sqlite:///{tmp_path/'t.db'}")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    alice = User(email="alice@x", display_name="a", password_hash="", api_key="ka", trust_level="admin", device_ids=[])
    bob = User(email="bob@x", display_name="b", password_hash="", api_key="kb", trust_level="user", device_ids=[])
    s.add_all([alice, bob]); s.commit()
    objs = [
        StorageObject(object_key="a1.jpg", original_filename="report.jpg", owner_user_id=alice.id, tenant_id="t1", link_id="L"),
        StorageObject(object_key="a2.jpg", original_filename="other.jpg", owner_user_id=alice.id, tenant_id="t1", link_id="L"),
        StorageObject(object_key="b1.jpg", original_filename="report.jpg", owner_user_id=bob.id, tenant_id="t2", link_id="L"),
        StorageObject(object_key="b2.jpg", original_filename="keep.jpg", owner_user_id=bob.id, tenant_id="t2", link_id="L"),
    ]
    s.add_all(objs); s.commit()
    monkeypatch.setattr(S.generic_storage, "delete", lambda key, tenant: None)
    # alice is a tenant-key user with trust_level=admin (the #2911/#2915 shape)
    import auth
    monkeypatch.setattr(auth, "is_privileged_admin", lambda u, d: False)
    yield s, alice, bob
    s.close()


def _left(s):
    return sorted(o.object_key for o in s.query(StorageObject).all())


def test_bulk_delete_only_own_objects_and_own_links(db):
    s, alice, bob = db
    n = S.bulk_delete_objects(s, name="report", current_user=alice)
    # alice's report + her linked a2; bob's report and his linked b2 survive
    assert n == 2
    assert _left(s) == ["b1.jpg", "b2.jpg"]


def test_bulk_delete_requires_a_filter(db):
    s, alice, _ = db
    with pytest.raises(ValueError):
        S.bulk_delete_objects(s, current_user=alice)
    assert len(_left(s)) == 4


def test_operator_bulk_delete_links_stay_per_owner_tenant(db, monkeypatch):
    s, alice, bob = db
    import auth
    monkeypatch.setattr(auth, "is_privileged_admin", lambda u, d: True)
    S.bulk_delete_objects(s, name="other", current_user=alice)
    # a2's link "L" pulls in alice's a1 (same owner and tenant) but never
    # bob's objects that happen to carry the same link_id
    assert _left(s) == ["b1.jpg", "b2.jpg"]


def test_route_guards_present():
    # hls_result attach gated on owner/operator
    assert "an HLS result may only be attached by the video's owner" in SRC
    # linked children cascade restricted
    assert "StorageObject.owner_user_id == obj.owner_user_id,\n        StorageObject.tenant_id == obj.tenant_id," in SRC
    # safety fields read-only for non-operators
    assert "safety_fields_read_only" in SRC
    # tasks filtered
    assert "only tasks of the caller's own objects" in SRC
    assert "non-operators see only tasks of their own objects" in SRC
