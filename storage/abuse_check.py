"""Abuse check before a file goes public or outside (#3329, Alex 18.11.).

Decided (Post 6250, part c):
- Only images and videos that are to become public or leave through a
  share link are checked. Private, personal and intern files are never sent
  (OpenAI's zero data retention is unverified).
- Fail-closed: without a clean verdict the file stays private. A wish to go
  public is parked in `public_pending` and fulfilled only after "clear".
- A hit makes the file "privat, fest" (private_media), drops its level,
  revokes its share links and blocks delivery for everyone but an admin.
- A suspected depiction of child sexual abuse additionally raises the bus
  event storage.abuse_suspected; cloud-api turns it into a card for Alex,
  who decides about a report. No automatic report, no agent decides.

The check itself runs through api-ai (OpenAI Decisions with the image). Off
unless ABUSE_CHECK_ENABLED=1, so nothing changes until AiApi's endpoint is
live. Status per object in storage_objects.abuse_status:
    NULL     never required (all objects before this change)
    pending  check requested, no verdict yet
    failed   check could not run (error, refusal, timeout); retried
    clear    checked, nothing found
    hit      checked, abuse suspected; final unless an admin lifts it
"""
from __future__ import annotations

import asyncio
import os
import secrets
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

import httpx

QUESTIONS = ("csam", "sexuell_explizit", "gewalt_missbrauch")
# Thresholds per question (probability of "yes"); AiApi/Jev may tune them.
THRESHOLDS = {"csam": float(os.getenv("ABUSE_T_CSAM", "0.5")),
              "sexuell_explizit": float(os.getenv("ABUSE_T_SEX", "0.8")),
              "gewalt_missbrauch": float(os.getenv("ABUSE_T_GEWALT", "0.8"))}
SYNC_TIMEOUT = float(os.getenv("ABUSE_CHECK_SYNC_TIMEOUT", "8"))
MAX_ATTEMPTS = int(os.getenv("ABUSE_CHECK_MAX_ATTEMPTS", "48"))
RETRY_EVERY = int(os.getenv("ABUSE_CHECK_RETRY_SECONDS", "300"))


class CheckUnavailable(Exception):
    """No verdict: error, refusal, timeout or not configured. Fail-closed."""


def enabled() -> bool:
    return os.getenv("ABUSE_CHECK_ENABLED", "").strip().lower() in ("1", "true", "yes")


def needs_check(obj) -> bool:
    mime = (getattr(obj, "mime_type", None) or "").lower()
    return enabled() and (mime.startswith("image/") or mime.startswith("video/")) \
        and not getattr(obj, "encrypted", False)


def _image_url(obj) -> str:
    """Short signed JPEG of the image (or a video frame) for api-ai to fetch."""
    from storage import signing
    base = os.getenv("STORAGE_PUBLIC_BASE_URL", "https://api-storage.arkturian.com").rstrip("/")
    s = signing.sign(obj.id, 300)
    return (f"{base}/storage/media/{obj.id}?width=768&format=jpg"
            f"&exp={s['exp']}&kid={s['kid']}&sig={s['sig']}")


_token_cache = {"token": None, "until": 0.0}


async def _service_token() -> str:
    """storage-api's own identity at auth-api (service:storage-api, rights
    ai:jev and ai:decisions), client_credentials, cached until shortly before
    expiry. Credentials from /etc/storage-api/ai-client.env (EnvironmentFile)."""
    import time
    if _token_cache["token"] and time.time() < _token_cache["until"]:
        return _token_cache["token"]
    cid = os.getenv("STORAGE_API_SERVICE_CLIENT_ID", "").strip()
    sec = os.getenv("STORAGE_API_SERVICE_CLIENT_SECRET", "").strip()
    url = os.getenv("STORAGE_API_SERVICE_TOKEN_URL", "").strip()
    if not (cid and sec and url):
        raise CheckUnavailable("no service identity")
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.post(url, data={"grant_type": "client_credentials", "client_id": cid, "client_secret": sec})
        j = r.json()
        tok = j["access_token"]
        _token_cache.update(token=tok, until=time.time() + max(60, int(j.get("expires_in", 3600)) - 120))
        return tok
    except Exception as e:
        raise CheckUnavailable(f"token {type(e).__name__}")


