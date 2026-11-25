"""#3366: the weekly drift signal counts numbers only, per question."""
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import abuse_drift_report as D  # noqa: E402


def _db(rows):
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE storage_objects (id INTEGER, original_filename TEXT, metadata_json TEXT)")
    for i, md in enumerate(rows):
        con.execute("INSERT INTO storage_objects VALUES (?, ?, ?)", (i, f"geheim-{i}.png", json.dumps(md)))
    return con


def _w(csam=0.0, minor=0.0, sex=0.0, gew=0.0):
    return {"csam": csam, "minderjaehrig_sichtbar": minor, "sexuell_explizit": sex, "gewalt_missbrauch": gew}


NOW = datetime(2026, 11, 25, 12, tzinfo=timezone.utc)


def test_counts_stage1_and_drift_per_question_in_window():
    con = _db([
        {"abuse_check_schatten": {"am": "2026-11-24T10:00:00Z", "werte": _w()}},
        {"abuse_check_schatten": {"am": "2026-11-24T10:00:00Z", "werte": _w(csam=0.06)}},          # stage 1 csam
        {"abuse_check_schatten": {"am": "2026-11-24T10:00:00Z", "werte": _w(gew=0.31, minor=0.9)}},  # stage 1 gewalt
        {"abuse_check": {"am": "2026-11-23T10:00:00Z", "werte": _w(sex=0.9)}},                       # stage 2
        {"abuse_check_schatten": {"am": "2026-11-24T10:00:00Z", "fehler": "http 429"}},
        {"abuse_check_schatten": {"am": "2026-11-01T10:00:00Z", "werte": _w(csam=0.5)}},           # outside
    ])
    r = D.report(con, 7, now=NOW)
    assert r["pruefungen"] == 5 and r["ohne_wert"] == 1
    assert r["stufe1_je_frage"] == {"csam": 1, "gewalt_missbrauch": 1}
    assert r["stufe2_je_frage"] == {"sexuell_explizit": 1}
    assert r["ueber_0_03_je_frage"] == {"csam": 1, "minderjaehrig_sichtbar": 1,
                                        "sexuell_explizit": 1, "gewalt_missbrauch": 1}


def test_output_has_no_ids_or_names():
    con = _db([{"abuse_check_schatten": {"am": "2026-11-24T10:00:00Z", "werte": _w(csam=0.06)}}])
    text = json.dumps(D.report(con, 7, now=NOW))
    assert "geheim" not in text and '"id"' not in text
