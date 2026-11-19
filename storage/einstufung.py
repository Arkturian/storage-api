"""Visibility suggestion at upload time (#3391, Post 6250 part b).

Order, first matching rule wins:
1. The abuse check overrides everything (storage/abuse_check.py).
2. An explicit choice wins: is_public=true, private, encrypted or a
   visibility_level. Jev is not asked. (The MCP gateway always sends
   is_public=false, so false counts as "nothing chosen".)
3. Fixed rules before Jev: mail/messenger attachments, browser screenshots,
   backups, logs, exports and everything from Business stay private. A list
   of public collections does not exist yet, so that rule is absent.
4. Otherwise Jev chooses between privat and personal, from metadata only
   (file name, type, size, context, collection, uploader). personal only at
   p >= 0.8; below that, on error, 429 (daily limit) or after 2 s: privat.
5. Jev never assigns intern, öffentlich or privat verschlüsselt.

Shadow first (Cloud/Alex: two weeks): EINSTUFUNG_ENABLED=1 stores the
suggestion in metadata_json.einstufung_schatten and writes one journal line
without the file name; the visibility is never changed. There is no
enforcing mode yet; Cloud decides about it after the evaluation with AiApi.
Jev is never a permission gate.

Question and option texts are AiApi's measured wording (api-ai
tools/sichtbarkeit_bench.py, neutral variant, 60/60 in Post 6249). AiApi's
option "intern" there meant all signed-in users of one tenant, which is
today's "personal"; only the option key is renamed.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import datetime
from typing import Optional

import httpx

SCHWELLE = float(os.getenv("EINSTUFUNG_SCHWELLE", "0.8"))
TIMEOUT = float(os.getenv("EINSTUFUNG_TIMEOUT", "2"))

STUFEN = {
    "privat": ("Nur der hochladende Agent bzw. Mensch und ausdruecklich Freigegebene. Fuer alles, was "
               "Kunden-, Vertrags-, Finanz-, Gesundheits- oder Personendaten enthalten kann, Anhaenge aus "
               "Mail oder Messenger, Aufnahmen oder Fotos von Menschen, Ausweise, Zugangsdaten, Logs, "
               "Backups, Exporte, unveroeffentlichte Kundenentwuerfe, Screenshots aus dem Browser eines "
               "Menschen, und alles, dessen Zweck aus den Angaben nicht erkennbar ist."),
    "personal": ("Alle angemeldeten Agenten und Menschen des Mandanten. Nur fuer Arbeitsmaterial ohne "
                 "Personen- oder Kundendaten: KI-erzeugte Bilder, Videos, Audio, Musik, 3D-Modelle, "
                 "Diagramme, Dokumentation, Testdateien, Vorschaubilder, eigene Designs."),
}
FRAGE = ("Welche Sichtbarkeit passt fuer diese hochgeladene Datei? Beurteile nur die Angaben "
         "(Dateiname, Typ, Groesse, Kontext, Sammlung, Hochlader); der Inhalt ist nicht bekannt.")

# Rule 3: fixed private contexts. Matched as whole words in context and
# collection, lower case.
_FEST_PRIVAT = re.compile(
    r"(?<![a-z])(mail|email|e-mail|gmail|outlook|whatsapp|telegram|signal|messenger|attachment|anhang|"
    r"anhaenge|browser|backup|backups|log|logs|export|exports|business)(?![a-z])")
_FEST_PRIVAT_DATEI = re.compile(r"\.(log|bak|sql|dump)$|(?<![a-z])(backup|export)(?![a-z])")


def enabled() -> bool:
    return os.getenv("EINSTUFUNG_ENABLED", "").strip() in ("1", "true", "yes", "on")


def ausdruecklich(is_public=False, private=False, encrypted=False, visibility_level=None) -> bool:
    """Rule 2: did the uploader choose a visibility? is_public=false is the
    gateway's default and does not count."""
    return bool(is_public or private or encrypted or (visibility_level or "").strip())


def hochlader(grant_scope: Optional[str]) -> str:
    """Who uploaded, without personal data: an agent by name, otherwise only
    'mensch' or 'dienst'."""
    for s in (grant_scope or "").split(","):
        s = s.strip()
        if s.startswith("cloud-session:"):
            return "agent:" + s.split(":", 1)[1]
        if s.startswith("cloud-user:"):
            return "mensch"
    return "dienst"


def feste_regel(obj, von: str) -> Optional[str]:
    """Rule 3. Returns the rule name if a fixed rule makes the file private."""
    if von == "agent:Business":
        return "business"
    for feld in (obj.context, obj.collection_id):
        if feld and _FEST_PRIVAT.search(str(feld).lower()):
            return "kontext"
    if _FEST_PRIVAT_DATEI.search((obj.original_filename or "").lower()):
        return "dateiname"
    return None


