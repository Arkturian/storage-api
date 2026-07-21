#!/usr/bin/env python3
"""merge_from_vps.py — Storage-consolidation Phase 1/2: merge the VPS instance
(api-storage.arkturian.com) into the arkserver DB, ID-preserving.

Validated preconditions (2026-07-21, see Content-thread + memory):
  * All VPS IDs >= 100000 are disjoint from arkserver (arkserver MAX id 37148,
    zero rows >= 100000).
  * The only overlapping IDs (4208, 4210-4223) are byte-identical duplicates
    (same checksum + external_uri on both sides) -> skipped.
  * tenant 'oneal' on the VPS is obsolete Nov-2025 test data -> skipped
    (the real O'Neal catalog lives on aiserver).

What it does (in one transaction on the TARGET db):
  1. users:    map VPS users by email -> existing arkserver user id, else
               insert (new id). Produces owner remap table.
  2. tenants + tenant_api_keys: upsert missing (so existing client keys like
               annasacher_vps_* keep working after cutover).
  3. storage_objects: insert with ORIGINAL id, owner_user_id remapped,
               only columns common to both schemas. Skip: tenant=oneal,
               ids already present with equal checksum. ABORT on id present
               with different checksum (should not happen).
  4. async_tasks are NOT migrated (ephemeral).

Usage:
  # 1. consistent snapshot on the VPS:
  #    ssh root@VPS "sqlite3 /var/lib/storage-api/storage.db \\".backup /tmp/vps_snapshot.db\\""
  #    scp root@VPS:/tmp/vps_snapshot.db /tmp/vps_snapshot.db
  # 2. dry-run against a COPY of the arkserver db:
  #    python3 scripts/merge_from_vps.py --source /tmp/vps_snapshot.db \\
  #        --target /tmp/arkserver_copy.db
  # 3. cutover run: same with --target /var/lib/storage-api/storage.db

The script is idempotent: rerunning skips ids that already exist with equal
checksum, so a delta-run after a fresh snapshot only adds new rows.
"""

import argparse
import sqlite3
import sys

SKIP_TENANTS = {"oneal"}


