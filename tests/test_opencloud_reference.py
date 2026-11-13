"""OpenCloud-Ordner als Referenz-Objekte (#2810, Konzept Post 5939).

Pflichtregel 8: Storage liest nur, ein fehlende Datei löscht nie ein Objekt,
ein Objekt findet seine verschobene Datei über die OpenCloud-Datei-ID wieder.
Die Datei-IDs stehen wie bei OpenCloud (posix-Treiber) als xattr am Inode.
"""
import asyncio
import os
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import models  # noqa: E402
from models import StorageObject  # noqa: E402
from storage import opencloud as oc  # noqa: E402


def _xattrs_supported(d: Path) -> bool:
    probe = d / ".xattr-probe"
    probe.write_text("x")
    try:
        os.setxattr(str(probe), "user.test", b"1")
        return True
    except OSError:
        return False
    finally:
        probe.unlink()


@pytest.fixture
def env(tmp_path, monkeypatch):
    if not _xattrs_supported(tmp_path):
        pytest.skip("Dateisystem ohne user-xattr")
    projects = tmp_path / "projects"
    space_id = str(uuid.uuid4())
    space = projects / space_id
    (space / "docs").mkdir(parents=True)
    (space / ".oc-nodes").mkdir()
    (space / ".Trash").mkdir()
    os.setxattr(str(space), oc.XATTR_SPACE_ID, space_id.encode())
    os.setxattr(str(space), oc.XATTR_ID, space_id.encode())
    (tmp_path / "users" / "privat").mkdir(parents=True)
    monkeypatch.setenv("OPENCLOUD_PROJECTS_ROOT", str(projects))

    models._hwm_ready.clear()
    eng = create_engine(f"sqlite:///{tmp_path / 's.db'}")
    models.Base.metadata.create_all(eng, tables=[StorageObject.__table__])
    db = sessionmaker(bind=eng)()

    enqueued = []

    async def fake_enqueue(obj, db=None, ai_mode="full", **kw):
        enqueued.append((obj.id, ai_mode))

    import storage.service as svc
    monkeypatch.setattr(svc, "enqueue_ai_safety_and_transcoding", fake_enqueue)
    return {"tmp": tmp_path, "projects": projects, "space": space, "space_id": space_id,
            "db": db, "enqueued": enqueued}


def _file(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    os.setxattr(str(path), oc.XATTR_ID, str(uuid.uuid4()).encode())
    return path


def _binding(env, **kw):
    raw = {"name": "pilot", "path": str(env["space"] / "docs"), "collection_id": "opencloud:pilot",
           "tenant_id": "arkturian", "owner_user_id": 2}
    raw.update(kw)
    return oc.parse_binding(raw)


def _sync(env, binding):
    return asyncio.run(oc.sync_binding(binding, env["db"]))


def test_binding_nur_innerhalb_eines_space(env):
    with pytest.raises(oc.BindingError):
        oc.parse_binding({"name": "x", "path": str(env["tmp"] / "users" / "privat"),
                          "collection_id": "c", "owner_user_id": 2})
    with pytest.raises(oc.BindingError):
        oc.parse_binding({"name": "x", "path": str(env["projects"]), "collection_id": "c", "owner_user_id": 2})
    b = _binding(env)
    assert b.space_id == env["space_id"]


def test_sensibler_ordner_laeuft_ohne_ki(env):
    b = _binding(env, sensitive=True, ai_mode="full")
    assert b.effective_ai_mode == "none"


def test_sync_legt_private_referenzen_an_und_liest_nur(env):
    a = _file(env["space"] / "docs" / "konzept.md", "# Konzept\n")
    _file(env["space"] / "docs" / "unter" / "notiz.txt", "hallo")
    (env["space"] / "docs" / ".~lock.konzept.md#").write_text("lock")  # versteckt, wird übersprungen
    before = (a.read_bytes(), a.stat().st_mtime_ns)

    rep = _sync(env, _binding(env))
    assert len(rep.created) == 2 and not rep.errors
    objs = env["db"].query(StorageObject).order_by(StorageObject.id).all()
    for o in objs:
        assert o.storage_mode == "reference"
        assert o.opencloud_space_id == env["space_id"]
        assert o.reference_state == oc.STATE_OK
        assert o.collection_id == "opencloud:pilot"
        assert o.is_public is False and o.metadata_json.get("private_media") is True
    assert env["enqueued"] == []  # ai_mode none ist die Vorgabe
    assert (a.read_bytes(), a.stat().st_mtime_ns) == before

    rep2 = _sync(env, _binding(env))
    assert rep2.created == [] and rep2.changed == [] and rep2.missing == []


def test_aenderung_in_opencloud_wird_erkannt(env):
    from storage.service import generic_storage

    a = _file(env["space"] / "docs" / "konzept.md", "v1")
    _sync(env, _binding(env))
    o = env["db"].query(StorageObject).one()
    old_sum = o.checksum
    tenant_dir = generic_storage.webview_dir / "arkturian"
    tenant_dir.mkdir(parents=True, exist_ok=True)
    stale = tenant_dir / f"web_{Path(o.object_key).stem}_200e_q80.jpg"
    stale.write_bytes(b"alt")

    a.write_text("v2, länger")
    rep = _sync(env, _binding(env))
    env["db"].refresh(o)
    assert rep.changed == [o.id]
    assert o.checksum != old_sum and o.file_size_bytes == a.stat().st_size
    assert not stale.exists()


def test_verschieben_und_umbenennen_behalten_das_objekt(env):
    a = _file(env["space"] / "docs" / "konzept.md", "text")
    _sync(env, _binding(env))
    o = env["db"].query(StorageObject).one()
    target = env["space"] / "docs" / "archiv" / "konzept-alt.md"
    target.parent.mkdir()
    a.rename(target)  # rename behält den Inode und damit die xattr

    rep = _sync(env, _binding(env))
    env["db"].refresh(o)
    assert rep.created == [] and rep.moved == [o.id]
    assert o.reference_path == str(target) and o.original_filename == "konzept-alt.md"


def test_geloeschte_datei_loescht_nie_das_objekt_und_kommt_zurueck(env):
    a = _file(env["space"] / "docs" / "konzept.md", "text")
    _sync(env, _binding(env))
    o = env["db"].query(StorageObject).one()
    trash = env["space"] / ".Trash" / "konzept.md"
    a.rename(trash)  # in den Papierkorb: ausserhalb des sichtbaren Baums

    rep = _sync(env, _binding(env))
    env["db"].refresh(o)
    assert rep.missing == [o.id]
    assert env["db"].query(StorageObject).count() == 1
    assert o.reference_state == oc.STATE_MISSING
    # Media-Route: kein Durchsuchen des Space bei bereits fehlender Datei
    assert oc.refresh_reference(o, env["db"], search_missing=False).missing

    trash.rename(a)  # wiederhergestellt
    rep = _sync(env, _binding(env))
    env["db"].refresh(o)
    assert rep.restored == [o.id] and rep.created == []
    assert o.reference_state == oc.STATE_OK


def test_fremde_objekte_bleiben_unberuehrt(env):
    db = env["db"]
    legacy = StorageObject(object_key="x", storage_mode="reference", reference_path="/nicht/da")
    db.add(legacy); db.commit()
    res = oc.refresh_reference(legacy, db)
    assert not res.missing and legacy.reference_state is None
