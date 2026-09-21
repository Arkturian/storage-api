"""#1943: Eine geloeschte media-ID wird nie an ein anderes Objekt vergeben.

Nachgestellt mit dem Schema, das pdrei und der zentrale Speicher tatsaechlich
haben (`id INTEGER NOT NULL PRIMARY KEY`, ohne AUTOINCREMENT) — nicht mit dem,
das create_all heute fuer eine frische Datenbank anlegen wuerde. Sonst wuerde
der Test die Luecke gar nicht sehen.
"""
import os
import sqlite3
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import models  # noqa: E402
from models import StorageObject  # noqa: E402

from sqlalchemy.dialects import sqlite as _sqlite  # noqa: E402
from sqlalchemy.schema import CreateTable  # noqa: E402

# Das Schema der Live-Datenbanken: alle Spalten des Modells, aber OHNE
# AUTOINCREMENT (so steht es auf pdrei und arkserver, per sqlite_master gelesen).
ALTES_SCHEMA = str(CreateTable(StorageObject.__table__).compile(dialect=_sqlite.dialect())).replace(
    " AUTOINCREMENT", "")
assert "AUTOINCREMENT" not in ALTES_SCHEMA.upper()


def _engine(pfad):
    con = sqlite3.connect(pfad)
    con.execute(ALTES_SCHEMA)
    con.commit()
    con.close()
    eng = create_engine(f"sqlite:///{pfad}", connect_args={"check_same_thread": False, "timeout": 10})

    @event.listens_for(eng, "connect")
    def _p(dbapi, rec):
        dbapi.execute("PRAGMA journal_mode=WAL")
        dbapi.execute("PRAGMA busy_timeout=10000")
    return eng


def _neu(sess, key):
    o = StorageObject(object_key=key, original_filename=key)
    sess.add(o)
    sess.commit()
    return o.id


def _roh_ids(pfad):
    con = sqlite3.connect(pfad)
    try:
        return [r[0] for r in con.execute("select id from storage_objects order by id")]
    finally:
        con.close()


@pytest.fixture
def db(tmp_path):
    models._hwm_ready.clear()
    return tmp_path / "storage.db"


def test_geloeschte_hoechste_nummer_wird_nicht_neu_vergeben(db):
    """Genau der Ablauf vom 18.09.: die hoechsten vier loeschen, dann hochladen."""
    Session = sessionmaker(bind=_engine(db))
    s = Session()
    ids = [_neu(s, f"ausweis-{i}") for i in range(4)]
    for oid in ids:
        s.delete(s.get(StorageObject, oid))
    s.commit()
    neu = [_neu(s, f"zahlungsliste-{i}") for i in range(2)]
    assert not set(neu) & set(ids), f"Nummern {set(neu) & set(ids)} wurden wiederverwendet"
    assert neu == [ids[-1] + 1, ids[-1] + 2]


def test_gegenprobe_ohne_waechter_vergibt_neu(db):
    """Die Probe muss die Luecke sehen koennen: ohne den Waechter vergibt
    dasselbe Schema die geloeschte Nummer tatsaechlich neu."""
    event.remove(StorageObject, "before_insert", models._assign_never_reused_id)
    try:
        Session = sessionmaker(bind=_engine(db))
        s = Session()
        erste = _neu(s, "a")
        s.delete(s.get(StorageObject, erste))
        s.commit()
        assert _neu(s, "b") == erste
    finally:
        event.listen(StorageObject, "before_insert", models._assign_never_reused_id)


def test_marke_startet_bei_bestehendem_maximum(db):
    """Bestehende Datenbank mit Daten, Waechter kommt spaeter dazu."""
    con = sqlite3.connect(db)
    con.execute(ALTES_SCHEMA)
    con.executemany("insert into storage_objects (id, object_key, tenant_id) values (?, ?, 'arkturian')",
                    [(1549, "x"), (1558, "y")])
    con.commit()
    con.close()
    eng = create_engine(f"sqlite:///{db}")
    s = sessionmaker(bind=eng)()
    assert _neu(s, "neu") == 1559


def test_parallele_uploads_bekommen_verschiedene_nummern(db):
    eng = _engine(db)
    Session = sessionmaker(bind=eng)
    vergeben, fehler = [], []

    def lade(n):
        s = Session()
        try:
            for i in range(10):
                vergeben.append(_neu(s, f"t{n}-{i}"))
        except Exception as e:  # noqa: BLE001
            fehler.append(repr(e))
        finally:
            s.close()

    ts = [threading.Thread(target=lade, args=(n,)) for n in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not fehler, fehler
    assert len(vergeben) == 40 and len(set(vergeben)) == 40
    assert _roh_ids(db) == sorted(vergeben)


def test_explizite_id_bleibt_unangetastet(db):
    """Importskripte, die eine Nummer bewusst setzen (merge_from_vps), werden nicht umgebogen."""
    s = sessionmaker(bind=_engine(db))()
    o = StorageObject(id=4711, object_key="fest", original_filename="fest")
    s.add(o)
    s.commit()
    assert o.id == 4711
