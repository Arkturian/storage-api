"""Abgelaufene Objekte werden nie ausgeliefert, auch ohne cleanup_expired-Lauf.

P3 24.10.2026: Auf den Kundeninstanzen lief kein TTL-Aufraeumen; "24-h-Kopien"
von Kontoauszuegen waren seit 30.08. anonym abrufbar.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage import routes  # noqa: E402


def _obj(expires_at, public=True):
    return SimpleNamespace(tombstoned_at=None, expires_at=expires_at, metadata_json={},
                           is_public=public, owner_user_id=1, ai_danger_potential=0,
                           ai_safety_rating="safe", ai_safety_status="completed",
                           mime_type="application/pdf")


def test_abgelaufen_ist_404_auch_fuer_den_besitzer():
    owner = SimpleNamespace(id=1, trust_level="user", api_key=None)
    for wer in (None, owner):
        with pytest.raises(HTTPException) as e:
            routes._check_quarantine(_obj(datetime.utcnow() - timedelta(minutes=1)), wer)
        assert e.value.status_code == 404 and e.value.detail["code"] == "expired"


def test_zeitzonenbewusst_abgelaufen():
    with pytest.raises(HTTPException):
        routes._check_quarantine(_obj(datetime.now(timezone.utc) - timedelta(seconds=5)), None)


def test_noch_gueltig_und_ohne_ablauf_unberuehrt():
    routes._check_quarantine(_obj(datetime.utcnow() + timedelta(hours=1)), None)
    routes._check_quarantine(_obj(None), None)
