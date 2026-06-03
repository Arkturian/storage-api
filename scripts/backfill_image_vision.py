#!/usr/bin/env python3
"""Backfill AI vision (title/subtitle/tags + safety) for title-less images.

Re-runs the analyze-async pipeline over images that have no ai_title yet. Uses
the public HTTP API (same path the UI triggers), so it goes through the live
VISION_BACKEND=claude + api-ai localize-to-/tmp contract.

Scope is explicit — pick ONE:
  --collection <id>     all title-less images in that collection
  --ids 100-200,305     an explicit id list/range
  --all                 every title-less image (LARGE — ~11.8k as of 2026-06)

Throttled: --concurrency N analyses run at once (default 3) so we don't swamp
claude / api-ai. Each analysis takes ~30-60s, so plan accordingly.

Idempotent-ish: by default skips objects that ALREADY have an ai_title (so a
re-run only fills the gaps). Use --force to re-analyze regardless.

Examples:
  ./venv/bin/python scripts/backfill_image_vision.py \
      --base http://127.0.0.1:8080 --key "$MASTER_KEY" \
      --collection Tscheppaschlucht --concurrency 3

  ./venv/bin/python scripts/backfill_image_vision.py \
      --base http://127.0.0.1:8080 --key "$MASTER_KEY" \
      --ids 36319,36400-36410 --dry-run
"""
import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx


def parse_ids(spec: str):
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    seen, uniq = set(), []
    for i in out:
        if i not in seen:
            seen.add(i)
            uniq.append(i)
    return uniq


def list_collection_image_ids(client, base, key, collection_id, page_size=500):
    """Page through /storage/list filtering image mime + collection."""
    ids, offset = [], 0
    while True:
        r = client.get(
            f"{base}/storage/list",
            params={"collection_id": collection_id, "limit": page_size, "offset": offset},
            headers={"X-API-KEY": key}, timeout=60,
        )
        r.raise_for_status()
        items = r.json().get("objects") or r.json().get("items") or []
        if not items:
            break
        for o in items:
            if str(o.get("mime_type", "")).startswith("image/"):
                ids.append(o["id"])
        if len(items) < page_size:
            break
        offset += page_size
    return ids


def get_object(client, base, key, oid):
    r = client.get(f"{base}/storage/objects/{oid}", headers={"X-API-KEY": key}, timeout=30)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def analyze_one(client, base, key, oid, mode, poll_timeout):
    """Trigger analyze-async and poll the task to completion. Returns (oid, ok, title)."""
    r = client.post(
        f"{base}/storage/analyze-async/{oid}",
        params={"mode": mode, "ai_tasks": "safety,vision,embedding,kg"},
        headers={"X-API-KEY": key}, timeout=60,
    )
    r.raise_for_status()
    task_id = r.json().get("task_id")
    deadline = time.time() + poll_timeout
    while time.time() < deadline:
        t = client.get(f"{base}/storage/tasks/{task_id}", headers={"X-API-KEY": key}, timeout=30)
        st = t.json().get("status")
        if st in ("completed", "failed"):
            break
        time.sleep(4)
    obj = get_object(client, base, key, oid)
    title = (obj or {}).get("ai_title")
    return oid, bool(title), title


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="http://127.0.0.1:8080", help="storage-api base URL")
    ap.add_argument("--key", required=True, help="X-API-KEY (master key)")
    scope = ap.add_mutually_exclusive_group(required=True)
    scope.add_argument("--collection", help="collection_id to backfill")
    scope.add_argument("--ids", help="explicit ids, e.g. 36319,36400-36410")
    scope.add_argument("--all", action="store_true", help="ALL title-less images (large)")
    ap.add_argument("--mode", default="quality", choices=["fast", "quality"])
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--poll-timeout", type=int, default=180, help="seconds to wait per analysis")
    ap.add_argument("--limit", type=int, default=0, help="cap number of objects (0 = no cap)")
    ap.add_argument("--force", action="store_true", help="re-analyze even if ai_title already set")
    ap.add_argument("--dry-run", action="store_true", help="list the targets, do nothing")
    args = ap.parse_args()

    with httpx.Client() as client:
        # Resolve the candidate id set
        if args.ids:
            candidates = parse_ids(args.ids)
        elif args.collection:
            candidates = list_collection_image_ids(client, args.base, args.key, args.collection)
        else:  # --all
            print("ERROR: --all needs DB access; run with --collection/--ids per batch instead.",
                  file=sys.stderr)
            sys.exit(2)

        # Filter to title-less images (unless --force)
        targets = []
        for oid in candidates:
            o = get_object(client, args.base, args.key, oid)
            if not o:
                continue
            if not str(o.get("mime_type", "")).startswith("image/"):
                continue
            if not args.force and (o.get("ai_title") or "").strip():
                continue
            targets.append(oid)
            if args.limit and len(targets) >= args.limit:
                break

        print(f"Scope: {len(targets)} title-less image(s) to analyze "
              f"(concurrency={args.concurrency}, mode={args.mode}).")
        if args.dry_run:
            print("DRY-RUN ids:", ",".join(map(str, targets)))
            return
        if not targets:
            print("Nothing to do.")
            return

        ok = fail = 0
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futs = {pool.submit(analyze_one, client, args.base, args.key, oid,
                                args.mode, args.poll_timeout): oid for oid in targets}
            for i, fut in enumerate(as_completed(futs), 1):
                oid = futs[fut]
                try:
                    _oid, good, title = fut.result()
                    if good:
                        ok += 1
                        print(f"[{i}/{len(targets)}] #{_oid} ✓ {title!r}")
                    else:
                        fail += 1
                        print(f"[{i}/{len(targets)}] #{_oid} ✗ (no title)")
                except Exception as e:  # noqa: BLE001
                    fail += 1
                    print(f"[{i}/{len(targets)}] #{oid} ERROR: {e}")

        print(f"\nDone. ok={ok} fail={fail} of {len(targets)}")


if __name__ == "__main__":
    main()
