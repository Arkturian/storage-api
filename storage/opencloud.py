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
* Only the folder sync marks an object missing, and only after it could read
  the whole bound folder. A folder it may not enter (EACCES) is an error of
  that run, never a deletion (FileSync, 13.11.: OpenCloud creates new folders
  0700, the ACL mask then hides them from www-data).
* Only folders listed in the bindings file are synced: below the projects
  root, or a read-only view under ``OPENCLOUD_VIEWS_ROOT`` (bindfs) with an
  explicit ``space_id``. A binding path the running user can write to is
  refused. The personal drives under ``users/`` are never touched.
* Sensitive bindings always run with ``ai_mode="none"`` (no analysis, no
  embeddings, nothing leaves the house).
"""
from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
import re
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
_DEFAULT_VIEWS_ROOT = "/srv/opencloud-views"
_SPACE_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


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


def views_root() -> Path:
    return Path(os.getenv("OPENCLOUD_VIEWS_ROOT", _DEFAULT_VIEWS_ROOT)).resolve()


def _is_hidden(name: str) -> bool:
    # .oc-nodes, .oc-tmp, .Trash and editor/OS droppings like .DS_Store
    return name.startswith(".")


def iter_files(root: Path, errors: Optional[List[str]] = None) -> Iterator[Path]:
    """Regular files below root, without OpenCloud's internal folders or symlinks.

    os.walk skips a folder it cannot list without a word. Every such folder,
    and every file that cannot be stat'ed, is appended to ``errors`` so the
    caller knows the listing is incomplete.
    """
    errs = errors if errors is not None else []

    def _onerror(exc: OSError) -> None:
        errs.append(f"{exc.filename}: {exc.strerror or exc}")

    for dirpath, dirnames, filenames in os.walk(root, onerror=_onerror, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not _is_hidden(d))
        for name in sorted(filenames):
            if _is_hidden(name):
                continue
            p = Path(dirpath) / name
            try:
                if p.is_symlink() or not p.is_file():
                    continue
            except OSError as exc:
                errs.append(f"{p}: {exc.strerror or exc}")
                continue
            yield p


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
    name = raw["name"]
    root, views = projects_root(), views_root()
    try:
        path = Path(raw["path"]).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise BindingError(f"binding '{name}': path not readable ({exc})") from exc
    if not path.is_dir():
        raise BindingError(f"binding '{name}': path is not a folder")
    given = str(raw.get("space_id") or "").strip().lower() or None
    if given and not _SPACE_ID_RE.match(given):
        raise BindingError(f"binding '{name}': space_id is not a space uuid")
    if root in path.parents:
        space_id = path.relative_to(root).parts[0]
        if given and given != space_id:
            raise BindingError(f"binding '{name}': space_id {given} does not match the path ({space_id})")
        space_xattr = _xattr(root / space_id, XATTR_SPACE_ID)
        if space_xattr and space_xattr != space_id:
            raise BindingError(f"binding '{name}': space id mismatch ({space_xattr} != {space_id})")
    elif views in path.parents:
        if not given:
            raise BindingError(f"binding '{name}': a view below {views} needs an explicit space_id")
        space_id = given
    else:
        raise BindingError(f"binding '{name}': path must lie inside a space below {root} "
                           f"or in a read-only view below {views}")
    if os.access(path, os.W_OK) and os.getenv("OPENCLOUD_ALLOW_WRITABLE") != "1":
        raise BindingError(f"binding '{name}': path is writable for this user; storage must only read (rule 8)")
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
    unreadable: bool = False


def refresh_reference(obj, db, *, at: Optional[Path] = None, commit: bool = True) -> RefreshResult:
    """Bring an OpenCloud reference object in line with its file.

    ``at`` is where the folder sync just found the file with this object's id
    (a move or rename); without it the stored ``reference_path`` is checked.
    Recomputes size and checksum and drops cached derivatives when mtime or
    size changed. It never marks an object missing: a file that is not where
    the object points comes back as ``missing`` (or ``unreadable`` on EACCES)
    without touching the state. Only the folder sync, after a complete walk,
    decides that a file is gone (``mark_missing``).
    """
    if not is_opencloud_object(obj):
        return RefreshResult(path=Path(obj.reference_path) if obj.reference_path else None)

    res = RefreshResult(path=None)
    was_missing = getattr(obj, "reference_state", None) == STATE_MISSING
    path = at or (Path(obj.reference_path) if obj.reference_path else None)
    try:
        ok = path is not None and path.is_file() and file_id(path) == obj.opencloud_file_id
        st = path.stat() if ok else None
    except PermissionError:
        res.unreadable = True
        return res
    except OSError:
        ok, st = False, None
    if not ok:
        res.missing = True
        return res
    if obj.reference_path != str(path):
        obj.reference_path = str(path)
        res.moved = True

    if (st.st_mtime_ns, st.st_size) != (obj.reference_mtime_ns, obj.reference_size):
        try:
            checksum = _sha256_of(path)
        except PermissionError:
            res.unreadable = True
            return res
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


def mark_missing(obj, db, *, commit: bool = True) -> bool:
    """Mark an object whose file left its folder. Never deletes. True if the state changed."""
    if getattr(obj, "reference_state", None) == STATE_MISSING:
        return False
    obj.reference_state = STATE_MISSING
    if commit:
        db.commit()
    return True


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
    walk_errors: List[str] = []
    for path in iter_files(binding.path, walk_errors):
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
        res = refresh_reference(obj, db, at=path)
        if res.unreadable:
            report.errors.append(f"{path}: not readable")
            continue
        if res.moved:
            report.moved.append(obj.id)
        if res.changed:
            report.changed.append(obj.id)
            if binding.effective_ai_mode != "none":
                from storage.service import enqueue_ai_safety_and_transcoding
                await enqueue_ai_safety_and_transcoding(obj, db=db, ai_mode=binding.effective_ai_mode)
        if res.restored:
            report.restored.append(obj.id)

    report.errors.extend(walk_errors)
    if walk_errors or dry_run:
        # The listing is incomplete: a file in a folder we could not enter is
        # not gone. Mark nothing missing in this run.
        return report

    # Objects of this folder whose file id did not show up anywhere in the
    # (completely read) folder have left it: mark missing, never delete.
    stale = (db.query(StorageObject)
             .filter(StorageObject.collection_id == binding.collection_id,
                     StorageObject.tenant_id == binding.tenant_id,
                     StorageObject.opencloud_space_id == binding.space_id,
                     StorageObject.opencloud_file_id.isnot(None))
             .all())
    for obj in stale:
        if obj.opencloud_file_id in seen:
            continue
        if mark_missing(obj, db):
            report.missing.append(obj.id)
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
