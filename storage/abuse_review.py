"""Admin review of an abuse-check verdict (#3346).

A hit locks a file for everyone but a real admin; stage 1 (held) keeps it
private and off share links. Until now only a database edit undid either.

    POST /storage/objects/{id}/abuse-review   {"entscheidung": "fehlalarm"|"bestaetigt", "begruendung": "..."}
    GET  /storage/objects/{id}/abuse-review   -> the log for this object

Only a real admin (auth.is_privileged_admin: never a tenant key, never an
impersonated principal, never the public default master). Every decision is
written to abuse_reviews and announced as storage.abuse_reviewed.

fehlalarm: abuse_status -> clear, HLS folder back from quarantine and
hls_url restored. private_media stays (one-way by design); the owner sets
the visibility again. The knowledge-graph embedding is not rebuilt here;
it comes back with the next analysis run (ai_mode / backfill).
bestaetigt: nothing is lifted; the decision is recorded.
"""
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from auth import get_current_user
from database import get_db
from models import AbuseReview, StorageObject, User
from storage import routes as R

router = APIRouter()


class AbuseReviewRequest(BaseModel):
    entscheidung: str
    begruendung: str


def _require_real_admin(current_user, db) -> str:
    if not R._is_privileged_admin(current_user, db) or R._is_public_default_master(current_user):
        raise HTTPException(status_code=403, detail={"error": "only a real admin may review abuse verdicts", "code": "admin_required"})
    return str(getattr(current_user, "email", None) or f"user:{getattr(current_user, 'id', '?')}")


def _restore_hls(obj, rec: dict) -> str:
    """Move the quarantined HLS folder back; returns what happened."""
    src = rec.get("hls_quarantaene")
    url = rec.get("hls_url")
    if not src or src == "FEHLER" or not url:
        return "keins" if not src else "fehlt"
    try:
        from storage.service import generic_storage
        dst = Path(generic_storage.media_dir) / (obj.tenant_id or "arkturian") / Path(obj.object_key).stem
        if dst.exists():
            return "ziel_belegt"
        if not Path(src).is_dir():
            return "fehlt"
        shutil.move(src, str(dst))
        obj.hls_url = url
        return "zurueck"
    except Exception as e:
        print(f"⚠️ abuse review: HLS not restored object={obj.id}: {type(e).__name__}", flush=True)
        return "fehler"


@router.post("/objects/{object_id}/abuse-review")
def review_abuse_verdict(
    object_id: int,
    payload: AbuseReviewRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    admin = _require_real_admin(current_user, db)
    entscheidung = (payload.entscheidung or "").strip().lower()
    if entscheidung not in ("fehlalarm", "bestaetigt"):
        raise HTTPException(status_code=422, detail={"error": "entscheidung must be fehlalarm or bestaetigt", "code": "invalid_entscheidung"})
    begruendung = (payload.begruendung or "").strip()
    if not (10 <= len(begruendung) <= 2000):
        raise HTTPException(status_code=422, detail={"error": "begruendung: 10..2000 characters", "code": "invalid_begruendung"})
    obj = db.query(StorageObject).filter(StorageObject.id == object_id).first()
    if obj is None or getattr(obj, "tombstoned_at", None) is not None:
        raise HTTPException(status_code=404, detail="Not found")
    vorher = getattr(obj, "abuse_status", None)
    if vorher not in ("hit", "held"):
        raise HTTPException(status_code=409, detail={"error": "no blocking verdict on this object", "code": "nothing_to_review",
                                                     "abuse_status": vorher})
    now = datetime.utcnow()
    md = dict(obj.metadata_json or {}) if isinstance(obj.metadata_json, dict) else {}
    rec = dict(md.get("abuse_check") or {})
    hls = None
    if entscheidung == "fehlalarm":
        hls = _restore_hls(obj, rec) if vorher == "hit" else "keins"
        obj.abuse_status = "clear"
        rec.update(aufgehoben_von=admin, aufgehoben_am=now.isoformat() + "Z")
        if hls == "zurueck":
            rec.pop("hls_quarantaene", None)
    else:
        rec.update(bestaetigt_von=admin, bestaetigt_am=now.isoformat() + "Z")
    md["abuse_check"] = rec
    obj.metadata_json = md
    db.add(AbuseReview(object_id=obj.id, tenant_id=obj.tenant_id, entscheidung=entscheidung,
                       status_vorher=vorher, status_nachher=obj.abuse_status, begruendung=begruendung,
                       admin=admin, created_at=now))
    db.commit()
    print(f"⚖️ abuse review object={obj.id} {vorher} -> {obj.abuse_status} entscheidung={entscheidung} admin={admin}", flush=True)
    from storage.encrypted_io import emit_event
    emit_event("abuse_reviewed", {"object_id": obj.id, "tenant_id": obj.tenant_id, "entscheidung": entscheidung,
                                  "status_vorher": vorher, "status_nachher": obj.abuse_status,
                                  "admin": admin, "am": now.isoformat() + "Z"})
    out = {"object_id": obj.id, "entscheidung": entscheidung, "status_vorher": vorher,
           "status_nachher": obj.abuse_status, "private_media": bool(md.get("private_media"))}
    if hls is not None:
        out["hls"] = hls
    return out


@router.get("/objects/{object_id}/abuse-review")
def list_abuse_reviews(
    object_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_real_admin(current_user, db)
    rows = db.query(AbuseReview).filter(AbuseReview.object_id == object_id).order_by(AbuseReview.id).all()
    return {"object_id": object_id, "reviews": [
        {"id": r.id, "entscheidung": r.entscheidung, "status_vorher": r.status_vorher,
         "status_nachher": r.status_nachher, "begruendung": r.begruendung, "admin": r.admin,
         "am": r.created_at.isoformat() + "Z" if r.created_at else None} for r in rows]}
