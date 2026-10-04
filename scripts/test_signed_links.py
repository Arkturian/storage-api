#!/usr/bin/env python3
"""Contract tests for POST /storage/sign + media grants (Post 5235).

Runs against an ISOLATED instance (own DB copy, own upload dir). Needs:
  --db        the instance's SQLite file (keys are read from it, never printed)
  --base      e.g. http://127.0.0.1:8096
  --objs      file with lines TAG|id|object_key|tenant for P (public),
              Q (private, owned by a principal), R (system-owned private),
              F (other tenant)
The script sets sign_tenants on one service key in THAT db copy.
"""
import argparse, sqlite3, sys, time
import httpx

FAIL = []
def check(c, label, detail=""):
    print(f"  [{'PASS' if c else 'FAIL'}] {label}{(' — ' + str(detail)) if detail else ''}")
    if not c: FAIL.append(label)

ap = argparse.ArgumentParser()
ap.add_argument("--db", required=True); ap.add_argument("--base", required=True); ap.add_argument("--objs", required=True)
a = ap.parse_args()
o = {l.split("|")[0]: int(l.split("|")[1]) for l in open(a.objs) if l.strip()}
P, Q, R, F = o["P"], o["Q"], o["R"], o["F"]
db = sqlite3.connect(a.db)
svc = db.execute("select api_key from tenant_api_keys where tenant_id='arkturian' and is_service=1 and is_active=1 order by rowid limit 1").fetchone()[0]
svc2 = db.execute("select api_key from tenant_api_keys where tenant_id='arkturian' and is_service=1 and is_active=1 and api_key<>? limit 1", (svc,)).fetchone()
plain = db.execute("select api_key from tenant_api_keys where tenant_id='arkturian' and coalesce(is_service,0)=0 and api_key<>'Inetpass1' and is_active=1 limit 1").fetchone()[0]
owner_q = db.execute("select u.email from storage_objects o join users u on u.id=o.owner_user_id where o.id=?", (Q,)).fetchone()[0]
other_email = db.execute("select email from users where id<>(select owner_user_id from storage_objects where id=?) and trust_level='user' limit 1", (Q,)).fetchone()[0]
db.execute("update tenant_api_keys set sign_tenants='arkturian' where api_key=?", (svc,)); db.commit()
B = a.base
S = httpx.Client(base_url=B, headers={"X-API-KEY": svc}, timeout=60)
ADMIN = httpx.Client(base_url=B, headers={"X-API-KEY": "Inetpass1"}, timeout=60)
PLAIN = httpx.Client(base_url=B, headers={"X-API-KEY": plain}, timeout=60)
anon = httpx.Client(base_url=B, timeout=60)
scope = "content-post:5226"
def sign(client, ids, **kw):
    return client.post("/storage/sign", json={"tenant": kw.get("tenant", "arkturian"), "scope": kw.get("scope", scope), "ids": ids, "ttl": kw.get("ttl", 600)}, headers=kw.get("headers"))

print("== who may sign")
check(sign(PLAIN, [P]).status_code == 403, "plain tenant key -> 403")
if svc2: check(httpx.post(f"{B}/storage/sign", headers={"X-API-KEY": svc2[0]}, json={"tenant": "arkturian", "scope": scope, "ids": [P], "ttl": 60}).status_code == 403, "service key without sign_tenants -> 403")
check(sign(S, [P], tenant="oneal").status_code == 403, "service key, tenant not enabled -> 403")
check(sign(S, [P], headers={"X-On-Behalf-Of": owner_q}).status_code == 400, "X-On-Behalf-Of on /sign -> 400")
check(anon.post("/storage/sign", json={"tenant": "arkturian", "scope": scope, "ids": [P]}).status_code == 401, "no key -> 401")
check(sign(S, [P], ttl=901).status_code == 422, "ttl 901 -> 422")
check(sign(S, [P], scope="bad scope").status_code == 422, "invalid scope -> 422")
check(sign(S, list(range(1, 502))).status_code == 422, "501 ids -> 422")

print("== per-id answers before any grant")
r = sign(S, [P, Q, R, F, 999999999]); d = r.json(); it = {i["id"]: i for i in d["items"]}
check(r.status_code == 200 and "expires_at" in d, "200 with expires_at", r.status_code)
check(it[P].get("public") is True and it[P].get("signed") is False and "?" not in it[P]["url"], "public object -> plain url")
check(it[Q].get("error") == "not_granted", "private, no grant -> not_granted", it[Q])
check(it[R].get("error") == "not_granted", "system-owned private, no grant -> not_granted")
check(it[F].get("error") == "wrong_tenant", "other tenant -> wrong_tenant", it[F])
check(it[999999999].get("error") == "not_found", "unknown id -> not_found")

