#!/usr/bin/env python3
"""Weekly drift signal of the abuse check (#3366, contract 6255 v5, Jev).

Numbers only, never ids, names or content: per question the stage-1 events and
the values above 0.03, over the checks of the last N days (shadow and enforcing
records). A sudden rise points at a change in the model or the load without
anyone looking at content.

    python scripts/abuse_drift_report.py [--db /var/lib/storage-api/storage.db] [--days 7]
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import contextlib  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
with contextlib.redirect_stdout(sys.stderr):  # package start-up chatter stays off the JSON
    from storage.abuse_check import QUESTIONS, T, _verdict  # noqa: E402

DRIFT_ABOVE = 0.03


def report(con, days: int, now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    out = {"tage": days, "seit": since + "Z", "pruefungen": 0, "ohne_wert": 0,
           "stufe1_je_frage": {}, "stufe2_je_frage": {}, "ueber_0_03_je_frage": {q: 0 for q in QUESTIONS},
           "schwellen": dict(T)}
    for (raw_s, raw_e) in con.execute(
            "SELECT json_extract(metadata_json,'$.abuse_check_schatten'), json_extract(metadata_json,'$.abuse_check') "
            "FROM storage_objects WHERE json_extract(metadata_json,'$.abuse_check_schatten') IS NOT NULL "
            "OR json_extract(metadata_json,'$.abuse_check') IS NOT NULL"):
        for raw in (raw_s, raw_e):
            if not raw:
                continue
            rec = json.loads(raw)
            am = str(rec.get("am") or rec.get("zuletzt") or "")
            if am[:19] < since:
                continue
            werte = rec.get("werte")
            out["pruefungen"] += 1
            if not werte:
                out["ohne_wert"] += 1
                continue
            for q in QUESTIONS:
                if float(werte.get(q, 0.0)) > DRIFT_ABOVE:
                    out["ueber_0_03_je_frage"][q] += 1
            stufe, kategorie, _ = _verdict({q: float(werte.get(q, 0.0)) for q in QUESTIONS})
            key = {"held": "stufe1_je_frage", "hit": "stufe2_je_frage"}.get(stufe)
            if key:
                out[key][kategorie] = out[key].get(kategorie, 0) + 1
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/var/lib/storage-api/storage.db")
    ap.add_argument("--days", type=int, default=7)
    a = ap.parse_args()
    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    print(json.dumps(report(con, a.days), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
