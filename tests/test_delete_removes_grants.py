"""Loeschen eines Objekts entfernt seine media_grants (ELE 24.10.2026)."""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import models  # noqa: E402
from models import MediaGrant, StorageObject  # noqa: E402


def _s(tmp_path):
    models._hwm_ready.clear()
    eng = create_engine(f"sqlite:///{tmp_path/'s.db'}")
    models.Base.metadata.create_all(eng, tables=[StorageObject.__table__, MediaGrant.__table__])
    return sessionmaker(bind=eng)()


def _grant(s, oid, scope):
    s.add(MediaGrant(object_id=oid, scope=scope, granted_by_user_id=1, granted_via="upload",
                     created_at=datetime.utcnow()) if hasattr(MediaGrant, "created_at")
          else MediaGrant(object_id=oid, scope=scope, granted_by_user_id=1, granted_via="upload"))


def test_grants_verschwinden_mit_dem_objekt(tmp_path):
    s = _s(tmp_path)
    a, b = StorageObject(object_key="a"), StorageObject(object_key="b")
    s.add_all([a, b]); s.commit()
    _grant(s, a.id, "cloud-session:X"); _grant(s, b.id, "cloud-session:Y"); s.commit()
    s.delete(a); s.commit()
    rest = [(g.object_id, g.scope) for g in s.query(MediaGrant).all()]
    assert rest == [(b.id, "cloud-session:Y")]