def zustand(obj, von: str) -> str:
    """The state Jev sees, in the format of AiApi's benchmark."""
    return json.dumps({"datei": obj.original_filename, "mime": obj.mime_type,
                       "bytes": obj.file_size_bytes, "kontext": obj.context,
                       "sammlung": obj.collection_id, "von": von}, ensure_ascii=False)


class JevFehler(Exception):
    pass


async def frage_jev(obj, von: str) -> dict:
    """One Jev call under storage-api's own identity (service:storage-api,
    daily limit at api-ai). Returns {choice, p_personal, ms, kosten}; raises
    JevFehler on any failure (timeout, 429 daily limit, HTTP error, bad answer)."""
    from storage.abuse_check import CheckUnavailable, _service_token
    url = os.getenv("EINSTUFUNG_URL", "https://api-ai.arkturian.com/ai/jev").strip()
    try:
        token = await _service_token()
    except CheckUnavailable as e:
        raise JevFehler(str(e))
    body = {"state": zustand(obj, von),
            "questions": {"stufe": {"type": "choice", "instructions": FRAGE, "criteria": STUFEN}}}
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.post(url, params={"engine": "jev", "max_attempts": 1}, json=body,
                             headers={"Authorization": f"Bearer {token}"})
    except httpx.TimeoutException:
        raise JevFehler("timeout")
    except Exception as e:
        raise JevFehler(type(e).__name__)
    ms = int((time.monotonic() - t0) * 1000)
    if r.status_code == 429:
        raise JevFehler("tagesgrenze")
    if r.status_code != 200:
        raise JevFehler(f"http {r.status_code}")
    try:
        d = r.json()
        a = d["answers"]["stufe"]
        choice = a.get("choice")
        p = float((a.get("probabilities") or {}).get("personal", 0.0))
    except Exception:
        raise JevFehler("bad answer")
    if choice not in STUFEN:
        raise JevFehler("bad answer")
    return {"choice": choice, "p_personal": round(p, 4), "ms": ms,
            "kosten": float(d.get("cost_usd") or 0.0)}


def regel(antwort: Optional[dict]) -> str:
    """Rule 4: personal only if Jev chose it with p >= SCHWELLE."""
    if antwort and antwort.get("choice") == "personal" and antwort.get("p_personal", 0.0) >= SCHWELLE:
        return "personal"
    return "privat"


async def vorschlag(obj, von: str) -> dict:
    """The full suggestion for an object without an explicit choice."""
    fest = feste_regel(obj, von)
    if fest:
        return {"vorschlag": "privat", "quelle": f"regel:{fest}"}
    try:
        a = await frage_jev(obj, von)
    except JevFehler as e:
        return {"vorschlag": "privat", "quelle": "fehler", "fehler": str(e)[:40]}
    return {"vorschlag": regel(a), "quelle": "jev", "jev": a["choice"], "p_personal": a["p_personal"],
            "ms": a["ms"], "kosten": a["kosten"]}


def schatten_bald(obj, *, explizit: bool, grant_scope: Optional[str] = None) -> None:
    """Shadow mode hook for the upload paths: schedule the suggestion in the
    background, so the upload never waits for it."""
    if not enabled() or obj is None or explizit or getattr(obj, "encrypted", False):
        return
    try:
        asyncio.get_event_loop().create_task(schatten(obj.id, hochlader(grant_scope)))
    except Exception as e:
        print(f"⚠️ einstufung not scheduled object={obj.id}: {type(e).__name__}", flush=True)


async def schatten(object_id: int, von: str) -> Optional[str]:
    """Compute the suggestion and record it next to the real visibility.
    Changes nothing else. The journal line carries no file name."""
    import database
    from models import StorageObject
    db = database.SessionLocal()
    try:
        obj = db.get(StorageObject, object_id)
        if obj is None:
            return None
        v = await vorschlag(obj, von)
        tatsaechlich = ("oeffentlich" if obj.is_public else (obj.visibility_level or "privat"))
        v.update(am=datetime.utcnow().isoformat() + "Z", tatsaechlich=tatsaechlich, von=von)
        md = dict(obj.metadata_json or {}) if isinstance(obj.metadata_json, dict) else {}
        md["einstufung_schatten"] = v
        obj.metadata_json = md
        db.commit()
        teile = [f"object={object_id}", f"vorschlag={v['vorschlag']}", f"quelle={v['quelle']}",
                 f"tatsaechlich={tatsaechlich}", f"mime={obj.mime_type or '-'}"]
        if "p_personal" in v:
            teile += [f"p_personal={v['p_personal']}", f"ms={v['ms']}"]
        if v.get("fehler"):
            teile.append(f"fehler={v['fehler']}")
        print("🧭 einstufung schatten " + " ".join(teile), flush=True)
        return v["vorschlag"]
    except Exception as e:
        db.rollback()
        print(f"⚠️ einstufung failed object={object_id}: {type(e).__name__}", flush=True)
        return None
    finally:
        db.close()
