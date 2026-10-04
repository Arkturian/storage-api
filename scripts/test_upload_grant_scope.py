#!/usr/bin/env python3
"""grant_scope on upload (Post 5250, variant c). Isolated instance only.

    python scripts/test_upload_grant_scope.py --db <copy.sqlite> --base http://127.0.0.1:PORT
Sets sign_tenants on one service key IN THAT COPY; keys are never printed.
"""
import argparse, sqlite3, sys, io, secrets
import httpx

FAIL = []
def check(c, label, detail=""):
    print(f"  [{'PASS' if c else 'FAIL'}] {label}{(' — ' + str(detail)) if detail else ''}")
    if not c: FAIL.append(label)

ap = argparse.ArgumentParser(); ap.add_argument("--db", required=True); ap.add_argument("--base", required=True)
a = ap.parse_args(); B = a.base
db = sqlite3.connect(a.db)
svc = db.execute("select api_key from tenant_api_keys where tenant_id='arkturian' and is_service=1 and is_active=1 order by rowid limit 1").fetchone()[0]
plain = db.execute("select api_key from tenant_api_keys where tenant_id='arkturian' and coalesce(is_service,0)=0 and api_key<>'Inetpass1' and is_active=1 limit 1").fetchone()[0]
db.execute("update tenant_api_keys set sign_tenants='arkturian' where api_key=?", (svc,)); db.commit()
P = httpx.Client(base_url=B, headers={"X-API-KEY": plain}, timeout=60)
S = httpx.Client(base_url=B, headers={"X-API-KEY": svc}, timeout=60)
anon = httpx.Client(base_url=B, timeout=60)
def png(): 
    from PIL import Image
    b = io.BytesIO(); Image.new("RGB", (8, 8), (secrets.randbelow(255), 10, 20)).save(b, "PNG"); return b.getvalue()
def count(): return db.execute("select count(*) from storage_objects").fetchone()[0]
def grants(oid): return [r[0] for r in db.execute("select scope from media_grants where object_id=?", (oid,))]
def sign(oid, scope): return S.post("/storage/sign", json={"tenant": "arkturian", "scope": scope, "ids": [oid], "ttl": 60}).json()["items"][0]

print("== /upload")
n0 = count()
r = P.post("/storage/upload", files={"file": ("x.png", png(), "image/png")}, data={"grant_scope": "bad scope", "ai_mode": "none", "is_public": "false"})
check(r.status_code == 422 and count() == n0, "invalid scope -> 422, nothing stored", r.status_code)
r = P.post("/storage/upload", files={"file": (f"gs-{secrets.token_hex(3)}.png", png(), "image/png")},
           data={"grant_scope": "cloud-session:TestAgent", "ai_mode": "none", "is_public": "false", "private": "true", "reuse_existing": "false"})
oid = r.json().get("id"); check(r.status_code == 200 and oid, "private upload with scope -> 200", r.status_code)
check(grants(oid) == ["cloud-session:TestAgent"], "grant created atomically", grants(oid))
check(db.execute("select granted_via from media_grants where object_id=?", (oid,)).fetchone()[0] == "upload", "granted_via=upload")
it = sign(oid, "cloud-session:TestAgent"); check(it.get("signed") is True, "signable in that scope")
check(sign(oid, "cloud-session:Other").get("error") == "not_granted", "NOT signable in another scope")
u = it["url"].replace(B, ""); rr = anon.get(u)
check(rr.status_code == 200 and "no-store" in rr.headers.get("cache-control", ""), "signed GET 200, no-store", rr.status_code)
check(anon.get(f"/storage/media/{oid}").status_code == 403, "unsigned GET still 403")
r = P.post("/storage/upload", files={"file": ("y.png", png(), "image/png")}, data={"ai_mode": "none", "reuse_existing": "false"})
check(r.status_code == 200 and grants(r.json()["id"]) == [], "upload without scope -> no grant")

print("== /upload-ticket")
n0 = count()
check(P.post("/storage/upload-ticket", params={"filename": "t.png", "grant_scope": "x"}).status_code == 422 and count() == n0, "ticket: invalid scope -> 422")
t = P.post("/storage/upload-ticket", params={"filename": f"t-{secrets.token_hex(3)}.png", "grant_scope": "cloud-session:TicketAgent", "private": "true"}).json()
r = httpx.put(t["upload_url"], content=png(), timeout=60)
tid = r.json().get("id"); check(r.status_code == 200 and grants(tid) == ["cloud-session:TicketAgent"], "ticket upload binds", grants(tid) if tid else r.text[:100])

print("== /fetch")
r = P.post("/storage/fetch", json={"url": f"{B}/storage/media/129220", "grant_scope": "bad scope"})
check(r.status_code == 422, "fetch: invalid scope -> 422", r.status_code)
r = P.post("/storage/fetch", json={"url": f"{B}/storage/media/129220", "grant_scope": "cloud-session:FetchAgent", "filename": f"f-{secrets.token_hex(3)}.png"})
fid = r.json().get("id") if r.status_code == 200 else None
check(fid and grants(fid) == ["cloud-session:FetchAgent"], "fetch binds", r.status_code)

print(f"\n{len(FAIL)} failure(s)" + (": " + ", ".join(FAIL) if FAIL else ""))
sys.exit(1 if FAIL else 0)
