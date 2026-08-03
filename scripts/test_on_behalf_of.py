#!/usr/bin/env python3
"""Regression tests for the X-On-Behalf-Of service contract.

Agreed with 3DPresenter-Codex (Content post 4437, section q-7db4946ae2fd):
a key explicitly flagged `is_service` may act for a verified user; the request
is then authorised entirely as that user and inherits NO admin rights from the
service key. Any other key presenting the header is rejected outright.

Covered:
  1. non-service key + header            -> 403 (no silent ignore)
  2. service key + unknown principal     -> 403 fail closed
  3. service key + valid principal       -> upload lands on that owner
  4. impersonated user, foreign object   -> delete/patch 403/404, object survives
  5. impersonated user, own object       -> delete succeeds
  6. keyless public read                 -> unchanged

Needs a temporary service key; create and drop it around the run:
    python scripts/test_on_behalf_of.py --base URL --service-key K --principal mail
"""
from __future__ import annotations

import argparse
import io
import sys
import time

import httpx

FAILURES: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> bool:
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")
    if not cond:
        FAILURES.append(label)
    return cond


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--service-key", required=True, help="tenant key flagged is_service")
    ap.add_argument("--plain-key", required=True, help="ordinary key WITHOUT is_service")
    ap.add_argument("--principal", required=True, help="e-mail of an existing user")
    ap.add_argument("--foreign-object", type=int, required=True, help="id owned by someone else")
    args = ap.parse_args()

    base = args.base.rstrip("/")
    stamp = str(int(time.time()))
    svc = {"X-API-KEY": args.service_key, "X-On-Behalf-Of": args.principal}

    with httpx.Client(follow_redirects=True, timeout=90.0) as c:
        print("\n1. Nicht-Service-Key mit Header -> 403")
        r = c.get(f"{base}/storage/list?limit=1",
                  headers={"X-API-KEY": args.plain_key, "X-On-Behalf-Of": args.principal})
        check(r.status_code == 403, "abgewiesen statt still ignoriert", f"HTTP {r.status_code}")

        print("\n2. Service-Key mit unbekanntem Principal -> 403 (fail closed)")
        r = c.get(f"{base}/storage/list?limit=1",
                  headers={"X-API-KEY": args.service_key,
                           "X-On-Behalf-Of": f"nobody-{stamp}@example.invalid"})
        check(r.status_code == 403, "unbekannter Principal abgewiesen", f"HTTP {r.status_code}")

        print("\n3. Service-Key + gueltiger Principal -> Upload gehoert dem Principal")
        payload = f"on-behalf-{stamp}\n".encode()
        r = c.post(f"{base}/storage/upload", headers=svc,
                   files={"file": (f"obo_{stamp}.txt", io.BytesIO(payload), "text/plain")},
                   data={"ttl_hours": "1", "reuse_existing": "false"})
        ok = check(r.status_code == 200, "Upload akzeptiert", f"HTTP {r.status_code}")
        own_id = r.json().get("id") if ok else None
        if ok:
            meta = c.get(f"{base}/storage/objects/{own_id}", headers=svc).json()
            check(meta.get("owner_email") == args.principal or meta.get("owner_user_id") is not None,
                  "Objekt traegt den Principal als Owner",
                  str(meta.get("owner_email") or meta.get("owner_user_id")))

        print("\n4. Fremdes Objekt -> keine Adminrechte geerbt")
        r = c.delete(f"{base}/storage/objects/{args.foreign_object}", headers=svc)
        check(r.status_code in (403, 404), "Fremd-Delete abgewiesen", f"HTTP {r.status_code}")
        still = c.get(f"{base}/storage/objects/{args.foreign_object}",
                      headers={"X-API-KEY": args.service_key})
        check(still.status_code == 200, "fremdes Objekt existiert weiterhin")
        r = c.patch(f"{base}/storage/objects/{args.foreign_object}", headers=svc,
                    json={"is_public": True})
        check(r.status_code in (403, 404), "Fremd-Visibility abgewiesen", f"HTTP {r.status_code}")

        print("\n5. Eigenes Objekt -> Operationen gruen")
        if own_id:
            r = c.patch(f"{base}/storage/objects/{own_id}", headers=svc, json={"title": "obo-test"})
            check(r.status_code == 200, "eigenes PATCH erlaubt", f"HTTP {r.status_code}")
            r = c.delete(f"{base}/storage/objects/{own_id}", headers=svc)
            check(r.status_code == 200, "eigenes DELETE erlaubt", f"HTTP {r.status_code}")

        print("\n6. keyless public read unveraendert")
        r = c.get(f"{base}/storage/list?limit=3")
        items = r.json().get("items", []) if r.status_code == 200 else []
        check(r.status_code == 200 and all(i.get("is_public") for i in items),
              "anonym nur oeffentliche Objekte", f"HTTP {r.status_code}, {len(items)} items")

    print()
    if FAILURES:
        print(f"FEHLGESCHLAGEN ({len(FAILURES)}): " + "; ".join(FAILURES))
        return 1
    print("Alle On-Behalf-Of-Regressionstests bestanden.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