def cols(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, help="VPS snapshot sqlite file")
    ap.add_argument("--target", required=True, help="arkserver sqlite file (COPY for dry-run!)")
    ap.add_argument("--commit", action="store_true", help="actually write (default: rollback at end = dry-run)")
    ap.add_argument("--media-root", default="/mnt/backup-disk/uploads/storage/media",
                    help="media root for alias file copies (copy-mode key renames)")
    args = ap.parse_args()

    src = sqlite3.connect(f"file:{args.source}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    tgt = sqlite3.connect(args.target)
    tgt.row_factory = sqlite3.Row
    tgt.execute("BEGIN")

    report = {"users_mapped": 0, "users_created": 0, "tenants_created": 0,
              "keys_upserted": 0, "objects_inserted": 0, "objects_skipped_dup": 0,
              "objects_aliased": 0, "objects_skipped_oneal": 0, "conflicts": 0}

    # ---- 1. users: map by email --------------------------------------------
    user_map: dict[int, int] = {}
    ucols_common = [c for c in cols(src, "users") if c in cols(tgt, "users") and c != "id"]
    for u in src.execute("SELECT * FROM users"):
        row = tgt.execute("SELECT id FROM users WHERE email = ?", (u["email"],)).fetchone()
        if row:
            user_map[u["id"]] = row["id"]
            report["users_mapped"] += 1
        else:
            # api_key must stay unique; if the key already exists on target under
            # a different email, insert the user without api_key (login-less).
            vals = {c: u[c] for c in ucols_common}
            if vals.get("api_key") and tgt.execute(
                    "SELECT 1 FROM users WHERE api_key = ?", (vals["api_key"],)).fetchone():
                vals["api_key"] = None
            ph = ",".join("?" for _ in vals)
            cur = tgt.execute(
                f"INSERT INTO users ({','.join(vals)}) VALUES ({ph})", list(vals.values()))
            user_map[u["id"]] = cur.lastrowid
            report["users_created"] += 1

    # ---- 2. tenants + tenant_api_keys --------------------------------------
    for t in src.execute("SELECT * FROM tenants"):
        if t["id"] in SKIP_TENANTS:
            continue
        if not tgt.execute("SELECT 1 FROM tenants WHERE id = ?", (t["id"],)).fetchone():
            tcols_common = [c for c in cols(src, "tenants") if c in cols(tgt, "tenants")]
            vals = {c: t[c] for c in tcols_common}
            ph = ",".join("?" for _ in vals)
            tgt.execute(f"INSERT INTO tenants ({','.join(vals)}) VALUES ({ph})", list(vals.values()))
            report["tenants_created"] += 1
    for k in src.execute("SELECT * FROM tenant_api_keys"):
        if k["tenant_id"] in SKIP_TENANTS:
            continue
        existing = tgt.execute(
            "SELECT tenant_id FROM tenant_api_keys WHERE api_key = ?", (k["api_key"],)).fetchone()
        if existing:
            if existing["tenant_id"] != k["tenant_id"]:
                print(f"⚠️  key {k['api_key'][:12]}… maps to {existing['tenant_id']} on target "
                      f"but {k['tenant_id']} on source — keeping target mapping")
            continue
        tgt.execute(
            "INSERT INTO tenant_api_keys (api_key, tenant_id, label, is_active) VALUES (?,?,?,?)",
            (k["api_key"], k["tenant_id"], k["label"] if "label" in k.keys() else None, 1))
        report["keys_upserted"] += 1

    # ---- 3. storage_objects -------------------------------------------------
    ocols_common = [c for c in cols(src, "storage_objects") if c in cols(tgt, "storage_objects")]
    src_only = [c for c in cols(src, "storage_objects") if c not in ocols_common]
    if src_only:
        print(f"ℹ️  source-only columns dropped in merge: {src_only}")
    for o in src.execute("SELECT * FROM storage_objects"):
        if o["tenant_id"] in SKIP_TENANTS:
            report["objects_skipped_oneal"] += 1
            continue
        existing = tgt.execute(
            "SELECT checksum FROM storage_objects WHERE id = ?", (o["id"],)).fetchone()
        if existing:
            if (existing["checksum"] or "") == (o["checksum"] or ""):
                report["objects_skipped_dup"] += 1
                continue
            print(f"❌ CONFLICT: id {o['id']} exists on target with different checksum — ABORT")
            report["conflicts"] += 1
            tgt.rollback()
            return 2
        vals = {c: o[c] for c in ocols_common}
        vals["owner_user_id"] = user_map.get(o["owner_user_id"], o["owner_user_id"])
        # object_key is UNIQUE and doubles as the on-disk filename. A clash means
        # the same asset already lives on the target under a DIFFERENT id
        # (verified 2026-07-21: all 315 clashes — 313 koralmbahn + 2 tts — are
        # same-size, md5-sampled identical; the VPS db just has empty checksum
        # fields). We must keep BOTH ids resolvable (the VPS id may be baked
        # into consumer URLs), so: rename the key for the incoming row and,
        # for copy-mode objects, duplicate the file under the new name.
        # Genuinely different content -> abort.
        if vals.get("object_key"):
            other = tgt.execute(
                "SELECT id, checksum, file_size_bytes FROM storage_objects WHERE object_key = ?",
                (vals["object_key"],)).fetchone()
            if other:
                same_size = (other["file_size_bytes"] or 0) == (o["file_size_bytes"] or 0)
                cks_compatible = (not o["checksum"] or not other["checksum"]
                                  or o["checksum"] == other["checksum"])
                if not (same_size and cks_compatible):
                    print(f"❌ CONFLICT: object_key {vals['object_key']} exists with different "
                          f"content (target id {other['id']}) — ABORT")
                    report["conflicts"] += 1
                    tgt.rollback()
                    return 2
                old_key = vals["object_key"]
                stem, dot, ext = old_key.rpartition(".")
                new_key = f"{stem}_vpsmig.{ext}" if dot else f"{old_key}_vpsmig"
                vals["object_key"] = new_key
                if args.media_root and (o["storage_mode"] or "copy") == "copy":
                    import shutil
                    from pathlib import Path
                    tdir = Path(args.media_root) / (o["tenant_id"] or "arkturian")
                    src_f, dst_f = tdir / old_key, tdir / new_key
                    if args.commit:
                        if src_f.exists() and not dst_f.exists():
                            shutil.copy2(src_f, dst_f)
                        elif not src_f.exists():
                            print(f"⚠️  vps:{o['id']}: source file missing for alias copy: {src_f}")
                report["objects_aliased"] += 1
        ph = ",".join("?" for _ in vals)
        tgt.execute(
            f"INSERT INTO storage_objects ({','.join(vals)}) VALUES ({ph})", list(vals.values()))
        report["objects_inserted"] += 1

    # ---- report -------------------------------------------------------------
    print("\n=== MERGE REPORT ===")
    for k, v in report.items():
        print(f"  {k}: {v}")
    nmax = tgt.execute("SELECT MAX(id) FROM storage_objects").fetchone()[0]
    print(f"  target MAX(id) after merge: {nmax} (next new id: {nmax + 1})")

    if args.commit:
        tgt.commit()
        print("COMMITTED.")
    else:
        tgt.rollback()
        print("DRY-RUN — rolled back (use --commit to write).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
