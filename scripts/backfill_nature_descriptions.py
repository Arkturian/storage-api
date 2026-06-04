#!/usr/bin/env python3
"""Backfill title + species-aware description for nature-collection images.

Built for the Tscheppaschlucht (Kärnten) collection: for each title-less image
it asks claude (via api-ai, which localizes the storage URL to /tmp) to describe
the photo AND identify any animal/plant by species (German + scientific name),
then writes the parsed Title -> storage `title` and Description -> `description`.

Why a dedicated script instead of analyze-async: the production VISION prompt is
product/catalog-oriented and dilutes nature/species detail. This uses a focused
nature prompt that reliably yields species IDs (verified on Tscheppaschlucht).

Scope: --collection (default Tscheppaschlucht) OR --ids. Skips images that
already have a title unless --force. Throttled via --concurrency.

Example:
  ./venv/bin/python scripts/backfill_nature_descriptions.py \
      --key "$MASTER_KEY" --collection Tscheppaschlucht --concurrency 3
  ./venv/bin/python scripts/backfill_nature_descriptions.py \
      --key "$MASTER_KEY" --ids 36205 --force --dry-run
"""
import argparse
import re
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx

PROMPT_TMPL = (
    "Du beschreibst ein Foto aus dem Naturpark Tscheppaschlucht (Kärnten, Österreich). "
    "Falls ein TIER oder eine PFLANZE deutlich erkennbar ist, bestimme die Art so genau "
    "wie möglich — deutscher Name + wissenschaftlicher Name in Klammern. Erfinde keine Art "
    "wenn du unsicher bist. Antworte AUSSCHLIESSLICH in genau diesem Format, ohne Vorrede:\n"
    "Titel: <prägnant, max 6 Wörter; falls Tier/Pflanze sichtbar, die Art im Titel>\n"
    "Beschreibung: <1-2 Sätze; falls Tier/Pflanze sichtbar, mit Artbestimmung>\n"
    "Bild: {url}"
)


def parse_ids(spec):
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
    return list(dict.fromkeys(out))


def collection_titleless_image_ids(db_path, collection_id, force):
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cond = "" if force else "AND (ai_title IS NULL OR ai_title='') AND (title IS NULL OR title='')"
        rows = con.execute(
            f"SELECT id FROM storage_objects WHERE collection_id=? "
            f"AND mime_type LIKE 'image/%' {cond} ORDER BY id",
            (collection_id,),
        ).fetchall()
    finally:
        con.close()
    return [r[0] for r in rows]


def parse_response(text):
    """Pull 'Titel:' and 'Beschreibung:' out of claude's answer."""
    title, desc = None, None
    m = re.search(r"Titel:\s*(.+)", text)
    if m:
        title = m.group(1).strip().strip("*").strip()
    # description: everything after 'Beschreibung:' up to a 'Bild:' line or EOF
    m = re.search(r"Beschreibung:\s*(.+?)(?:\n\s*Bild:|\Z)", text, re.DOTALL)
    if m:
        desc = " ".join(m.group(1).split()).strip()
    return title, desc


def one(args, oid):
    url = f"{args.storage_base}/storage/media/{oid}?width={args.width}&format=jpg"
    prompt = PROMPT_TMPL.format(url=url)
    with httpx.Client() as client:
        r = client.post(f"{args.ai_base}/ai/claude",
                        json={"prompt": prompt},
                        headers={"X-API-KEY": args.key}, timeout=240)
        r.raise_for_status()
        resp = r.json().get("response") or r.json().get("message") or ""
        title, desc = parse_response(resp)
        if not title and not desc:
            return oid, False, "unparseable", resp[:80]
        if args.dry_run:
            return oid, True, title, desc
        patch = {}
        if title and not args.description_only:
            patch["title"] = title
        if desc:
            patch["description"] = desc
        p = client.patch(f"{args.storage_base}/storage/objects/{oid}",
                         json=patch, headers={"X-API-KEY": args.key}, timeout=30)
        p.raise_for_status()
        return oid, True, title, desc


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--key", required=True, help="X-API-KEY (master key)")
    ap.add_argument("--storage-base", default="http://127.0.0.1:8080")
    ap.add_argument("--ai-base", default="http://127.0.0.1:8000")
    ap.add_argument("--db", default="/var/lib/storage-api/storage.db")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--collection")
    g.add_argument("--ids")
    ap.add_argument("--width", type=int, default=700)
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--description-only", action="store_true",
                    help="only write description, keep existing title (for videos)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.ids:
        ids = parse_ids(args.ids)
    else:
        ids = collection_titleless_image_ids(args.db, args.collection, args.force)
    if args.limit:
        ids = ids[: args.limit]

    print(f"Targets: {len(ids)} image(s)  (concurrency={args.concurrency}, "
          f"width={args.width}, dry_run={args.dry_run})")
    if not ids:
        return

    ok = fail = 0
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futs = {pool.submit(one, args, oid): oid for oid in ids}
        for i, fut in enumerate(as_completed(futs), 1):
            oid = futs[fut]
            try:
                _oid, good, title, desc = fut.result()
                if good:
                    ok += 1
                    print(f"[{i}/{len(ids)}] #{_oid} ✓ {title}\n        {desc}")
                else:
                    fail += 1
                    print(f"[{i}/{len(ids)}] #{_oid} ✗ {title}: {desc}")
            except Exception as e:  # noqa: BLE001
                fail += 1
                print(f"[{i}/{len(ids)}] #{oid} ERROR: {e}")

    print(f"\nDone. ok={ok} fail={fail} of {len(ids)}")


if __name__ == "__main__":
    main()
