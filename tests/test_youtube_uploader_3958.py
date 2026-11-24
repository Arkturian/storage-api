"""#3958: the youtube-uploader profile for the youtube-api (Alex, card a2e85234, 25.11.2026).

Private uploads bound to cloud-session:YouTube only, a listing of its own
objects, nothing else. The route walk over every other route is in
test_route_profiles_3631.py (EXPECTED / KEY_OF).
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from models import StorageObject, User  # noqa: E402
from tests.test_route_profiles_3631 import AGENT, OWNER, YOUTUBE, _count, _snapshot, env  # noqa: E402,F401

SCOPE = "cloud-session:YouTube"
ACCOUNT = "youtube-uploader.david@api"


def _yt(c, headers=None, filename="yt-private-job1-transcript.json", body=b'{"t": 1}', **fields):
    # The field set of youtube-api app/pipeline/clients.py (52ccd29), with the scope from #3958.
    data = {"private": "true", "grant_scope": SCOPE, "is_public": "false", "ai_mode": "none",
            "analyze": "false", "collection_id": "youtube-content-private", "link_id": "job1",
            "reuse_existing": "true", **fields}
    data = {k: v for k, v in data.items() if v is not None}
    return c.post("/storage/upload", headers={"X-API-KEY": YOUTUBE, **(headers or {})}, data=data,
                  files={"file": (filename, body, "application/json")})


def _stored(eng):
    return (_count(eng, "SELECT count(*) FROM storage_objects"), _count(eng, "SELECT count(*) FROM media_grants"))


def _row(eng, oid):
    with eng.connect() as conn:
        return conn.execute(text("SELECT o.is_public, o.tenant_id, u.email, o.metadata_json, o.collection_id "
                                 "FROM storage_objects o JOIN users u ON u.id=o.owner_user_id WHERE o.id=:i"),
                            {"i": oid}).one()


def test_pipeline_upload_is_private_own_and_bound_to_youtube(env):
    c, _, eng = env
    r = _yt(c)
    assert r.status_code == 200, r.text
    new = r.json()["id"]
    is_public, tenant, email, md, coll = _row(eng, new)
    assert not is_public and tenant == "david" and email == ACCOUNT
    assert '"private_media": true' in md
    assert coll == "youtube-content-private"
    assert _count(eng, "SELECT count(*) FROM media_grants WHERE object_id=:o", o=new) == 1
    assert _count(eng, "SELECT scope FROM media_grants WHERE object_id=:o", o=new) == SCOPE


def test_download_cache_upload_fields(env):
    # app/services/youtube_dl.py: context, no ai switches, reuse_existing=false.
    c, _, _ = env
    r = _yt(c, filename="dQw4w9WgXcQ - t.mp3", context="YouTube: t", collection_id="youtube-mp3",
            link_id="dQw4w9WgXcQ", reuse_existing="false", ai_mode=None, analyze=None)
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("fields", [
    {"is_public": "true"}, {"is_public": "1"}, {"private": "false"}, {"private": None},
    {"grant_scope": "agent:YouTube"}, {"grant_scope": AGENT}, {"grant_scope": "cloud-session:YouTubeX"},
    {"grant_scope": f"{SCOPE},{AGENT}"}, {"grant_scope": "content-post:7"}, {"grant_scope": " , "},
    {"ai_mode": "safety"}, {"ai_mode": "vision"}, {"analyze": "true"}, {"reuse_existing": "1"},
    {"collection_id": "a/b"}, {"collection_id": ""}, {"link_id": "x" * 81}, {"link_id": "a b"},
    {"owner_email": OWNER}, {"storage_mode": "reference"}, {"reference_path": "/etc/passwd"},
    {"external_uri": "https://example.org/x"}, {"reference_id": "1"}, {"hls_result": "true"},
    {"visibility_level": "intern"}, {"encrypted": "true"}, {"ttl_hours": "1"}, {"ai_tasks": "vision"},
    {"transcribe_audio": "true"}, {"metadata_json": "{}"},
])
def test_refuses_everything_beyond_a_private_youtube_file(env, fields):
    c, _, eng = env
    assert _yt(c, filename="first.json").status_code == 200  # machine account exists from here on
    before = _stored(eng)
    r = _yt(c, filename="second.json", **fields)
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "ingest_upload_refused"
    assert _stored(eng) == before


@pytest.mark.parametrize("header", ["X-On-Behalf-Of", "X-Compute-Focal"])
def test_refuses_extra_headers(env, header):
    c, _, eng = env
    before = _stored(eng)
    r = _yt(c, headers={header: OWNER if header == "X-On-Behalf-Of" else "true"})
    assert r.status_code == 403
    assert _stored(eng) == before


@pytest.mark.parametrize("call", ["sign", "delete", "delete_objects", "grant", "share", "media"])
def test_named_counter_probes_hit_the_gate(env, call):
    c, oid, eng = env
    h = {"X-API-KEY": YOUTUBE}
    own = _yt(c).json()["id"]
    before = _snapshot(eng)
    r = {
        "sign": lambda: c.post("/storage/sign", headers=h, json={"tenant": "david", "scope": SCOPE,
                                                                 "ids": [own], "ttl": 60}),
        "delete": lambda: c.delete(f"/storage/{own}", headers=h),
        "delete_objects": lambda: c.delete(f"/storage/objects/{own}", headers=h),
        "grant": lambda: c.post(f"/storage/objects/{own}/grants", headers=h, json={"scope": AGENT}),
        "share": lambda: c.post("/storage/grants/share", headers=h,
                                json={"tenant": "david", "object_id": oid, "from_scope": AGENT, "to_scopes": [SCOPE]}),
        "media": lambda: c.get(f"/storage/media/{oid}", headers=h),
    }[call]()
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "route_profile_denied"
    assert _snapshot(eng) == before


def _foreign(eng, **kw):
    from sqlalchemy.orm import Session
    with Session(eng) as s:
        owner = s.query(User).filter(User.email == OWNER).one()
        obj = StorageObject(object_key=kw.pop("key"), tenant_id="david", owner_user_id=owner.id,
                            mime_type="application/json", **kw)
        s.add(obj)
        s.commit()
        return obj.id


def test_list_shows_only_its_own_objects(env):
    c, private_foreign, eng = env
    own = _yt(c, filename="dQw4w9WgXcQ - t.mp3", collection_id="youtube-mp3", reuse_existing="false").json()["id"]
    public_foreign = _foreign(eng, key="p.mp3", is_public=True, collection_id="youtube-mp3",
                              original_filename="dQw4w9WgXcQ - p.mp3", metadata_json={"tenant_id": "david"})
    h = {"X-API-KEY": YOUTUBE}
    for params in ({}, {"mine": "false"}, {"collection_id": "youtube-mp3"},
                   {"collection_id": "youtube-mp3", "name": "dQw4w9WgXcQ", "limit": 1},
                   {"collection_like": "youtube"}, {"link_id": "job1"}, {"search": "dQw4w9WgXcQ"}):
        r = c.get("/storage/list", headers=h, params=params)
        assert r.status_code == 200, (params, r.text)
        ids = {o["id"] for o in r.json()["items"]}
        assert ids <= {own}, params
        assert public_foreign not in ids and private_foreign not in ids
    hit = c.get("/storage/list", headers=h, params={"collection_id": "youtube-mp3", "name": "dQw4w9WgXcQ"}).json()
    assert [o["id"] for o in hit["items"]] == [own]
    assert hit["items"][0]["is_public"] is False
    for oid in (public_foreign, private_foreign):
        assert c.get("/storage/list", headers=h, params={"id": oid}).json()["items"] == []


def test_reuse_existing_only_ever_matches_its_own_object(env):
    c, _, eng = env
    name = "shared-name.json"
    foreign = _foreign(eng, key="f.json", is_public=False, original_filename=name,
                       metadata_json={"tenant_id": "david", "private_media": True})
    first = _yt(c, filename=name)
    assert first.status_code == 200, first.text
    assert first.json()["id"] != foreign
    second = _yt(c, filename=name)
    assert second.status_code == 200, second.text
    assert second.json()["id"] == first.json()["id"]
    assert _row(eng, foreign)[2] == OWNER
