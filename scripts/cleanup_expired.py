#!/usr/bin/env python3
"""
cleanup_expired.py — Purge storage objects whose TTL has expired.

Uses the SAME deletion pipeline as the DELETE /storage/{id} endpoint:
  - generic_storage.delete() → removes original + thumbnail + webview + HLS
  - Knowledge Graph embedding deletion
  - Async task cleanup
  - Cascade delete of linked children
  - DB row removal

Cron (every 15 minutes):
    */15 * * * * cd /var/www/api-storage.arkturian.com && ./venv/bin/python scripts/cleanup_expired.py >> /tmp/cleanup_expired.log 2>&1
"""

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func
from database import SessionLocal
from models import StorageObject
from storage.service import generic_storage


def cleanup_expired(batch_size: int = 50) -> int:
    """Delete expired storage objects using the full deletion pipeline."""
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        expired = (
            db.query(StorageObject)
            .filter(
                StorageObject.expires_at.isnot(None),
                StorageObject.expires_at < now,
            )
            .limit(batch_size)
            .all()
        )

        if not expired:
            return 0

        count = 0
        for obj in expired:
            try:
                _full_delete(db, obj)
                count += 1
            except Exception as e:
                db.rollback()
                print(f"[cleanup_expired] error deleting {obj.id} ({obj.object_key}): {e}")

        return count
    finally:
        db.close()


def _full_delete(db, obj: StorageObject):
    """Full deletion pipeline matching _perform_delete in routes.py."""

    # 1. Cascade delete linked children
    linked_children = (
        db.query(StorageObject)
        .filter(
            StorageObject.link_id == str(obj.id),
            StorageObject.owner_user_id == obj.owner_user_id,  # #2915
            StorageObject.tenant_id == obj.tenant_id,
        )
        .all()
    )
    for child in linked_children:
        _delete_embedding(child)
        if child.storage_mode not in ("external", "reference"):
            generic_storage.delete(child.object_key, child.tenant_id or "arkturian")
        db.delete(child)

    if linked_children:
        db.commit()

    # 2. Delete embedding from Knowledge Graph
    _delete_embedding(obj)

    # 3. Delete async tasks
    try:
        from models import Base
        # AsyncTask might not exist in all deployments
        from sqlalchemy import text
        db.execute(
            text("DELETE FROM async_tasks WHERE object_id = :oid"),
            {"oid": obj.id},
        )
    except Exception:
        pass

    # 4. Delete physical files (original + thumbnails + webview + HLS)
    if obj.storage_mode not in ("external", "reference"):
        generic_storage.delete(obj.object_key, obj.tenant_id or "arkturian")

    # 5. Delete DB row
    db.delete(obj)
    db.commit()


def _delete_embedding(obj: StorageObject):
    """Best-effort remove from Knowledge Graph vector store."""
    try:
        from knowledge_graph.vector_store import get_vector_store
        store = get_vector_store(tenant_id=obj.tenant_id or "arkturian")
        store.delete_embedding(obj.id)
    except Exception:
        pass


if __name__ == "__main__":
    count = cleanup_expired()
    if count > 0:
        print(f"[{datetime.utcnow().isoformat()}] purged {count} expired assets")
