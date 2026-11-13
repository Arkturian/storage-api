"""OpenCloud project folders as reference objects (#2810, concept Post 5939).

OpenCloud on arkserver runs the posix storage driver: every project space is a
plain directory under ``OPENCLOUD_PROJECTS_ROOT/<space-id>/`` that holds the
files under their real names. OpenCloud keeps its own ids in extended
attributes, ``user.oc.id`` on every node and ``user.oc.space.id`` on the space
root. The file id survives a move or rename inside the space.

Storage registers such files as ``storage_mode="reference"`` objects and reads
the bytes in place. Rules from the concept (Pflichtregel 8):

* Storage only reads. Nothing in this module writes, renames or deletes a file
  below the projects root; the www-data ACL on the bound folders is r-x anyway.
* The sync never deletes a storage object. A file that is gone sets
  ``reference_state="missing"``; when it comes back (restore from the trash, a
  version, a move back) the object re-attaches through its file id.
* Only folders listed in the bindings file are synced, and only below the
  projects root. The personal drives under ``users/`` are never touched.
* Sensitive bindings always run with ``ai_mode="none"`` (no analysis, no
  embeddings, nothing leaves the house).
"""
from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, List, Optional

logger = logging.getLogger(__name__)

XATTR_ID = "user.oc.id"
XATTR_SPACE_ID = "user.oc.space.id"
STATE_OK = "ok"
STATE_MISSING = "missing"
_AI_MODES = {"none", "safety", "vision", "full"}
_DEFAULT_PROJECTS_ROOT = "/var/lib/opencloud/storage/users/projects"


def projects_root() -> Path:
    return Path(os.getenv("OPENCLOUD_PROJECTS_ROOT", _DEFAULT_PROJECTS_ROOT)).resolve()


def _xattr(path: Path, name: str) -> Optional[str]:
    try:
        raw = os.getxattr(str(path), name, follow_symlinks=False)
    except (OSError, AttributeError):
        return None
    value = raw.decode("utf-8", "replace").strip().strip("\x00")
    return value or None


def file_id(path: Path) -> Optional[str]:
    """OpenCloud node id of a file or folder, None if it carries none."""
    return _xattr(path, XATTR_ID)


def space_root(space_id: str) -> Path:
    return projects_root() / space_id


def _is_hidden(name: str) -> bool:
    # .oc-nodes, .oc-tmp, .Trash and editor/OS droppings like .DS_Store
    return name.startswith(".")


def iter_files(root: Path) -> Iterator[Path]:
    """Regular files below root, without OpenCloud's internal folders or symlinks."""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not _is_hidden(d))
        for name in sorted(filenames):
            if _is_hidden(name):
                continue
            p = Path(dirpath) / name
            if p.is_file() and not p.is_symlink():
                yield p


def find_by_file_id(space_id: str, wanted: str) -> Optional[Path]:
    """Locate a moved or renamed file inside its space by its OpenCloud id."""
    root = space_root(space_id)
    if not root.is_dir():
        return None
    for p in iter_files(root):
        if file_id(p) == wanted:
            return p
    return None


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# --- bindings -----------------------------------------------------------------

class BindingError(ValueError):
    pass


@dataclass
class Binding:
    """One OpenCloud folder released for storage (maintained by FileSync)."""
    name: str
    path: Path
    space_id: str
    collection_id: str
    tenant_id: str
    owner_user_id: int
    sensitive: bool = False
    ai_mode: str = "none"
    is_public: bool = False

    @property
    def effective_ai_mode(self) -> str:
        return "none" if self.sensitive else self.ai_mode


def parse_binding(raw: dict) -> Binding:
    for key in ("name", "path", "collection_id", "owner_user_id"):
        if raw.get(key) in (None, ""):
            raise BindingError(f"binding is missing '{key}'")
    root = projects_root()
    try:
        path = Path(raw["path"]).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise BindingError(f"binding '{raw['name']}': path not readable ({exc})") from exc
    if path == root or root not in path.parents:
        raise BindingError(f"binding '{raw['name']}': path must lie inside a space below {root}")
    space_id = path.relative_to(root).parts[0]
    space_xattr = _xattr(root / space_id, XATTR_SPACE_ID)
    if space_xattr and space_xattr != space_id:
        raise BindingError(f"binding '{raw['name']}': space id mismatch ({space_xattr} != {space_id})")
    ai_mode = str(raw.get("ai_mode") or "none").lower()
    if ai_mode not in _AI_MODES:
        raise BindingError(f"binding '{raw['name']}': unknown ai_mode '{ai_mode}'")
    return Binding(
        name=str(raw["name"]),
        path=path,
        space_id=space_id,
        collection_id=str(raw["collection_id"]),
        tenant_id=str(raw.get("tenant_id") or "arkturian"),
        owner_user_id=int(raw["owner_user_id"]),
        sensitive=bool(raw.get("sensitive", False)),
        ai_mode=ai_mode,
        is_public=bool(raw.get("is_public", False)),
    )


