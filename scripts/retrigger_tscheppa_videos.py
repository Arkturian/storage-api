#!/usr/bin/env python3
"""
One-off: re-trigger the AI analysis (vision safety + quality + title/tags +
audio transcription) on the 57 Tscheppaschlucht videos that were uploaded with
the new ai_mode=none default and never got analyzed.

Does ONLY the AI analysis — does NOT re-transcode (they're already HLS'd) and
does NOT touch knowledge_id / the existing links. Per Knowledge/Alex request
2026-06-14.

Run:  sudo -u www-data .../venv/bin/python scripts/retrigger_tscheppa_videos.py
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import SessionLocal
from models import StorageObject
from storage.service import generic_storage, extract_thumbnails_for_ai
from tasks.ai_analysis import process_video_analysis

# Mirror enqueue_ai_safety_and_transcoding's scratch dir (local var there, not importable)
_AI_SCRATCH_ROOT = Path(os.getenv("AI_SCRATCH_DIR", "/var/lib/storage-api/tmp-ai"))
try:
    _AI_SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
except Exception:
    _AI_SCRATCH_ROOT = Path("/tmp")

IDS = [
    36360, 36361, 36363, 36364, 36365, 36366, 36367, 36368, 36369, 36370,
    36371, 36372, 36373, 36374, 36375, 36376, 36377, 36378, 36379, 36380,
    36381, 36382, 36383, 36384, 36385, 36386, 36387, 36388, 36389, 36390,
    36391, 36392, 36393, 36394, 36395, 36396, 36397, 36398, 36399, 36400,
    36401, 36402, 36403, 36404, 36405, 36406, 36407, 36408, 36409, 36410,
    36411, 36412, 36413, 36414, 36415, 36416, 36417,
]


def main():
    db = SessionLocal()
    enqueued, skipped, errors = 0, 0, 0
    try:
        for oid in IDS:
            obj = db.query(StorageObject).filter(StorageObject.id == oid).first()
            if not obj:
                print(f"  ⚠️  {oid}: not found", flush=True)
                errors += 1
                continue
            if not (obj.mime_type or "").startswith("video/"):
                print(f"  ⚠️  {oid}: not a video ({obj.mime_type})", flush=True)
                skipped += 1
                continue
            try:
                # opt into audio transcription (fills audio_transcript)
                mj = dict(obj.metadata_json or {})
                mj["transcribe_audio"] = True
                obj.metadata_json = mj
                obj.ai_safety_status = "pending"  # mark as queued
                db.commit()

                video_path = generic_storage.absolute_path_for_key(
                    obj.object_key, obj.tenant_id or "arkturian")
                if not Path(str(video_path)).exists():
                    print(f"  ❌ {oid}: file missing at {video_path}", flush=True)
                    errors += 1
                    continue

                thumb_dir = _AI_SCRATCH_ROOT / f"ai_thumbs_{oid}"
                rc = extract_thumbnails_for_ai(Path(str(video_path)), thumb_dir)
                if rc != 0:
                    print(f"  ❌ {oid}: frame extraction failed rc={rc}", flush=True)
                    errors += 1
                    continue

                process_video_analysis.delay(oid, str(thumb_dir), obj.original_filename)
                enqueued += 1
                print(f"  ✅ {oid} enqueued ({enqueued}/{len(IDS)})", flush=True)
            except Exception as e:
                db.rollback()
                print(f"  ❌ {oid}: {e}", flush=True)
                errors += 1
            time.sleep(0.3)  # gentle on ffmpeg
    finally:
        db.close()
    print(f"\n=== DONE: {enqueued} enqueued, {skipped} skipped, {errors} errors ===", flush=True)


if __name__ == "__main__":
    main()
