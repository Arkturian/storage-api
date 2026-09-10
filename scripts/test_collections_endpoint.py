#!/usr/bin/env python3
"""Contract tests for GET /storage/collections (post 4961, XCodeFieldshare).

The endpoint is a GROUP BY over the caller's /storage/list view. Everything
below therefore checks /collections AGAINST /list on the same instance — the
list endpoint is the oracle, not a fixture.

Covered (anonymous, always):
  1. shape          items/total/limit/offset, every item has name + item_count > 0
  2. completeness   sum(item_count) over all pages == /list?mine=false total
  3. group count    total == number of distinct groups actually paged
  4. per-collection item_count == /list?collection_id=X total (5 samples)
  5. pagination     total constant across pages, pages disjoint
  6. sort           name asc / item_count desc monotonic
  7. search/prefix  every returned id matches, prefix keeps sub-collections
  8. uncategorized  id=null present by default, gone with include_uncategorized=false
  9. preview        preview.id belongs to that collection and is visible
 10. validation     bad sort / order / limit -> 422
 12. /list?uncategorized=true == null bucket item_count; 422 with collection_id/like/link_id
Keyed (with --key): 11. per-collection item_count == /list?collection_id=X with the same
                        key. No sum check there: /list without collection_id narrows to
                        the owner (unless admin + mine=false), /list WITH collection_id is
                        tenant-wide — and /collections mirrors the latter by design.

    python scripts/test_collections_endpoint.py --base http://127.0.0.1:8097 [--key K]
"""
from __future__ import annotations

import argparse
import sys

import httpx

FAILURES: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> bool:
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")
    if not cond:
        FAILURES.append(label)
    return cond


def page_all(c: httpx.Client, params: dict) -> tuple[list[dict], int]:
    items: list[dict] = []
    offset = 0
    total = -1
    while True:
        r = c.get("/storage/collections", params={**params, "limit": 1000, "offset": offset})
        r.raise_for_status()
        d = r.json()
        total = d["total"]
        items.extend(d["items"])
        if len(d["items"]) < 1000:
            return items, total
        offset += 1000


def list_total(c: httpx.Client, params: dict) -> int:
    r = c.get("/storage/list", params={**params, "limit": 1})
    r.raise_for_status()
    return r.json()["total"]