def load_bindings(path: Optional[str] = None) -> List[Binding]:
    path = path or os.getenv("OPENCLOUD_BINDINGS_PATH", "")
    if not path:
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise BindingError("bindings file must hold a JSON list")
    return [parse_binding(item) for item in data]


# --- freshness of one object -----------------------------------------------------

def is_opencloud_object(obj) -> bool:
    return getattr(obj, "storage_mode", None) == "reference" and bool(getattr(obj, "opencloud_file_id", None))


def invalidate_derivatives(obj) -> int:
    """Drop every cached derivative of obj so the next request renders from the new bytes."""
    from storage.service import generic_storage

    tenant = obj.tenant_id or "arkturian"
    stem = Path(obj.object_key).stem if obj.object_key else None
    patterns = [f"oriented_{obj.id}.jpg", f"vidframe_{obj.id}_*", f"aud_{obj.id}_*"]
    if stem:
        patterns += [f"web_{stem}*", f"thumb_{stem}*", f"pdfpreview_{stem}_*", f"glb_{stem}_*"]
    removed = 0
    dirs = {generic_storage.webview_dir, generic_storage.webview_dir / tenant,
            generic_storage.thumbnails_dir, generic_storage.thumbnails_dir / tenant}
    for d in dirs:
        if not d.exists():
            continue
        for pat in patterns:
            for f in d.glob(pat):
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    pass
    return removed


@dataclass
class RefreshResult:
    path: Optional[Path]
    moved: bool = False
    changed: bool = False
    missing: bool = False
    restored: bool = False


def refresh_reference(obj, db, *, commit: bool = True, search_missing: bool = True) -> RefreshResult:
    """Bring an OpenCloud reference object in line with its file.

    Re-resolves a moved file by its OpenCloud id, recomputes size and checksum
    and drops cached derivatives when mtime or size changed, and marks the
    object missing (never deletes it) when the file is gone.

    search_missing=False skips the walk through the space for an object that
    is already marked missing (the media route uses it so a dead link does not
    scan the space on every request; the folder sync still finds a return).
    """
    if not is_opencloud_object(obj):
        return RefreshResult(path=Path(obj.reference_path) if obj.reference_path else None)

    res = RefreshResult(path=None)
    was_missing = getattr(obj, "reference_state", None) == STATE_MISSING
    path = Path(obj.reference_path) if obj.reference_path else None
    if path is None or not path.is_file() or file_id(path) != obj.opencloud_file_id:
        if was_missing and not search_missing:
            res.missing = True
            return res
        found = find_by_file_id(obj.opencloud_space_id, obj.opencloud_file_id) if obj.opencloud_space_id else None
        if found is None:
            if not was_missing:
                obj.reference_state = STATE_MISSING
                if commit:
                    db.commit()
            res.missing = True
            return res
        if path is None or found != path:
            obj.reference_path = str(found)
            res.moved = True
        path = found

    st = path.stat()
    if (st.st_mtime_ns, st.st_size) != (obj.reference_mtime_ns, obj.reference_size):
        checksum = _sha256_of(path)
        if checksum != obj.checksum or st.st_size != obj.file_size_bytes:
            invalidate_derivatives(obj)
            obj.checksum = checksum
            obj.file_size_bytes = st.st_size
            res.changed = True
        obj.reference_mtime_ns = st.st_mtime_ns
        obj.reference_size = st.st_size
    if was_missing:
        res.restored = True
    obj.reference_state = STATE_OK
    if res.moved:
        obj.original_filename = path.name
    if commit and (res.moved or res.changed or res.restored or db.is_modified(obj)):
        db.commit()
    res.path = path
    return res


# --- folder sync ---------------------------------------------------------------------

@dataclass
class SyncReport:
    binding: str
    created: List[int] = field(default_factory=list)
    changed: List[int] = field(default_factory=list)
    moved: List[int] = field(default_factory=list)
    restored: List[int] = field(default_factory=list)
    missing: List[int] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in
                ("binding", "created", "changed", "moved", "restored", "missing", "skipped", "errors")}