print("== who may bind")
check(PLAIN.post(f"/storage/objects/{Q}/grants", json={"scope": scope}).status_code == 404, "plain tenant key cannot bind -> 404")
check(S.post(f"/storage/objects/{Q}/grants", json={"scope": scope}).status_code == 404, "service key without on-behalf cannot bind -> 404")
r = S.post(f"/storage/objects/{Q}/grants", json={"scope": scope}, headers={"X-On-Behalf-Of": other_email})
check(r.status_code in (403, 404), "on behalf of a NON-owner -> 403/404", r.status_code)
r = S.post(f"/storage/objects/{Q}/grants", json={"scope": scope}, headers={"X-On-Behalf-Of": owner_q})
check(r.status_code == 200 and r.json()["created"] is True, "on behalf of the owner -> created", r.text[:120])
r = S.post(f"/storage/objects/{Q}/grants", json={"scope": scope}, headers={"X-On-Behalf-Of": owner_q})
check(r.json().get("created") is False, "same grant again -> idempotent")
check(S.post(f"/storage/objects/{R}/grants", json={"scope": scope}, headers={"X-On-Behalf-Of": owner_q}).status_code == 404, "owner of Q cannot bind R")
r = ADMIN.post(f"/storage/objects/{R}/grants", json={"scope": scope})
check(r.status_code == 200 and r.json()["created"], "real admin binds system-owned R")
check(ADMIN.post(f"/storage/objects/{R}/grants", json={"scope": "bad"}).status_code == 422, "invalid scope on bind -> 422")
check(ADMIN.post(f"/storage/objects/{Q}/grants", json={"scope": "content-post:1"}).status_code == 404, "default master cannot bind a private_media object")
g = S.get(f"/storage/objects/{Q}/grants", headers={"X-On-Behalf-Of": owner_q}).json()
check(len(g["grants"]) == 1 and g["grants"][0]["granted_via"].startswith("service:"), "grant list shows audit trail")

print("== signed urls work, and only for what they say")
d = sign(S, [Q, R]).json(); it = {i["id"]: i for i in d["items"]}
check(it[Q].get("signed") is True and it[R].get("signed") is True, "Q and R signed")
uq = it[Q]["url"].replace(B, ""); ur = it[R]["url"].replace(B, "")
rq = anon.get(uq)
check(rq.status_code == 200 and rq.headers.get("content-type", "").startswith("image/"), "anon GET signed Q (private_media) -> 200", rq.status_code)
check("no-store" in rq.headers.get("cache-control", ""), "signed response is no-store", rq.headers.get("cache-control"))
rw = anon.get(uq + "&width=200&format=jpg")
check(rw.status_code == 200 and rw.headers.get("content-type") == "image/jpeg", "free params width/format still work", rw.status_code)
check(anon.get(ur).status_code == 200, "anon GET signed R -> 200")
check(anon.get(f"/storage/media/{Q}").status_code == 403, "Q without signature still 403")
tampered = uq[:-1] + ("0" if uq[-1] != "0" else "1")
check(anon.get(tampered).status_code == 403, "tampered sig -> 403")
check(anon.get(uq.replace(f"/media/{Q}", f"/media/{R}")).status_code == 403, "Q's signature on R -> 403")
check(anon.get(f"/storage/media/{Q}?exp=9999999999&kid=t1&sig=" + "0" * 64).status_code == 403, "forged far-future exp -> 403")
check(anon.head(uq).status_code == 403, "HEAD with signature stays 403 (GET only)")
short = {i["id"]: i for i in sign(S, [Q], ttl=1).json()["items"]}[Q]["url"].replace(B, "")
time.sleep(2.2)
check(anon.get(short).status_code == 403, "expired signature -> 403")

print("== safety verdict still applies to signed links")
db.execute("update storage_objects set ai_safety_rating='unsafe', ai_danger_potential=9 where id=?", (R,)); db.commit()
check(anon.get(ur).status_code == 451, "unsafe object, valid signature -> 451")
db.execute("update storage_objects set ai_safety_rating=NULL, ai_danger_potential=NULL where id=?", (R,)); db.commit()

print("== unbinding")
check(S.delete(f"/storage/objects/{Q}/grants", params={"scope": scope}, headers={"X-On-Behalf-Of": owner_q}).json().get("deleted") is True, "owner removes grant")
check({i["id"]: i for i in sign(S, [Q]).json()["items"]}[Q].get("error") == "not_granted", "after unbind -> not_granted")

print(f"\n{len(FAIL)} failure(s)" + (": " + ", ".join(FAIL) if FAIL else ""))
sys.exit(1 if FAIL else 0)
