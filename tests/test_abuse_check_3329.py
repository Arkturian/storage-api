"""#3329 (c), Alex 18.11.: images/videos that go public or outside are
checked first (OpenAI Decisions via api-ai). Fail-closed: without a clean
verdict the file stays private. A hit locks it ("privat, fest"), revokes its
share links, blocks it for everyone but an admin and raises
storage.abuse_suspected (cloud-api turns csam into a card for Alex)."""
import asyncio
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from models import Base, ShareLink, StorageObject, TenantAPIKey, User  # noqa: E402

CLEAR = {"csam": 0.0, "minderjaehrig_sichtbar": 0.1, "sexuell_explizit": 0.02, "gewalt_missbrauch": 0.03}


def P(**kw):
    return {**{"csam": 0.0, "minderjaehrig_sichtbar": 0.0, "sexuell_explizit": 0.0, "gewalt_missbrauch": 0.0}, **kw}


def _png():
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (32, 24), (10, 120, 200)).save(b, format="PNG")
    return b.getvalue()


@pytest.fixture
def env(tmp_path, monkeypatch):
    from storage import abuse_check as A
    from storage import routes as R
    from storage import share_links as SL
    from storage import abuse_review as AR
    from storage.service import generic_storage
    monkeypatch.setenv("ABUSE_CHECK_ENABLED", "1")
    monkeypatch.setenv("ABUSE_CHECK_MODE", "scharf")   # these tests cover enforcing mode
    monkeypatch.setenv("STORAGE_SIGN_SECRETS", json.dumps({"k1": "x" * 40}))
    monkeypatch.setenv("STORAGE_SIGN_ACTIVE_KID", "k1")
    up = tmp_path / "up"
    for attr, sub in (("upload_root", ""), ("media_dir", "media"), ("thumbnails_dir", "thumbnails"), ("webview_dir", "webview")):
        p = up / sub if sub else up
        p.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(generic_storage, attr, p)
    eng = create_engine(f"sqlite:///{tmp_path/'t.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    import database
    S = sessionmaker(bind=eng)
    monkeypatch.setattr(database, "SessionLocal", S)
    s = S()
    owner = User(email="alex@x", display_name="a", password_hash="", api_key="ko", trust_level="user", device_ids=[])
    ops = User(email="ops@x", display_name="o", password_hash="", api_key="k-ops-not-a-tenant-key",
               trust_level="admin", device_ids=[])
    s.add_all([owner, ops]); s.commit()
    s.add(TenantAPIKey(api_key="k-svc", tenant_id="arkturian", label="svc", is_active=True, is_service=True))
    s.commit()
    state = {"user": True, "answer": CLEAR, "calls": 0, "admin": False}

    async def fake_ask(obj):
        state["calls"] += 1
        a = state["answer"]
        if isinstance(a, Exception):
            raise a
        return dict(a)

    monkeypatch.setattr(A, "_ask_orig", A._ask, raising=False)
    monkeypatch.setattr(A, "_ask", fake_ask)
    # never touch a real Chroma store from a test (object ids here are small numbers)
    monkeypatch.setattr(A, "_drop_embedding", lambda o: None)
    events = []
    from storage import encrypted_io
    monkeypatch.setattr(encrypted_io, "emit_event", lambda k, p: events.append((k, p)))

    from auth import get_current_user, get_current_user_optional
    from database import get_db
    from tenancy.config import get_tenant_id, get_tenant_id_optional

    def _cur():
        if state["admin"]:
            return s.get(User, ops.id)
        u = s.get(User, owner.id)
        setattr(u, "_storage_on_behalf_of", True)
        return u

    app = FastAPI()
    app.include_router(R.router, prefix="/storage")
    app.include_router(SL.router, prefix="/storage")
    app.include_router(AR.router, prefix="/storage")
    app.dependency_overrides[get_db] = lambda: s
    app.dependency_overrides[get_current_user] = _cur
    app.dependency_overrides[get_current_user_optional] = lambda: _cur() if state["user"] else None
    app.dependency_overrides[get_tenant_id] = lambda: "arkturian"
    app.dependency_overrides[get_tenant_id_optional] = lambda: "arkturian"
    yield SimpleNamespace(c=TestClient(app), s=s, S=S, state=state, events=events, A=A)
    s.close()


def _upload(env, data=None, name="bild.png", mime="image/png", public=True):
    r = env.c.post("/storage/upload", files={"file": (name, data or _png(), mime)},
                   data={"is_public": "true" if public else "false", "ai_mode": "none"})
    assert r.status_code == 200, r.text
    return r.json()


def _obj(env, oid):
    env.s.expire_all()
    return env.s.get(StorageObject, oid)


def test_clean_image_goes_public(env):
    j = _upload(env)
    o = _obj(env, j["id"])
    assert o.is_public is True and o.abuse_status == "clear" and env.state["calls"] == 1


def test_no_verdict_keeps_it_private(env):
    env.state["answer"] = env.A.CheckUnavailable("http 503")
    j = _upload(env)
    o = _obj(env, j["id"])
    assert o.is_public is False and o.public_pending is True and o.abuse_status == "failed"
    env.state["user"] = False
    assert env.c.get(f"/storage/media/{j['id']}").status_code == 403


def test_retry_makes_it_public_later(env):
    env.state["answer"] = env.A.CheckUnavailable("timeout")
    j = _upload(env)
    env.state["answer"] = CLEAR
    n = asyncio.get_event_loop().run_until_complete(env.A.retry_open(env.S))
    assert n == 1
    o = _obj(env, j["id"])
    assert o.is_public is True and o.public_pending is False and o.abuse_status == "clear"


def test_hit_locks_blocks_and_raises_event(env):
    env.state["answer"] = P(csam=0.93, sexuell_explizit=0.9)
    j = _upload(env)
    o = _obj(env, j["id"])
    assert o.abuse_status == "hit" and o.is_public is False and o.metadata_json.get("private_media") is True
    assert env.c.get(f"/storage/media/{j['id']}").status_code == 451          # even the owner
    kinds = [(k, p["kategorie"]) for k, p in env.events]
    assert kinds == [("abuse_suspected", "csam")]
    assert "original_filename" not in env.events[0][1] and "url" not in json.dumps(env.events[0][1])


def test_non_media_goes_public_without_check(env):
    j = _upload(env, data=b"hallo\n" * 10, name="n.txt", mime="text/plain")
    o = _obj(env, j["id"])
    assert o.is_public is True and o.abuse_status is None and env.state["calls"] == 0


def test_private_upload_never_checked(env):
    j = _upload(env, public=False)
    o = _obj(env, j["id"])
    assert o.is_public is False and o.abuse_status is None and env.state["calls"] == 0


def test_patch_public_goes_through_check(env):
    j = _upload(env, public=False)
    env.state["answer"] = env.A.CheckUnavailable("refusal")
    r = env.c.patch(f"/storage/objects/{j['id']}", json={"is_public": True})
    assert r.status_code == 200 and r.json()["is_public"] is False and r.json()["public_pending"] is True
    env.state["answer"] = CLEAR
    asyncio.get_event_loop().run_until_complete(env.A.retry_open(env.S))
    assert _obj(env, j["id"]).is_public is True


def _tok(url):
    return urlparse(url).path.split("/s/", 1)[1].split("/")[0]


def _link(env, oid):
    r = env.c.post(f"/storage/objects/{oid}/share-link", headers={"X-API-KEY": "k-svc"}, json={})
    return r


def test_share_link_of_image_waits_for_clear(env):
    j = _upload(env, public=False)
    env.state["answer"] = env.A.CheckUnavailable("503")
    r = _link(env, j["id"])
    assert r.status_code == 200 and r.json()["pruefung"] == "failed"
    tok = _tok(r.json()["url"])
    assert env.c.get(f"/storage/s/{tok}", follow_redirects=False).status_code == 404
    env.state["answer"] = CLEAR
    asyncio.get_event_loop().run_until_complete(env.A.retry_open(env.S))
    env.s.expire_all()  # the test shares one session; production opens a fresh one per request
    assert env.c.get(f"/storage/s/{tok}", follow_redirects=False).status_code == 302
    assert _obj(env, j["id"]).is_public is False          # a share link never makes it public


def test_hit_at_share_refused_and_existing_links_revoked(env):
    j = _upload(env, public=False)
    tok = _tok(_link(env, j["id"]).json()["url"])
    env.s.get(StorageObject, j["id"]).abuse_status = None   # pretend a later re-check
    env.s.commit()
    env.state["answer"] = P(gewalt_missbrauch=0.95)
    r = _link(env, j["id"])
    assert r.status_code == 451
    assert env.s.query(ShareLink).filter(ShareLink.revoked_at.is_(None)).count() == 0
    assert env.c.get(f"/storage/s/{tok}", follow_redirects=False).status_code == 404


def test_flag_off_changes_nothing(env, monkeypatch):
    monkeypatch.setenv("ABUSE_CHECK_ENABLED", "0")
    j = _upload(env)
    o = _obj(env, j["id"])
    assert o.is_public is True and o.abuse_status is None and env.state["calls"] == 0


def test_hit_quarantines_hls_out_of_the_nginx_tree(env, tmp_path, monkeypatch):
    from storage.service import generic_storage
    j = _upload(env, public=False)
    o = env.s.get(StorageObject, j["id"])
    hls = Path(generic_storage.media_dir) / o.tenant_id / Path(o.object_key).stem
    hls.mkdir(parents=True)
    (hls / "master.m3u8").write_text("#EXTM3U")
    o.hls_url = "https://x/uploads/storage/media/arkturian/" + hls.name + "/master.m3u8"
    env.s.commit()
    q = tmp_path / "quarantine"
    monkeypatch.setenv("ABUSE_QUARANTINE_DIR", str(q))
    env.state["answer"] = P(sexuell_explizit=0.95)
    r = env.c.patch(f"/storage/objects/{j['id']}", json={"is_public": True})
    assert r.status_code == 200
    o = _obj(env, j["id"])
    assert o.abuse_status == "hit" and o.hls_url is None
    assert not hls.exists() and (q / f"{o.id}_{hls.name}" / "master.m3u8").exists()
    assert o.metadata_json["abuse_check"]["hls_quarantaene"].startswith(str(q))


def test_hit_object_hidden_from_lists_metadata_and_sign(env):
    env.state["answer"] = P(csam=0.9)
    j = _upload(env)
    ids = [x["id"] for x in env.c.get("/storage/list", params={"limit": 50}).json()["items"]]
    assert j["id"] not in ids
    assert env.c.get(f"/storage/objects/{j['id']}").status_code == 451
    from models import MediaGrant
    env.s.add(TenantAPIKey(api_key="k-sign", tenant_id="arkturian", label="sign", is_active=True, sign_tenants="arkturian"))
    env.s.add(MediaGrant(object_id=j["id"], scope="cloud-user:1", granted_by_user_id=1, granted_via="upload"))
    env.s.commit()
    r = env.c.post("/storage/sign", headers={"X-API-KEY": "k-sign"},
                   json={"tenant": "arkturian", "scope": "cloud-user:1", "ids": [j["id"]], "ttl": 300})
    assert r.json()["items"][0]["error"] == "abuse_blocked"


def test_hit_drops_embedding(env, monkeypatch):
    dropped = []
    monkeypatch.setattr(env.A, "_drop_embedding", lambda o: dropped.append(o.id))
    env.state["answer"] = P(gewalt_missbrauch=0.9)
    j = _upload(env)
    assert dropped == [j["id"]]


def test_old_signed_url_blocked_after_hit(env):
    from storage import signing
    j = _upload(env, public=False)
    params = signing.sign(j["id"], 300)
    env.state["user"] = False
    assert env.c.get(f"/storage/media/{j['id']}", params=params).status_code == 200
    o = env.s.get(StorageObject, j["id"])
    env.A.apply_hit(env.s, o, "csam", 0.9, "test")
    env.s.expire_all()
    for extra in ({}, {"variant": "thumbnail"}, {"width": "200", "format": "webp"}):
        assert env.c.get(f"/storage/media/{j['id']}", params={**params, **extra}).status_code == 451
    assert env.c.head(f"/storage/media/{j['id']}", params=params).status_code == 451



@pytest.mark.parametrize("probs,expect", [
    (P(), ("clear", None)),
    (P(minderjaehrig_sichtbar=0.99), ("clear", None)),           # a child alone never triggers
    (P(csam=0.06), ("held", "csam")),
    (P(csam=0.2), ("hit", "csam")),
    (P(minderjaehrig_sichtbar=0.5, sexuell_explizit=0.2), ("hit", "csam")),
    (P(sexuell_explizit=0.3), ("held", "sexuell_explizit")),
    (P(gewalt_missbrauch=0.79), ("held", "gewalt_missbrauch")),
    (P(sexuell_explizit=0.8), ("hit", "sexuell_explizit")),
    (P(gewalt_missbrauch=0.8), ("hit", "gewalt_missbrauch")),
])
def test_thresholds_from_contract_6255(probs, expect):
    from storage import abuse_check as A
    r, k, _ = A._verdict(probs)
    assert (r, k) == expect


def test_stage1_stays_private_without_lock_or_card(env):
    env.state["answer"] = P(sexuell_explizit=0.4)
    j = _upload(env)
    o = _obj(env, j["id"])
    assert o.abuse_status == "held" and o.is_public is False and o.public_pending is False
    assert not (o.metadata_json or {}).get("private_media")
    assert env.c.get(f"/storage/media/{j['id']}").status_code == 200        # owner still reads it
    assert env.events == []
    r = _link(env, j["id"])
    assert r.status_code == 403 and r.json()["detail"]["code"] == "abuse_held"


def test_two_refusals_hold_and_raise_card_event(env):
    env.state["answer"] = env.A.Refused("refusal csam")
    j = _upload(env)
    assert _obj(env, j["id"]).abuse_status == "failed" and env.events == []
    asyncio.get_event_loop().run_until_complete(env.A.retry_open(env.S))
    o = _obj(env, j["id"])
    assert o.abuse_status == "held" and o.is_public is False
    assert [(k, p["kategorie"]) for k, p in env.events] == [("abuse_suspected", "ablehnung")]


def test_values_logged_without_name(env, capsys):
    env.state["answer"] = CLEAR
    _upload(env, name="Geheimer Name.png")
    out = capsys.readouterr().out
    line = [l for l in out.splitlines() if "abuse check object=" in l][0]
    assert "csam=0.0" in line and "minderjaehrig_sichtbar=0.1" in line and "Geheimer" not in line


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._b = status, body

    def json(self):
        return self._b


class _Client:
    def __init__(self, resp):
        self.resp, self.sent = resp, None

    async def post(self, url, json=None, headers=None):
        self.sent = json
        return self.resp


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_ask_frame_sends_contract_body_and_parses():
    from storage import abuse_check as A
    ans = [{"name": n, "type": "predicate", "probability": 0.01} for n in A.QUESTIONS]
    c = _Client(_Resp(200, {"answers": ans, "cost_usd": 0.00015}))
    probs = _run(A._ask_frame(c, "u", "t", b"\xff\xd8jpeg"))
    assert probs == {n: 0.01 for n in A.QUESTIONS}
    content = c.sent["input"][0]["content"][0]
    assert content["type"] == "input_image" and content["image_url"].startswith("data:image/jpeg;base64,")
    assert [q["name"] for q in c.sent["questions"]] == list(A.QUESTIONS)
    assert all(q["type"] == "predicate" for q in c.sent["questions"])


def test_ask_frame_refusal_and_errors():
    from storage import abuse_check as A
    ans = [{"name": "csam", "type": "refusal", "refusal": "nein"}] + \
          [{"name": n, "type": "predicate", "probability": 0.0} for n in list(A.QUESTIONS)[1:]]
    with pytest.raises(A.Refused):
        _run(A._ask_frame(_Client(_Resp(200, {"answers": ans})), "u", "t", b"x"))
    with pytest.raises(A.CheckUnavailable):
        _run(A._ask_frame(_Client(_Resp(429, {"error": "decisions_caller_budget_exceeded"})), "u", "t", b"x"))


def test_video_takes_highest_value_over_frames(env, monkeypatch):  # env keeps A._ask_orig
    from storage import abuse_check as A
    seq = iter([P(), P(gewalt_missbrauch=0.85), P()])

    async def frames(obj):
        return [b"a", b"b", b"c"]

    async def tok():
        return "t"

    async def ask_frame(c, url, token, jpeg):
        return next(seq)

    monkeypatch.setattr(A, "_frames", frames)
    monkeypatch.setattr(A, "_service_token", tok)
    monkeypatch.setattr(A, "_ask_frame", ask_frame)
    best = _run(A._ask_orig(SimpleNamespace(id=1)))    # the original _ask, kept by the fixture
    assert best["gewalt_missbrauch"] == 0.85 and best["csam"] == 0.0



# --- Shadow mode (Cloud 18.11.: first week logs only, nothing held or locked) ---

@pytest.fixture
def shadow_env(env, monkeypatch):
    monkeypatch.delenv("ABUSE_CHECK_MODE", raising=False)   # default once enabled = schatten
    calls = []
    monkeypatch.setattr(env.A, "shadow_soon", lambda o, anlass: calls.append((o.id, anlass)))
    env.shadow_calls = calls
    return env


def test_default_mode_is_shadow(shadow_env):
    A = shadow_env.A
    assert A.enabled() and A.shadow() and not A.enforcing()


def test_shadow_public_upload_goes_public_now_and_is_checked_later(shadow_env):
    j = _upload(shadow_env)
    o = _obj(shadow_env, j["id"])
    assert o.is_public is True and o.abuse_status is None and o.public_pending is False
    assert shadow_env.shadow_calls == [(j["id"], "upload_public")]
    assert shadow_env.state["calls"] == 0          # nothing asked synchronously


def test_shadow_check_logs_and_changes_nothing(shadow_env):
    j = _upload(shadow_env)
    shadow_env.state["answer"] = P(sexuell_explizit=0.95)
    r = asyncio.get_event_loop().run_until_complete(shadow_env.A.shadow_check(j["id"], "upload_public"))
    assert r == "hit"
    o = _obj(shadow_env, j["id"])
    assert o.is_public is True and o.abuse_status is None
    rec = o.metadata_json["abuse_check_schatten"]
    assert rec["ergebnis"] == "hit" and rec["werte"]["sexuell_explizit"] == 0.95
    shadow_env.state["user"] = False
    assert shadow_env.c.get(f"/storage/media/{j['id']}").status_code == 200
    assert shadow_env.events == []                  # no card outside csam


def test_shadow_csam_stage2_locks_for_real(shadow_env):
    j = _upload(shadow_env)
    tok = _tok(_link(shadow_env, j["id"]).json()["url"])
    shadow_env.state["answer"] = P(csam=0.5)
    r = asyncio.get_event_loop().run_until_complete(shadow_env.A.shadow_check(j["id"], "upload_public"))
    assert r == "hit"
    o = _obj(shadow_env, j["id"])
    assert o.abuse_status == "hit" and o.is_public is False and o.metadata_json.get("private_media") is True
    assert shadow_env.c.get(f"/storage/media/{j['id']}").status_code == 451
    assert shadow_env.c.get(f"/storage/s/{tok}", follow_redirects=False).status_code == 404
    (k, p), = shadow_env.events
    assert k == "abuse_suspected" and p["kategorie"] == "csam" and p["gesperrt"] is True


def test_shadow_csam_stage1_only_logged(shadow_env):
    j = _upload(shadow_env)
    shadow_env.state["answer"] = P(csam=0.1)
    r = asyncio.get_event_loop().run_until_complete(shadow_env.A.shadow_check(j["id"], "upload_public"))
    o = _obj(shadow_env, j["id"])
    assert r == "held" and o.abuse_status is None and o.is_public is True and shadow_env.events == []


def test_shadow_second_refusal_card_not_locked(shadow_env):
    j = _upload(shadow_env)
    shadow_env.state["answer"] = shadow_env.A.Refused("refusal csam")
    for _ in range(2):
        asyncio.get_event_loop().run_until_complete(shadow_env.A.shadow_check(j["id"], "upload_public"))
    (k, p), = shadow_env.events
    assert p["kategorie"] == "ablehnung" and p["gesperrt"] is False and p["schatten"] is True
    assert _obj(shadow_env, j["id"]).abuse_status is None


def test_shadow_share_link_not_gated(shadow_env):
    j = _upload(shadow_env, public=False)
    r = _link(shadow_env, j["id"])
    assert r.status_code == 200 and "pruefung" not in r.json()
    assert shadow_env.c.get(f"/storage/s/{_tok(r.json()['url'])}", follow_redirects=False).status_code == 302
    assert shadow_env.shadow_calls[-1] == (j["id"], "share_link")



# --- #3346: admin review of a verdict ---

def _review(env, oid, entscheidung="fehlalarm", begruendung="Fehlalarm, Familienfoto ohne Bezug"):
    return env.c.post(f"/storage/objects/{oid}/abuse-review",
                      json={"entscheidung": entscheidung, "begruendung": begruendung})


def _hit_with_hls(env, tmp_path, monkeypatch):
    from storage.service import generic_storage
    j = _upload(env, public=False)
    o = env.s.get(StorageObject, j["id"])
    hls = Path(generic_storage.media_dir) / o.tenant_id / Path(o.object_key).stem
    hls.mkdir(parents=True)
    (hls / "master.m3u8").write_text("#EXTM3U")
    url = "https://x/uploads/storage/media/arkturian/" + hls.name + "/master.m3u8"
    o.hls_url = url
    env.s.commit()
    monkeypatch.setenv("ABUSE_QUARANTINE_DIR", str(tmp_path / "q"))
    env.A.apply_hit(env.s, o, "csam", 0.6, "test")
    env.events.clear()
    return j["id"], hls, url


def test_only_real_admin_may_review(env, tmp_path, monkeypatch):
    oid, _, _ = _hit_with_hls(env, tmp_path, monkeypatch)
    r = _review(env, oid)                                    # owner via service key + on-behalf
    assert r.status_code == 403 and r.json()["detail"]["code"] == "admin_required"
    assert _obj(env, oid).abuse_status == "hit"


def test_false_alarm_lifts_block_restores_hls_and_logs(env, tmp_path, monkeypatch):
    from models import AbuseReview
    oid, hls, url = _hit_with_hls(env, tmp_path, monkeypatch)
    assert not hls.exists()
    env.state["admin"] = True
    r = _review(env, oid)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["status_vorher"] == "hit" and j["status_nachher"] == "clear" and j["hls"] == "zurueck"
    assert j["private_media"] is True                         # one-way, owner sets visibility again
    o = _obj(env, oid)
    assert o.abuse_status == "clear" and o.hls_url == url and (hls / "master.m3u8").exists()
    rows = env.s.query(AbuseReview).filter_by(object_id=oid).all()
    assert len(rows) == 1 and rows[0].admin == "ops@x" and rows[0].entscheidung == "fehlalarm"
    assert [(k, p["entscheidung"]) for k, p in env.events] == [("abuse_reviewed", "fehlalarm")]
    env.state["admin"] = False
    assert env.c.get(f"/storage/media/{oid}").status_code == 200      # owner reads again (private)
    lst = env.c.get(f"/storage/objects/{oid}/abuse-review")
    assert lst.status_code == 403


def test_confirmed_keeps_block_and_logs(env, tmp_path, monkeypatch):
    from models import AbuseReview
    oid, hls, _ = _hit_with_hls(env, tmp_path, monkeypatch)
    env.state["admin"] = True
    r = _review(env, oid, "bestaetigt", "Bestaetigt, Meldung durch Alex veranlasst")
    assert r.status_code == 200 and r.json()["status_nachher"] == "hit"
    assert _obj(env, oid).abuse_status == "hit" and not hls.exists()
    lst = env.c.get(f"/storage/objects/{oid}/abuse-review").json()["reviews"]
    assert [x["entscheidung"] for x in lst] == ["bestaetigt"]
    assert env.s.query(AbuseReview).count() == 1


def test_held_false_alarm(env):
    env.state["answer"] = P(sexuell_explizit=0.4)
    j = _upload(env)
    env.state["admin"] = True
    r = _review(env, j["id"])
    assert r.status_code == 200 and r.json()["status_vorher"] == "held" and r.json()["status_nachher"] == "clear"


@pytest.mark.parametrize("body,code", [
    ({"entscheidung": "vielleicht", "begruendung": "x" * 20}, "invalid_entscheidung"),
    ({"entscheidung": "fehlalarm", "begruendung": "kurz"}, "invalid_begruendung"),
])
def test_review_validation(env, tmp_path, monkeypatch, body, code):
    oid, _, _ = _hit_with_hls(env, tmp_path, monkeypatch)
    env.state["admin"] = True
    r = env.c.post(f"/storage/objects/{oid}/abuse-review", json=body)
    assert r.status_code == 422 and r.json()["detail"]["code"] == code


def test_nothing_to_review(env):
    j = _upload(env)                                          # clear
    env.state["admin"] = True
    r = _review(env, j["id"])
    assert r.status_code == 409 and r.json()["detail"]["code"] == "nothing_to_review"


# --- #3366: a 429 from api-ai's daily limit waits for resets_at ---------------

def test_ask_frame_429_carries_resets_at():
    from storage import abuse_check as A
    body = {"detail": {"error": "decisions_caller_budget_exceeded", "resets_at": "2026-11-26T00:00:00+01:00"}}
    with pytest.raises(A.RateLimited) as e:
        _run(A._ask_frame(_Client(_Resp(429, body)), "u", "t", b"x"))
    assert e.value.resets_at == "2026-11-25T23:00:00Z"
    with pytest.raises(A.RateLimited) as e:
        _run(A._ask_frame(_Client(_Resp(429, {"error": "x"})), "u", "t", b"x"))
    assert e.value.resets_at is None


def test_rate_limited_object_waits_and_keeps_its_attempts(env):
    A = env.A
    env.state["answer"] = A.RateLimited("http 429", "2999-01-01T00:00:00Z")
    j = _upload(env)
    o = _obj(env, j["id"])
    assert o.abuse_status == "failed" and o.is_public is False
    assert o.metadata_json["abuse_check"]["versuche"] == 0
    assert o.metadata_json["abuse_check"]["naechster_versuch"] == "2999-01-01T00:00:00Z"
    env.state["answer"] = CLEAR
    assert _run(A.retry_open(env.S)) == 0          # still waiting for the reset
    assert env.state["calls"] == 1


def test_rate_limit_over_lets_the_retry_through(env):
    A = env.A
    env.state["answer"] = A.RateLimited("http 429", "2000-01-01T00:00:00Z")
    j = _upload(env)
    env.state["answer"] = CLEAR
    assert _run(A.retry_open(env.S)) == 1
    o = _obj(env, j["id"])
    assert o.abuse_status == "clear" and o.is_public is True


def test_other_failure_clears_the_wait(env):
    A = env.A
    env.state["answer"] = A.RateLimited("http 429", "2999-01-01T00:00:00Z")
    j = _upload(env)
    o = _obj(env, j["id"])
    env.state["answer"] = A.CheckUnavailable("http 503")
    _run(A.check(env.s, o, "nachgeholt"))
    o = _obj(env, j["id"])
    assert o.metadata_json["abuse_check"]["naechster_versuch"] is None
    assert o.metadata_json["abuse_check"]["versuche"] == 1
