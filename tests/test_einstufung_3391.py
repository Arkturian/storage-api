"""#3391 (Post 6250 b): Jev suggests privat or personal at upload, shadow only.
Never intern/öffentlich, personal only at p >= 0.8, everything else privat,
explicit choice and fixed rules skip Jev, the visibility never changes."""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from models import Base, StorageObject, User  # noqa: E402
from storage import einstufung as E  # noqa: E402


def _obj(**kw):
    d = dict(original_filename="flora_render_v3.png", mime_type="image/png", file_size_bytes=120000,
             context="genimage", collection_id=None, is_public=False, visibility_level=None, encrypted=False)
    d.update(kw)
    return SimpleNamespace(**d)


class _Resp:
    def __init__(self, code, body=None):
        self.status_code = code
        self._b = body or {}

    def json(self):
        return self._b


def _jev_answer(choice, p_personal):
    return {"answers": {"stufe": {"choice": choice, "confidence": 0.9,
                                  "probabilities": {"personal": p_personal, "privat": 1 - p_personal}}},
            "cost_usd": 0.000025, "latency_ms": 330}


@pytest.fixture
def jev(monkeypatch):
    """Fake api-ai: set .verhalten to a response, an exception class, or a
    callable; .bodies records what was sent."""
    st = SimpleNamespace(verhalten=_Resp(200, _jev_answer("personal", 0.93)), bodies=[], params=[])

    async def _tok():
        return "tok"
    monkeypatch.setattr("storage.abuse_check._service_token", _tok)

    class _C:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, params=None, json=None, headers=None):
            st.bodies.append(json)
            st.params.append(params)
            v = st.verhalten
            if isinstance(v, type) and issubclass(v, BaseException):
                raise v("x")
            return v
    monkeypatch.setattr(E.httpx, "AsyncClient", _C)
    return st


def run(c):
    return asyncio.run(c)


def test_personal_only_at_threshold(jev):
    v = run(E.vorschlag(_obj(), "agent:Wave"))
    assert v["vorschlag"] == "personal" and v["quelle"] == "jev" and v["p_personal"] == 0.93
    assert jev.params[0] == {"engine": "jev", "max_attempts": 1}
    q = jev.bodies[0]["questions"]["stufe"]
    assert q["type"] == "choice" and set(q["criteria"]) == {"privat", "personal"}
    assert json.loads(jev.bodies[0]["state"]) == {"datei": "flora_render_v3.png", "mime": "image/png",
                                                  "bytes": 120000, "kontext": "genimage", "sammlung": None,
                                                  "von": "agent:Wave"}


def test_low_confidence_is_privat(jev):
    jev.verhalten = _Resp(200, _jev_answer("personal", 0.79))
    v = run(E.vorschlag(_obj(), "agent:Wave"))
    assert v["vorschlag"] == "privat" and v["jev"] == "personal" and v["p_personal"] == 0.79


def test_jev_says_privat(jev):
    jev.verhalten = _Resp(200, _jev_answer("privat", 0.05))
    assert run(E.vorschlag(_obj(), "agent:Wave"))["vorschlag"] == "privat"


@pytest.mark.parametrize("verhalten,fehler", [
    (httpx.ReadTimeout, "timeout"),
    (httpx.ConnectError, "ConnectError"),
    (_Resp(429, {"detail": {"error": "jev_budget", "resets_at": "2026-11-20T00:00:00+01:00"}}), "tagesgrenze"),
    (_Resp(502, {"detail": {"error": "jev_upstream_unreachable"}}), "http 502"),
    (_Resp(401, {}), "http 401"),
    (_Resp(200, {"answers": {}}), "bad answer"),
    (_Resp(200, _jev_answer("intern", 0.99)), "bad answer"),
    (_Resp(200, _jev_answer("oeffentlich", 0.99)), "bad answer"),
])
def test_sick_cases_fall_back_to_privat(jev, verhalten, fehler):
    jev.verhalten = verhalten
    v = run(E.vorschlag(_obj(), "agent:Wave"))
    assert v["vorschlag"] == "privat" and v["quelle"] == "fehler" and v["fehler"] == fehler


def test_no_identity_is_privat(monkeypatch, jev):
    from storage.abuse_check import CheckUnavailable

    async def _kein():
        raise CheckUnavailable("no service identity")
    monkeypatch.setattr("storage.abuse_check._service_token", _kein)
    v = run(E.vorschlag(_obj(), "agent:Wave"))
    assert v["vorschlag"] == "privat" and v["fehler"] == "no service identity" and not jev.bodies


