"""#3031 (Befund 106): ?refresh only for owner or a real operator; media_grants
go with their object, also on bulk deletes."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from models import Base, MediaGrant, StorageObject, User, delete_grants_for  # noqa: E402

SRC = (Path(__file__).resolve().parent.parent / "storage" / "routes.py").read_text()


@pytest.fixture
def db(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path/'t.db'}")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    u = User(email="o@x", display_name="o", password_hash="", api_key="k", trust_level="user", device_ids=[])
    s.add(u); s.commit()
    objs = [StorageObject(object_key=f"{i}.jpg", original_filename=f"{i}.jpg", owner_user_id=u.id, tenant_id="t1",
                          file_size_bytes=1, mime_type="image/jpeg") for i in range(3)]
    s.add_all(objs); s.commit()
    for o in objs:
        s.add(MediaGrant(object_id=o.id, scope="content-post:1", granted_by_user_id=u.id))
    s.commit()
    yield s, objs
    s.close()


def test_orm_delete_drops_grants(db):
    s, objs = db
    s.delete(objs[0]); s.commit()
    assert s.query(MediaGrant).filter(MediaGrant.object_id == objs[0].id).count() == 0
    assert s.query(MediaGrant).count() == 2


def test_bulk_delete_helper_drops_grants(db):
    s, objs = db
    ids = [objs[1].id, objs[2].id]
    delete_grants_for(s, ids)
    s.query(StorageObject).filter(StorageObject.id.in_(ids)).delete(synchronize_session=False)
    s.commit()
    assert s.query(MediaGrant).filter(MediaGrant.object_id.in_(ids)).count() == 0


def test_bulk_paths_call_helper():
    root = Path(__file__).resolve().parent.parent
    assert "delete_grants_for(db, object_ids_to_delete)" in (root / "storage" / "service.py").read_text()
    assert "delete_grants_for(db, object_ids)" in (root / "tenancy" / "routes.py").read_text()


def test_refresh_gated():
    assert "if not _may_refresh:\n            refresh = False" in SRC
    assert "not _signed and current_user is not None and not _is_public_default_master(current_user)" in SRC
