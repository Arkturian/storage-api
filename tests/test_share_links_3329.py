"""#3329: share links with an unguessable token.

Public objects are reachable by counting ids. What goes out should stay
private and get a share link: random token, only its hash stored, short
signed redirect, revocable, expiring."""
import hashlib
import json
import time
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import main  # noqa: E402
import models  # noqa: E402
from database import get_db  # noqa: E402
from models import MediaGrant, ShareLink, StorageObject, TenantAPIKey, User  # noqa: E402
from storage import routes as storage_routes  # noqa: E402

SVC = "k-svc-test"        # is_service key (owner path via X-On-Behalf-Of)
CLOUD = "k-cloud-test"    # cloud-api: sign_tenants + may_create_share_links
NOFLAG = "k-noflag-test"
AGENT = "cloud-session:CloudV2-time"
DATA = b"%PDF-1.4 Lohnzettel\n" * 50


@pytest.fixture
def env(tmp_path, monkeypatch):
    from storage.service import generic_storage
    monkeypatch.setenv("STORAGE_SIGN_SECRETS", json.dumps({"k1": "x" * 40}))
    monkeypatch.setenv("STORAGE_SIGN_ACTIVE_KID", "k1")
    monkeypatch.setenv("STORAGE_SHARE_BASE_URL", "https://files.example.test")
    up = tmp_path / "up"
    media = up / "media" / "arkturian"
    media.mkdir(parents=True)
    for attr, sub in (("upload_root", ""), ("media_dir", "media"), ("thumbnails_dir", "thumbnails"), ("webview_dir", "webview")):
        p = up / sub if sub else up
        p.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(generic_storage, attr, p)

    models._hwm_ready.clear()
    eng = create_engine(f"sqlite:///{tmp_path/'s.db'}", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(eng)
    import database
    S = sessionmaker(bind=eng)
    monkeypatch.setattr(database, "SessionLocal", S)
    s = S()
    owner = User(email="alex@x", display_name="a", password_hash="", api_key="ko", trust_level="user", device_ids=[])
    other = User(email="fremd@x", display_name="f", password_hash="", api_key="kf", trust_level="user", device_ids=[])
    s.add_all([owner, other]); s.commit()
    s.add_all([TenantAPIKey(api_key=SVC, tenant_id="arkturian", label="svc", is_active=True, is_service=True),
               TenantAPIKey(api_key=CLOUD, tenant_id="arkturian", label="cloud", is_active=True,
                            sign_tenants="arkturian", may_create_share_links=True),
               TenantAPIKey(api_key=NOFLAG, tenant_id="arkturian", label="noflag", is_active=True,
                            sign_tenants="arkturian")])
    objs = {}
    for k, enc in (("doc", False), ("received", False), ("enc", False)):
        key = f"u{owner.id}_{k}.pdf"
        (media / key).write_bytes(DATA)
        o = StorageObject(object_key=key, tenant_id="arkturian", owner_user_id=owner.id, is_public=False,
                          original_filename=f"Lohnzettel {k}.pdf", mime_type="application/pdf",
                          file_size_bytes=len(DATA), file_url=f"/uploads/storage/media/arkturian/{key}",
                          metadata_json={"private_media": True}, encrypted=(k == "enc"))
        s.add(o); s.commit()
        objs[k] = o.id
    s.add_all([MediaGrant(object_id=objs["doc"], scope=AGENT, granted_by_user_id=1, granted_via="upload"),
               MediaGrant(object_id=objs["enc"], scope=AGENT, granted_by_user_id=1, granted_via="upload"),
               MediaGrant(object_id=objs["received"], scope=AGENT, granted_by_user_id=1,
                          granted_via="share:cloud-session:Business")])
    s.commit()
    owner_id, other_id = owner.id, other.id
    state = {"uid": owner_id, "on_behalf": True}
    s.close()

    def _db():
        d = S()
        try:
            yield d
        finally:
            d.close()

    def _cur():
        d = S()
        u = d.get(User, state["uid"])
        d.expunge(u); d.close()
        if state["on_behalf"]:
            setattr(u, "_storage_on_behalf_of", True)
        return u

    main.app.dependency_overrides[get_db] = _db
    main.app.dependency_overrides[storage_routes.get_current_user] = _cur
    main.app.dependency_overrides[storage_routes.get_current_user_optional] = lambda: None
    try:
        yield SimpleNamespace(c=TestClient(main.app), S=S, objs=objs, state=state, owner=owner_id, other=other_id)
    finally:
        main.app.dependency_overrides.clear()


def _create(env, obj="doc", key=SVC, **body):
    return env.c.post(f"/storage/objects/{env.objs[obj]}/share-link", headers={"X-API-KEY": key}, json=body)


def _token(url):
    return urlparse(url).path.split("/")[2]


def test_owner_creates_link_and_only_hash_is_stored(env):
    r = _create(env)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["url"].startswith("https://files.example.test/s/") and j["url"].endswith("/Lohnzettel-doc.pdf")
    tok = _token(j["url"])
    assert len(tok) >= 32 and j["token_prefix"] == tok[:6]
    s = env.S()
    rows = s.query(ShareLink).all()
    assert len(rows) == 1 and rows[0].token_hash == hashlib.sha256(tok.encode()).hexdigest()
    assert tok not in json.dumps([c.name for c in ShareLink.__table__.columns]) and rows[0].token_prefix == tok[:6]
    assert abs((rows[0].expires_at - datetime.utcnow()) - timedelta(days=30)) < timedelta(minutes=2)
    s.close()


def test_open_redirects_to_short_signed_url_and_serves_private_file(env):
    from storage import signing
    tok = _token(_create(env).json()["url"])
    r = env.c.get(f"/storage/s/{tok}/egal.pdf", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["referrer-policy"] == "no-referrer" and "private" in r.headers["cache-control"]
    loc = urlparse(r.headers["location"])
    assert loc.path == f"/storage/media/{env.objs['doc']}"
    q = {k: v[0] for k, v in parse_qs(loc.query).items()}
    assert signing.verify(env.objs["doc"], int(q["exp"]), q["kid"], q["sig"])
    assert int(q["exp"]) - time.time() <= 125
    got = env.c.get(r.headers["location"])
    assert got.status_code == 200 and got.content == DATA


def test_on_share_host_redirect_stays_on_files_path(env):
    tok = _token(_create(env).json()["url"])
    r = env.c.get(f"/storage/s/{tok}", headers={"X-Forwarded-Host": "files.example.test"}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith(f"/{env.objs['doc']}/Lohnzettel-doc.pdf?exp=")


def test_private_file_still_not_reachable_by_id(env):
    _create(env)
    assert env.c.get(f"/storage/media/{env.objs['doc']}").status_code in (401, 403, 404)


@pytest.mark.parametrize("tok", ["A" * 32, "kurz", "../../etc"])
def test_unknown_token_is_404(env, tok):
    assert env.c.get(f"/storage/s/{tok}", follow_redirects=False).status_code == 404


def test_revoke_makes_link_404(env):
    j = _create(env).json()
    tok = _token(j["url"])
    r = env.c.delete(f"/storage/objects/{env.objs['doc']}/share-links/{j['id']}", headers={"X-API-KEY": SVC})
    assert r.status_code == 200 and r.json()["revoked"] is True
    assert env.c.get(f"/storage/s/{tok}", follow_redirects=False).status_code == 404
    lst = env.c.get(f"/storage/objects/{env.objs['doc']}/share-links", headers={"X-API-KEY": SVC}).json()
    assert lst["share_links"][0]["aktiv"] is False and "url" not in lst["share_links"][0]


def test_expired_link_is_404(env):
    tok = _token(_create(env).json()["url"])
    s = env.S()
    s.query(ShareLink).update({ShareLink.expires_at: datetime.utcnow() - timedelta(seconds=1)})
    s.commit(); s.close()
    assert env.c.get(f"/storage/s/{tok}", follow_redirects=False).status_code == 404


@pytest.mark.parametrize("delta", [timedelta(seconds=-10), timedelta(days=400)])
def test_gueltig_bis_bounds(env, delta):
    r = _create(env, gueltig_bis=(datetime.utcnow() + delta).isoformat())
    assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_gueltig_bis"


def test_gueltig_bis_respected(env):
    until = datetime.utcnow() + timedelta(days=3)
    j = _create(env, gueltig_bis=until.isoformat()).json()
    assert j["gueltig_bis"].startswith(until.isoformat()[:16])


def test_not_owner_gets_404(env):
    env.state["uid"] = env.other
    assert _create(env).status_code == 404


def test_plain_tenant_key_owner_without_on_behalf_refused(env):
    env.state["on_behalf"] = False
    assert _create(env).status_code == 404


def test_encrypted_refused(env):
    r = _create(env, obj="enc")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "encrypted_share_link_not_allowed"


def test_agent_path_own_file(env):
    r = _create(env, key=CLOUD, scope=AGENT)
    assert r.status_code == 200 and r.json()["created_by"] == f"agent:{AGENT}"
    lst = env.c.get(f"/storage/objects/{env.objs['doc']}/share-links", params={"scope": AGENT},
                    headers={"X-API-KEY": CLOUD}).json()
    assert len(lst["share_links"]) == 1


def test_agent_sees_and_revokes_only_its_own_links(env):
    mine = _create(env, key=CLOUD, scope=AGENT).json()
    owners = _create(env).json()
    lst = env.c.get(f"/storage/objects/{env.objs['doc']}/share-links", params={"scope": AGENT},
                    headers={"X-API-KEY": CLOUD}).json()
    assert [x["id"] for x in lst["share_links"]] == [mine["id"]]
    r = env.c.delete(f"/storage/objects/{env.objs['doc']}/share-links/{owners['id']}", params={"scope": AGENT},
                     headers={"X-API-KEY": CLOUD})
    assert r.status_code == 404


def test_agent_received_file_refused(env):
    r = _create(env, obj="received", key=CLOUD, scope=AGENT)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "not_own_file"


@pytest.mark.parametrize("key,scope,code", [
    (NOFLAG, AGENT, "share_link_not_allowed"),
    (CLOUD, "cloud-session:Fremd", "not_granted"),
    (CLOUD, "cloud-user:7", "invalid_scope"),
])
def test_agent_path_gates(env, key, scope, code):
    r = _create(env, key=key, scope=scope)
    assert r.status_code in (403, 422) and r.json()["detail"]["code"] == code


def test_agent_path_refuses_on_behalf(env):
    r = env.c.post(f"/storage/objects/{env.objs['doc']}/share-link",
                   headers={"X-API-KEY": CLOUD, "X-On-Behalf-Of": "alex@x"}, json={"scope": AGENT})
    assert r.status_code == 400


def test_link_goes_with_deleted_object(env):
    _create(env)
    s = env.S()
    s.delete(s.get(StorageObject, env.objs["doc"])); s.commit()
    assert s.query(ShareLink).count() == 0
    s.close()