@pytest.mark.parametrize("obj,von,regel", [
    (_obj(), "agent:Business", "business"),
    (_obj(context="gmail-attachment"), "agent:Comm", "kontext"),
    (_obj(context="whatsapp"), "agent:Comm", "kontext"),
    (_obj(context="browser_screenshot"), "mensch", "kontext"),
    (_obj(collection_id="backups"), "agent:Cloud", "kontext"),
    (_obj(original_filename="api.log", mime_type="text/plain"), "agent:Cloud", "dateiname"),
    (_obj(original_filename="db-export-2026.json"), "agent:Cloud", "dateiname"),
])
def test_fixed_rules_skip_jev(jev, obj, von, regel):
    v = run(E.vorschlag(obj, von))
    assert v == {"vorschlag": "privat", "quelle": f"regel:{regel}"}
    assert not jev.bodies


def test_fixed_rule_words_are_whole_words(jev):
    # "catalog" contains "log", "mailbox-art" is not a mail attachment rule hit by substring only
    assert E.feste_regel(_obj(context="catalog", original_filename="katalog.png"), "agent:Wave") is None


def test_explicit_choice():
    assert not E.ausdruecklich(False, False, False, None)  # gateway default
    assert E.ausdruecklich(True)
    assert E.ausdruecklich(private=True)
    assert E.ausdruecklich(encrypted=True)
    assert E.ausdruecklich(visibility_level="personal")


def test_hochlader_without_personal_data():
    assert E.hochlader("cloud-session:Wave") == "agent:Wave"
    assert E.hochlader("cloud-user:42") == "mensch"
    assert E.hochlader(None) == "dienst"


def test_schatten_bald_skips(monkeypatch):
    called = []
    monkeypatch.setattr(E, "schatten", lambda *a: called.append(a))
    monkeypatch.delenv("EINSTUFUNG_ENABLED", raising=False)
    E.schatten_bald(SimpleNamespace(id=1, encrypted=False), explizit=False)
    monkeypatch.setenv("EINSTUFUNG_ENABLED", "1")
    E.schatten_bald(SimpleNamespace(id=1, encrypted=False), explizit=True)
    E.schatten_bald(SimpleNamespace(id=1, encrypted=True), explizit=False)
    assert called == []


@pytest.fixture
def db(tmp_path, monkeypatch):
    eng = create_engine(f"sqlite:///{tmp_path/'t.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    import database
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=eng))
    s = sessionmaker(bind=eng)()
    u = User(email="a@x", display_name="a", password_hash="", api_key="k", trust_level="user", device_ids=[])
    s.add(u); s.commit()
    o = StorageObject(object_key="u_k.png", tenant_id="arkturian", owner_user_id=u.id, is_public=False,
                      original_filename="Rechnung_Kunde_Hofer.png", mime_type="image/png", file_size_bytes=10,
                      context="genimage", metadata_json={"x": 1})
    s.add(o); s.commit()
    yield SimpleNamespace(s=s, oid=o.id)
    s.close()


def test_shadow_records_and_changes_nothing(db, jev, capsys):
    assert run(E.schatten(db.oid, "agent:Wave")) == "personal"
    db.s.expire_all()
    o = db.s.get(StorageObject, db.oid)
    assert o.is_public is False and o.visibility_level is None
    rec = o.metadata_json["einstufung_schatten"]
    assert rec["vorschlag"] == "personal" and rec["tatsaechlich"] == "privat" and rec["von"] == "agent:Wave"
    assert o.metadata_json["x"] == 1
    out = capsys.readouterr().out
    assert "einstufung schatten" in out and "vorschlag=personal" in out
    assert "Rechnung" not in out and "Hofer" not in out  # no file name in the journal


def test_shadow_on_daily_limit(db, jev, capsys):
    jev.verhalten = _Resp(429, {"detail": {"error": "jev_budget"}})
    assert run(E.schatten(db.oid, "mensch")) == "privat"
    db.s.expire_all()
    rec = db.s.get(StorageObject, db.oid).metadata_json["einstufung_schatten"]
    assert rec["fehler"] == "tagesgrenze" and rec["vorschlag"] == "privat"
    assert "Hofer" not in capsys.readouterr().out
