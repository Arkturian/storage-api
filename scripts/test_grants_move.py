#!/usr/bin/env python3
"""POST /storage/grants/move (Post 5250, agent rename). Isolated instance only.

    python scripts/test_grants_move.py --db <copy.sqlite> --base http://127.0.0.1:PORT
Seeds its own grants in THAT copy; keys are never printed.
"""
import argparse, sqlite3, sys
import httpx

FAIL = []
def check(c, label, detail=""):
    print(f"  [{'PASS' if c else 'FAIL'}] {label}{(' — ' + str(detail)) if detail else ''}")
    if not c: FAIL.append(label)

ap = argparse.ArgumentParser(); ap.add_argument("--db", required=True); ap.add_argument("--base", required=True)
a = ap.parse_args(); B = a.base
db = sqlite3.connect(a.db)
svc = db.execute("select api_key from tenant_api_keys where tenant_id='arkturian' and is_service=1 and is_active=1 order by rowid limit 1").fetchone()[0]
nosign = db.execute("select api_key from tenant_api_keys where tenant_id='arkturian' and is_service=1 and is_active=1 and api_key<>? limit 1", (svc,)).fetchone()
plain = db.execute("select api_key from tenant_api_keys where tenant_id='arkturian' and coalesce(is_service,0)=0 and api_key<>'Inetpass1' and is_active=1 limit 1").fetchone()[0]
db.execute("update tenant_api_keys set sign_tenants='arkturian' where api_key=?", (svc,))
ark = [r[0] for r in db.execute("select id from storage_objects where tenant_id='arkturian' and tombstoned_at is null order by id desc limit 3")]
one = db.execute("select id from storage_objects where tenant_id='oneal' and tombstoned_at is null order by id desc limit 1").fetchone()[0]
db.execute("delete from media_grants where scope like 'cloud-session%:mv%' or scope like 'cloud-session%:Neu%' or scope='content-post:mv1'")
for oid in ark: db.execute("insert into media_grants (object_id, scope, granted_by_user_id, granted_via, created_at) values (?,?,1,'test',datetime('now'))", (oid, "cloud-session:mvAlt"))
db.execute("insert into media_grants (object_id, scope, granted_by_user_id, granted_via, created_at) values (?,?,1,'test',datetime('now'))", (ark[0], "cloud-session:NeuName"))
db.execute("insert into media_grants (object_id, scope, granted_by_user_id, granted_via, created_at) values (?,?,1,'test',datetime('now'))", (one, "cloud-session:mvAlt"))
db.commit()
def scopes(oid): return sorted(r[0] for r in db.execute("select scope from media_grants where object_id=?", (oid,)))
def mv(key, f, t, tenant="arkturian", h=None):
    return httpx.post(f"{B}/storage/grants/move", headers={"X-API-KEY": key, **(h or {})}, json={"tenant": tenant, "from_scope": f, "to_scope": t}, timeout=60)

print("== gate")
check(mv(plain, "cloud-session:mvAlt", "cloud-session:NeuName").status_code == 403, "plain tenant key -> 403")
if nosign: check(mv(nosign[0], "cloud-session:mvAlt", "cloud-session:NeuName").status_code == 403, "service key without sign_tenants -> 403")
check(mv(svc, "cloud-session:mvAlt", "cloud-session:NeuName", tenant="oneal").status_code == 403, "tenant not enabled -> 403")
check(mv(svc, "cloud-session:mvAlt", "cloud-session:NeuName", h={"X-On-Behalf-Of": "apopovic.aut@gmail.com"}).status_code == 400, "X-On-Behalf-Of -> 400")
check(httpx.post(f"{B}/storage/grants/move", json={"tenant": "arkturian", "from_scope": "cloud-session:a", "to_scope": "cloud-session:b"}, timeout=60).status_code == 401, "no key -> 401")
print("== validation")
check(mv(svc, "cloud-session:mvAlt", "content-post:mv1").status_code == 422, "cross family (cloud -> content) -> 422")
check(mv(svc, "cloud-session:mvAlt", "cloud-session:mvAlt").status_code == 422, "same scope -> 422")
check(mv(svc, "cloud-session:mvAlt", "bad scope").status_code == 422, "invalid scope -> 422")
print("== move")
r = mv(svc, "cloud-session:mvAlt", "cloud-session:NeuName"); d = r.json()
check(r.status_code == 200 and d["moved"] == 2 and d["merged"] == 1, "2 moved, 1 merged (target already bound)", d)
check(all(scopes(o).count("cloud-session:NeuName") == 1 and "cloud-session:mvAlt" not in scopes(o) for o in ark), "every arkturian object now carries the new scope once")
check(scopes(one) == ["cloud-session:mvAlt"], "other tenant's grant untouched", scopes(one))
r = mv(svc, "cloud-session:NeuName", "cloud-session-x:4ac3b6"); d = r.json()
check(r.status_code == 200 and d["moved"] == 3, "cloud-session -> cloud-session-x (same family) allowed", d)
r = mv(svc, "cloud-session:nothing-here", "cloud-session:x2"); 
check(r.status_code == 200 and r.json()["moved"] == 0, "empty move is a no-op 200")
db.execute("delete from media_grants where scope in ('cloud-session-x:4ac3b6','cloud-session:mvAlt')"); db.commit()
print(f"\n{len(FAIL)} failure(s)" + (": " + ", ".join(FAIL) if FAIL else ""))
sys.exit(1 if FAIL else 0)