async def _ask(obj) -> dict:
    """Probabilities per question from api-ai. Contract with AiApi pending;
    this is the one place that changes when it is final."""
    url = os.getenv("ABUSE_CHECK_URL", "").strip()
    if not url:
        raise CheckUnavailable("not configured")
    token = await _service_token()
    body = {"image_url": _image_url(obj), "questions": list(QUESTIONS),
            "object_id": obj.id, "tenant_id": obj.tenant_id}
    try:
        async with httpx.AsyncClient(timeout=SYNC_TIMEOUT) as c:
            r = await c.post(url, json=body, headers={"Authorization": f"Bearer {token}"})
    except Exception as e:
        raise CheckUnavailable(type(e).__name__)
    if r.status_code != 200:
        raise CheckUnavailable(f"http {r.status_code}")
    try:
        data = r.json()
        probs = {q: float(data["answers"][q]) for q in QUESTIONS}
    except Exception:
        raise CheckUnavailable("bad answer")  # includes a refusal without probabilities
    return probs


def _verdict(probs: dict) -> Tuple[str, Optional[str], float]:
    """("hit", kategorie, p) for the worst crossed threshold, csam first."""
    for q in QUESTIONS:
        if probs.get(q, 0.0) >= THRESHOLDS[q]:
            return "hit", q, probs[q]
    worst = max(QUESTIONS, key=lambda q: probs.get(q, 0.0))
    return "clear", None, probs.get(worst, 0.0)


def _record(obj, **info) -> None:
    md = dict(obj.metadata_json or {}) if isinstance(obj.metadata_json, dict) else {}
    rec = dict(md.get("abuse_check") or {})
    rec.update(info)
    md["abuse_check"] = rec
    obj.metadata_json = md


def _attempts(obj) -> int:
    md = obj.metadata_json if isinstance(obj.metadata_json, dict) else {}
    return int((md.get("abuse_check") or {}).get("versuche", 0))


def _quarantine_hls(obj) -> Optional[str]:
    """nginx serves .m3u8/.ts below the upload root straight from disk, without
    the app (storage-api nginx, /uploads/*.m3u8|*.ts). A blocked video must
    leave that tree: move its HLS folder outside, or failing that rename it
    in place to an unguessable name. Never deleted (a false alarm can be
    undone by an admin). Returns where it went, or None."""
    try:
        from storage.service import generic_storage
        key = getattr(obj, "object_key", None)
        if not key:
            return None
        src = Path(generic_storage.media_dir) / (obj.tenant_id or "arkturian") / Path(key).stem
        if not src.is_dir():
            return None
        qroot = Path(os.getenv("ABUSE_QUARANTINE_DIR")
                     or Path(generic_storage.upload_root).parent.parent / "storage-quarantine")
        try:
            qroot.mkdir(parents=True, exist_ok=True)
            dst = qroot / f"{obj.id}_{src.name}"
            shutil.move(str(src), str(dst))
            return str(dst)
        except Exception as e:
            dst = src.with_name(f".q_{secrets.token_hex(16)}")
            src.rename(dst)
            print(f"⚠️ abuse quarantine fell back to rename object={obj.id}: {type(e).__name__}", flush=True)
            return str(dst)
    except Exception as e:
        print(f"🔴 abuse quarantine of HLS failed object={obj.id}: {type(e).__name__}: {e}", flush=True)
        return "FEHLER"


def _drop_embedding(obj) -> None:
    """The knowledge graph may hold a description of the image; remove it.
    It is derived data and can be rebuilt if an admin lifts the block."""
    try:
        from knowledge_graph.vector_store import get_vector_store
        get_vector_store(tenant_id=obj.tenant_id).delete_embedding(obj.id)
    except Exception as e:
        print(f"⚠️ abuse: embedding not removed object={obj.id}: {type(e).__name__}", flush=True)


