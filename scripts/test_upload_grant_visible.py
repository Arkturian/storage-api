"""Upload/fetch responses say whether grant_scope was bound; the owner_email
combination that can never bind is refused before anything is stored.

Run against a scratch instance only (it uploads):
  BASE=http://127.0.0.1:8089 ADMIN_KEY=... PLAIN_KEY=... python scripts/test_upload_grant_visible.py
ADMIN_KEY: a key allowed to set owner_email. PLAIN_KEY: any other valid key.
"""
import os
import sys

import requests

B = os.environ["BASE"].rstrip("/")
ADMIN = {"X-API-KEY": os.environ["ADMIN_KEY"]}
PLAIN = {"X-API-KEY": os.environ["PLAIN_KEY"]}
failed = 0


def chk(name, cond, info=""):
    global failed
    failed += 0 if cond else 1
    print("PASS" if cond else "FAIL", name, info)


def up(headers, name, **data):
    data = {"is_public": "false", "ai_mode": "none", "reuse_existing": "false", **data}
    return requests.post(f"{B}/storage/upload", headers=headers,
                         files={"file": (name, os.urandom(48), "application/octet-stream")}, data=data)


count = lambda: requests.get(f"{B}/storage/list?limit=1", headers=ADMIN).json().get("total")

r = up(PLAIN, "a.bin", grant_scope="test-visible:a")
chk("bound scope is returned", r.status_code == 200 and r.json().get("grant_scope") == "test-visible:a", r.text[:80] if r.status_code != 200 else "")
r = up(PLAIN, "b.bin")
chk("no scope sent -> null", r.status_code == 200 and r.json().get("grant_scope") is None and "grant_scope" in r.json())
before = count()
r = up(ADMIN, "c.bin", grant_scope="test-visible:c", owner_email="someone-else@example.test")
chk("scope + foreign owner_email -> 422", r.status_code == 422 and r.json()["detail"].get("code") == "grant_scope_owner_mismatch", str(r.status_code))
chk("nothing stored on 422", count() == before, f"{before} -> {count()}")
r = up(ADMIN, "d.bin", owner_email="someone-else@example.test")
chk("owner_email without scope still works", r.status_code == 200 and r.json().get("grant_scope") is None, str(r.status_code))
r = up(PLAIN, "e.bin", grant_scope="not a scope")
chk("invalid scope still 422 invalid_scope", r.status_code == 422 and r.json()["detail"].get("code") == "invalid_scope")
r = requests.get(f"{B}/storage/objects/{up(PLAIN, 'f.bin', grant_scope='test-visible:f').json()['id']}", headers=PLAIN)
chk("plain GET does not claim a binding", r.status_code == 200 and r.json().get("grant_scope") is None)

print("ALL PASS" if not failed else f"{failed} FAILED")
sys.exit(1 if failed else 0)
