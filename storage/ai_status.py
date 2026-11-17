"""Where AI-analysis progress files live (#3243).

storage-api has its own tmpfs /tmp since the #3230 switchover, the workers do
not share it. Progress written by the worker under /tmp was therefore never
seen by GET /storage/processing-status. Both sides now use one directory under
the shared scratch root (group storage-db, setgid).
"""
import logging
import os
import shutil
from pathlib import Path

_log = logging.getLogger(__name__)


def ai_status_dir(object_id: int) -> Path:
    root = os.getenv("AI_STATUS_DIR") or str(
        Path(os.getenv("AI_SCRATCH_DIR", "/var/lib/storage-api/tmp-ai")) / "status")
    return Path(root) / f"ai_analysis_status_{int(object_id)}"


def cleanup_scratch_dir(scratch_path: str) -> None:
    """
    Remove the pre-processing scratch directory after a task finishes.

    Called only on the success path — on failure we keep the inputs around
    so retries can re-use them and humans can inspect what tripped the
    classifier. Scratch lives outside /tmp to survive the nightly tmp cleanup,
    but it is *not* meant to grow unbounded — every successful task purges
    its own dir. Best-effort; never raises.
    """
    try:
        p = Path(scratch_path)
        # Only delete dirs we created (under AI_SCRATCH_DIR or /tmp/ai_*),
        # never an arbitrary path that ended up in the queue.
        prefixes = ("ai_thumbs_", "ai_images_")
        if p.is_dir() and p.name.startswith(prefixes):
            shutil.rmtree(p, ignore_errors=True)
            return
        # #3243: a file handed over inside its scratch dir (external mode).
        # The parent branch used to be unreachable behind the name check.
        # Only directly under the scratch root, never a stored object's dir.
        scratch_root = Path(os.getenv("AI_SCRATCH_DIR", "/var/lib/storage-api/tmp-ai")).resolve()
        parent = p.resolve().parent
        if p.is_file() and parent.name.startswith(prefixes) and parent.parent == scratch_root:
            shutil.rmtree(parent, ignore_errors=True)
    except Exception as e:
        _log.warning(f"   ⚠️  Failed to cleanup scratch dir {scratch_path}: {e}")