def apply_hit(db, obj, kategorie: str, p: float, anlass: str) -> None:
    from models import ShareLink
    now = datetime.utcnow()
    obj.abuse_status = "hit"
    obj.is_public = False
    obj.public_pending = False
    obj.visibility_level = None
    md = dict(obj.metadata_json or {}) if isinstance(obj.metadata_json, dict) else {}
    md["private_media"] = True
    obj.metadata_json = md
    _record(obj, kategorie=kategorie, p=round(p, 3), am=now.isoformat() + "Z")
    if getattr(obj, "hls_url", None):
        _record(obj, hls_url=obj.hls_url, hls_quarantaene=_quarantine_hls(obj))
        obj.hls_url = None
    _drop_embedding(obj)
    db.query(ShareLink).filter(ShareLink.object_id == obj.id, ShareLink.revoked_at.is_(None)).update(
        {ShareLink.revoked_at: now, ShareLink.revoked_by: "abuse_check"}, synchronize_session=False)
    db.commit()
    print(f"🚫 abuse hit object={obj.id} kategorie={kategorie} p={p:.2f} anlass={anlass}", flush=True)
    from storage.encrypted_io import emit_event
    emit_event("abuse_suspected", {"object_id": obj.id, "tenant_id": obj.tenant_id,
                                   "owner_user_id": obj.owner_user_id, "kategorie": kategorie,
                                   "wahrscheinlichkeit": round(p, 3), "anlass": anlass,
                                   "geprueft_am": now.isoformat() + "Z", "gesperrt": True})


async def check(db, obj, anlass: str) -> str:
    """Run one check and apply it. Returns the new abuse_status."""
    if getattr(obj, "abuse_status", None) in ("clear", "hit"):
        return obj.abuse_status
    _record(obj, versuche=_attempts(obj) + 1, anlass=anlass)
    try:
        probs = await _ask(obj)
    except CheckUnavailable as e:
        obj.abuse_status = "failed"
        _record(obj, fehler=str(e)[:80], zuletzt=datetime.utcnow().isoformat() + "Z")
        db.commit()
        print(f"⚠️ abuse check unavailable object={obj.id}: {e}", flush=True)
        return "failed"
    result, kategorie, p = _verdict(probs)
    if result == "hit":
        apply_hit(db, obj, kategorie, p, anlass)
        return "hit"
    obj.abuse_status = "clear"
    _record(obj, p_max=round(p, 3), am=datetime.utcnow().isoformat() + "Z", fehler=None)
    if getattr(obj, "public_pending", False):
        obj.is_public = True
        obj.public_pending = False
        obj.visibility_level = None
    db.commit()
    return "clear"


async def request_public(db, obj, anlass: str) -> None:
    """Make obj public, through the check where one is required."""
    if obj is None:
        return
    if not needs_check(obj) or obj.abuse_status == "clear":
        if not obj.is_public:
            obj.is_public = True
            obj.public_pending = False
            obj.visibility_level = None
            db.commit()
        return
    if obj.abuse_status == "hit":
        return
    obj.is_public = False
    obj.public_pending = True
    obj.abuse_status = "pending"
    db.commit()
    try:
        await asyncio.wait_for(check(db, obj, anlass), timeout=SYNC_TIMEOUT + 2)
    except asyncio.TimeoutError:
        obj.abuse_status = "failed"
        db.commit()
    db.refresh(obj)


async def ensure_for_share(db, obj) -> Optional[str]:
    """A share link of an image/video works only after "clear"."""
    if not needs_check(obj):
        return None
    if obj.abuse_status in ("clear", "hit"):
        return obj.abuse_status
    obj.abuse_status = "pending"
    db.commit()
    try:
        return await asyncio.wait_for(check(db, obj, "share_link"), timeout=SYNC_TIMEOUT + 2)
    except asyncio.TimeoutError:
        obj.abuse_status = "failed"
        db.commit()
        return "failed"


def share_allowed(obj) -> bool:
    """Opening a share link: images/videos only with a clean verdict."""
    if getattr(obj, "abuse_status", None) == "hit":
        return False
    if not needs_check(obj):
        return True
    return obj.abuse_status == "clear"


async def retry_open(session_factory, limit: int = 20) -> int:
    """Catch up pending/failed checks (fail-closed means: keep trying)."""
    from models import StorageObject
    db = session_factory()
    done = 0
    try:
        rows = (db.query(StorageObject)
                .filter(StorageObject.abuse_status.in_(("pending", "failed")),
                        StorageObject.tombstoned_at.is_(None))
                .order_by(StorageObject.id).limit(limit).all())
        for o in rows:
            if _attempts(o) >= MAX_ATTEMPTS:
                continue
            await check(db, o, "nachgeholt")
            done += 1
    finally:
        db.close()
    return done


async def retry_loop() -> None:
    import database
    while True:
        await asyncio.sleep(RETRY_EVERY)
        if not enabled():
            continue
        try:
            n = await retry_open(database.SessionLocal)
            if n:
                print(f"🔁 abuse check retried {n}", flush=True)
        except Exception as e:
            print(f"⚠️ abuse retry loop: {type(e).__name__}: {e}", flush=True)