def run(c: httpx.Client, label: str, list_params: dict, sum_check: bool) -> None:
    print(f"\n== {label}")
    r = c.get("/storage/collections", params={"limit": 5})
    check(r.status_code == 200, "GET /collections 200", str(r.status_code))
    d = r.json()
    check(all(k in d for k in ("items", "total", "limit", "offset")), "response shape")
    check(all(i["name"] and i["item_count"] > 0 for i in d["items"]), "every item has name and item_count > 0")

    items, total = page_all(c, {})
    check(total == len(items), "total == number of groups paged", f"{total} vs {len(items)}")
    ids = [i["id"] for i in items]
    check(len(ids) == len(set(ids)), "group ids unique across pages")
    if sum_check:
        lt = list_total(c, list_params)
        ssum = sum(i["item_count"] for i in items)
        check(ssum == lt, "sum(item_count) == /list total", f"{ssum} vs {lt}")

    named = [i for i in items if i["id"]]
    for i in sorted(named, key=lambda x: -x["item_count"])[:3] + named[-2:]:
        n = list_total(c, {**list_params, "collection_id": i["id"]})
        check(n == i["item_count"], f"item_count matches /list for {i['id'][:40]}", f"{i['item_count']} vs {n}")
    null_bucket = next((i for i in items if i["id"] is None), None)
    if null_bucket:
        n = list_total(c, {**list_params, "uncategorized": "true"})
        check(n == null_bucket["item_count"], "uncategorized bucket matches /list?uncategorized=true", f"{null_bucket['item_count']} vs {n}")
        r = c.get("/storage/list", params={"uncategorized": "true", "limit": 3})
        check(all(not o.get("collection_id") for o in r.json()["items"]), "/list?uncategorized=true returns only objects without collection")
    for bad in ({"collection_id": "x"}, {"collection_like": "x"}, {"link_id": "1"}):
        r = c.get("/storage/list", params={"uncategorized": "true", **bad})
        check(r.status_code == 422, f"uncategorized + {list(bad)[0]} -> 422", str(r.status_code))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--key", default=None, help="API key for the keyed run (never printed)")
    a = ap.parse_args()

    anon = httpx.Client(base_url=a.base, timeout=60)
    run(anon, "anonymous", {"mine": "false"}, sum_check=True)

    print("\n== pagination / sort / filters (anonymous)")
    p1 = anon.get("/storage/collections", params={"limit": 10, "offset": 0, "sort": "name", "order": "asc"}).json()
    p2 = anon.get("/storage/collections", params={"limit": 10, "offset": 10, "sort": "name", "order": "asc"}).json()
    check(p1["total"] == p2["total"], "total constant across pages")
    check(not ({i["id"] for i in p1["items"]} & {i["id"] for i in p2["items"]}), "pages disjoint")
    names = [i["id"] or "" for i in p1["items"] + p2["items"]]
    check(names == sorted(names, key=str.lower), "sort=name asc monotonic (case-insensitive)")
    cnt = [i["item_count"] for i in anon.get("/storage/collections", params={"limit": 50, "sort": "item_count"}).json()["items"]]
    check(cnt == sorted(cnt, reverse=True), "sort=item_count default desc monotonic")

    s = anon.get("/storage/collections", params={"search": "udo", "limit": 1000}).json()
    check(s["total"] > 0 and all("udo" in (i["id"] or "").lower() for i in s["items"]), "search=udo: every id contains term", str(s["total"]))
    pf = anon.get("/storage/collections", params={"prefix": "UdoJuergensMp3/", "limit": 1000}).json()
    check(pf["total"] > 0 and all((i["id"] or "").lower().startswith("udojuergensmp3/") for i in pf["items"]), "prefix keeps only sub-collections", str(pf["total"]))
    esc = anon.get("/storage/collections", params={"search": "%", "limit": 5}).json()
    check(all("%" in (i["id"] or "") for i in esc["items"]), "search='%' is literal, not wildcard", str(esc["total"]))

    allc = anon.get("/storage/collections", params={"limit": 1000, "sort": "item_count"}).json()
    has_null = any(i["id"] is None and i["name"] == "Uncategorized" for i in allc["items"])
    check(has_null, "uncategorized bucket present (id=null)")
    nonull = anon.get("/storage/collections", params={"limit": 1000, "include_uncategorized": "false", "sort": "item_count"}).json()
    check(all(i["id"] is not None for i in nonull["items"]) and nonull["total"] == allc["total"] - 1, "include_uncategorized=false drops exactly that bucket")

    pv = anon.get("/storage/collections", params={"limit": 3, "sort": "item_count", "preview": "true", "include_uncategorized": "false"}).json()
    for i in pv["items"]:
        ok = bool(i["preview"]) and i["preview"]["id"] == i["latest_object_id"]
        if ok:
            o = anon.get(f"/storage/objects/{i['preview']['id']}")
            ok = o.status_code == 200 and (o.json().get("collection_id") or "") == i["id"] and o.json().get("is_public") is True
        check(ok, f"preview belongs to collection and is visible: {i['id'][:40]}")
    nopv = anon.get("/storage/collections", params={"limit": 2}).json()
    check(all(i["preview"] is None for i in nopv["items"]), "preview absent unless requested")

    for params in ({"sort": "bogus"}, {"order": "sideways"}, {"limit": 0}, {"limit": 5000}):
        r = anon.get("/storage/collections", params=params)
        check(r.status_code == 422, f"validation 422 for {params}", str(r.status_code))

    if a.key:
        keyed = httpx.Client(base_url=a.base, timeout=60, headers={"X-API-KEY": a.key})
        run(keyed, "keyed (tenant-wide, like /list?collection_id=)", {}, sum_check=False)
        r = keyed.get("/storage/collections", params={"mine": "true", "limit": 1})
        check(r.status_code == 200, "mine is not a parameter (ignored, not rejected)", str(r.status_code))

    print(f"\n{len(FAILURES)} failure(s)" + (": " + ", ".join(FAILURES) if FAILURES else ""))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