async def _create_object(binding: Binding, path: Path, fid: str, db):
    from config import settings
    from storage.domain import save_file_and_record

    st = path.stat()
    if st.st_size > settings.MAX_FILE_SIZE:
        raise ValueError(f"file too large ({st.st_size} bytes)")
    data = path.read_bytes()
    mime = mimetypes.guess_type(path.name)[0]
    obj = await save_file_and_record(
        db,
        owner_user_id=binding.owner_user_id,
        data=data,
        original_filename=path.name,
        context="opencloud",
        is_public=binding.is_public,
        collection_id=binding.collection_id,
        storage_mode="reference",
        reference_path=str(path),
        ai_context_metadata={"source": "opencloud", "binding": binding.name,
                             "file_path": str(path.relative_to(binding.path))},
        tenant_id=binding.tenant_id,
        mime_type=mime,
    )
    obj.opencloud_space_id = binding.space_id
    obj.opencloud_file_id = fid
    obj.reference_mtime_ns = st.st_mtime_ns
    obj.reference_size = st.st_size
    obj.reference_state = STATE_OK
    if not binding.is_public:
        md = dict(obj.metadata_json or {})
        md["private_media"] = True
        obj.metadata_json = md
    db.commit()
    if binding.effective_ai_mode != "none":
        from storage.service import enqueue_ai_safety_and_transcoding
        await enqueue_ai_safety_and_transcoding(obj, db=db, ai_mode=binding.effective_ai_mode)
    return obj


async def sync_binding(binding: Binding, db, *, dry_run: bool = False) -> SyncReport:
    """Mirror one released folder into its collection. Never deletes anything."""
    from models import StorageObject

    report = SyncReport(binding=binding.name)
    seen: set = set()
    for path in iter_files(binding.path):
        fid = file_id(path)
        if not fid:
            report.skipped.append(str(path.relative_to(binding.path)))
            continue
        seen.add(fid)
        obj = (db.query(StorageObject)
               .filter(StorageObject.opencloud_file_id == fid,
                       StorageObject.tenant_id == binding.tenant_id)
               .first())
        if obj is None:
            if dry_run:
                report.created.append(-1)
                continue
            try:
                obj = await _create_object(binding, path, fid, db)
                report.created.append(obj.id)
            except Exception as exc:  # one bad file must not stop the folder
                db.rollback()
                report.errors.append(f"{path.name}: {exc}")
            continue
        if dry_run:
            continue
        res = refresh_reference(obj, db)
        if res.moved:
            report.moved.append(obj.id)
        if res.changed:
            report.changed.append(obj.id)
            if binding.effective_ai_mode != "none":
                from storage.service import enqueue_ai_safety_and_transcoding
                await enqueue_ai_safety_and_transcoding(obj, db=db, ai_mode=binding.effective_ai_mode)
        if res.restored:
            report.restored.append(obj.id)

    # Objects of this folder whose file did not show up: re-resolve by id
    # (moved elsewhere in the space), otherwise mark missing. Never delete.
    stale = (db.query(StorageObject)
             .filter(StorageObject.collection_id == binding.collection_id,
                     StorageObject.tenant_id == binding.tenant_id,
                     StorageObject.opencloud_space_id == binding.space_id,
                     StorageObject.opencloud_file_id.isnot(None))
             .all())
    for obj in stale:
        if obj.opencloud_file_id in seen or dry_run:
            continue
        res = refresh_reference(obj, db)
        if res.missing:
            report.missing.append(obj.id)
        elif res.moved:
            report.moved.append(obj.id)
    return report


async def sync_all(db, bindings: Optional[List[Binding]] = None, *, dry_run: bool = False) -> List[dict]:
    bindings = load_bindings() if bindings is None else bindings
    out = []
    for b in bindings:
        report = await sync_binding(b, db, dry_run=dry_run)
        logger.info("opencloud sync %s", report.as_dict())
        out.append(report.as_dict())
    return out


def _main() -> int:  # pragma: no cover - thin CLI wrapper
    import argparse
    import asyncio

    from database import SessionLocal

    ap = argparse.ArgumentParser(description="Sync released OpenCloud folders into storage (#2810)")
    ap.add_argument("--bindings", default=os.getenv("OPENCLOUD_BINDINGS_PATH", ""))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    db = SessionLocal()
    try:
        reports = asyncio.run(sync_all(db, load_bindings(args.bindings), dry_run=args.dry_run))
    finally:
        db.close()
    print(json.dumps(reports, indent=2))
    return 1 if any(r["errors"] for r in reports) else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
