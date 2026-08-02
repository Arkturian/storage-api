#!/usr/bin/env python3
"""Regression tests for upload immutability (Issue #772, see also #512).

/storage/media/{id} is referenced by manifests, SHA-pinned URLs and published
links, so a given id must always serve the same bytes. The filename dedup
(`reuse_existing`, default true) used to overwrite the stored content of an
existing object, which silently changed what a published id delivered and
destroyed the previous bytes.

Covered here:
  1. same filename + same content    -> replay: SAME id, content untouched
  2. same filename + different content -> NEW id, original id still intact
  3. parallel uploads of distinct content -> no id collision, no lost upload

Run against a live instance:
    python scripts/test_upload_immutability.py \
        --base https://api-storage.arkserver.arkturian.com --key "$API_KEY"

Exit code 0 = all passed. Uploads use ttl_hours=1 so the TTL cron reaps them.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

TTL_HOURS = "1"
FAILURES: list[str] = []


def check(condition: bool, label: str, detail: str = "") -> bool:
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {label}{(' — ' + detail) if detail else ''}")
    if not condition:
        FAILURES.append(label)
    return condition


def upload(client: httpx.Client, base: str, key: str, filename: str, payload: bytes) -> dict:
    resp = client.post(
        f"{base}/storage/upload",
        headers={"X-API-KEY": key},
        files={"file": (filename, io.BytesIO(payload), "text/plain")},
        data={"ttl_hours": TTL_HOURS, "context": "immutability-regression"},
        timeout=120.0,
    )
    resp.raise_for_status()
    return resp.json()


def fetch(client: httpx.Client, base: str, object_id: int) -> bytes:
    resp = client.get(f"{base}/storage/media/{object_id}", timeout=60.0)
    resp.raise_for_status()
    return resp.content


def test_replay_on_identical_content(client, base, key, stamp) -> None:
    print("\n1. gleicher Dateiname + gleicher Inhalt -> Replay auf dieselbe ID")
    name = f"immut_replay_{stamp}.txt"
    payload = b"content-A\n"

    first = upload(client, base, key, name, payload)
    second = upload(client, base, key, name, payload)

    check(first["id"] == second["id"], "gleiche ID zurueckgegeben",
          f"{first['id']} vs {second['id']}")
    check(fetch(client, base, first["id"]) == payload, "Inhalt unveraendert")
    check(first.get("checksum") == hashlib.sha256(payload).hexdigest(),
          "checksum entspricht dem Inhalt")


def test_new_id_on_different_content(client, base, key, stamp) -> None:
    print("\n2. gleicher Dateiname + anderer Inhalt -> neue ID, Original intakt")
    name = f"immut_divergent_{stamp}.txt"
    original = b"content-A\n"
    divergent = b"content-B-different\n"

    first = upload(client, base, key, name, original)
    second = upload(client, base, key, name, divergent)

    check(first["id"] != second["id"], "neue ID fuer abweichenden Inhalt",
          f"{first['id']} -> {second['id']}")
    check(fetch(client, base, first["id"]) == original,
          "ORIGINAL-ID liefert weiterhin die alten Bytes")
    check(fetch(client, base, second["id"]) == divergent,
          "neue ID liefert den neuen Inhalt")


def test_parallel_uploads(client, base, key, stamp) -> None:
    print("\n3. parallele Uploads mit unterschiedlichem Inhalt -> nichts geht verloren")
    payloads = [f"parallel-{i}-{stamp}\n".encode() for i in range(4)]

    def do(i: int) -> dict:
        # Distinct filenames — this mirrors how generators must behave after
        # Issue #512 (a second-granular timestamp is NOT unique enough).
        return upload(client, base, key, f"immut_par_{stamp}_{i}.txt", payloads[i])

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(do, range(4)))

    ids = [r["id"] for r in results]
    check(len(set(ids)) == len(ids), "alle IDs verschieden", str(ids))
    served = [fetch(client, base, r["id"]) for r in results]
    check(served == payloads, "jede ID liefert genau ihren eigenen Inhalt")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True, help="e.g. https://api-storage.arkserver.arkturian.com")
    parser.add_argument("--key", required=True, help="X-API-KEY value")
    args = parser.parse_args()

    base = args.base.rstrip("/")
    stamp = str(int(time.time()))
    print(f"Upload-Immutabilitaet gegen {base} (stamp {stamp})")

    with httpx.Client(follow_redirects=True) as client:
        test_replay_on_identical_content(client, base, args.key, stamp)
        test_new_id_on_different_content(client, base, args.key, stamp)
        test_parallel_uploads(client, base, args.key, stamp)

    print()
    if FAILURES:
        print(f"FEHLGESCHLAGEN ({len(FAILURES)}): " + "; ".join(FAILURES))
        return 1
    print("Alle Immutabilitaets-Regressionstests bestanden.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
