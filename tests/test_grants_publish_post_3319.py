"""#3319: an agent binds its own private file to a content post it writes.

content-api (key flagged may_publish_to_post) has checked the agent's JWT and
its write right on the post, then calls /grants/share from the agent's scope
to exactly one content-post:N. Received files, encrypted files, human scopes
and every other cross-family pair stay refused."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import main  # noqa: E402
import models  # noqa: E402
from database import get_db  # noqa: E402
from models import MediaGrant, StorageObject, TenantAPIKey  # noqa: E402
from storage import routes as storage_routes  # noqa: E402

CONTENT = "k-content-test"
CLOUD = "k-cloud-test"
AGENT = "cloud-session:CloudV2-time"
AGENT_X = "cloud-session-x:4845524d494e45"
OTHER = "cloud-session:Business"
POST = "content-post:6228"


@pytest.fixture
def env(tmp_path):
    models._hwm_ready.clear()
    eng = create_engine(f"sqlite:///{tmp_path/'s.db'}")
    models.Base.metadata.create_all(eng, tables=[StorageObject.__table__, MediaGrant.__table__, TenantAPIKey.__table__])
    S = sessionmaker(bind=eng)
    s = S()
    s.add_all([TenantAPIKey(api_key=CONTENT, tenant_id="arkturian", label="content", is_active=True,
                            sign_tenants="arkturian", may_publish_to_post=True),
               TenantAPIKey(api_key=CLOUD, tenant_id="arkturian", label="cloud", is_active=True,
                            sign_tenants="arkturian")])
    objs = {k: StorageObject(object_key=k, tenant_id=t, encrypted=(k == "enc"))
            for k, t in (("own", "arkturian"), ("own_x", "arkturian"), ("received", "arkturian"),
                         ("received_ro", "arkturian"), ("enc", "arkturian"), ("foreign", "oneal"),
                         ("human", "arkturian"))}
    s.add_all(objs.values()); s.commit()
    for k, scope, via in (("own", AGENT, "upload"), ("own_x", AGENT_X, "upload"),
                          ("received", OTHER, "upload"), ("received", AGENT, "share:" + OTHER),
                          ("received_ro", OTHER, "upload"), ("received_ro", AGENT, "share-ro:" + OTHER),
                          ("enc", AGENT, "upload"), ("foreign", AGENT, "upload"),
                          ("human", "cloud-user:7", "admin")):
        s.add(MediaGrant(object_id=objs[k].id, scope=scope, granted_by_user_id=1, granted_via=via))
    s.commit()
    ids = {k: o.id for k, o in objs.items()}
    s.close()

    def _db():
        d = S()
        try:
            yield d
        finally:
            d.close()

    main.app.dependency_overrides[get_db] = _db
    main.app.dependency_overrides[storage_routes.get_current_user] = lambda: SimpleNamespace(id=1)
    try:
        yield SimpleNamespace(c=TestClient(main.app), S=S, ids=ids)
    finally:
        main.app.dependency_overrides.clear()


def _share(env, obj, src=AGENT, dst=(POST,), key=CONTENT, tenant="arkturian", reshare=None):
    body = {"tenant": tenant, "object_id": env.ids[obj], "from_scope": src, "to_scopes": list(dst)}
    if reshare is not None:
        body["reshare"] = reshare
    return env.c.post("/storage/grants/share", headers={"X-API-KEY": key}, json=body)


def _via(env, obj):
    s = env.S()
    try:
        return {g.scope: g.granted_via for g in s.query(MediaGrant).filter_by(object_id=env.ids[obj])}
    finally:
        s.close()


@pytest.mark.parametrize("obj,src", [("own", AGENT), ("own_x", AGENT_X)])
def test_own_file_binds_to_post_read_only(env, obj, src):
    r = _share(env, obj, src=src, reshare=True)  # reshare is forced off
    assert r.status_code == 200 and r.json()["shared"] == [POST]
    assert _via(env, obj)[POST] == "share-ro:" + src
    r = _share(env, obj, src=src)
    assert r.status_code == 200 and r.json()["already"] == [POST]


def test_post_cannot_pass_it_on(env):
    assert _share(env, "own").status_code == 200
    r = _share(env, "own", src=POST, dst=("content-post:1",))
    assert r.status_code == 403 and r.json()["detail"]["code"] == "reshare_not_allowed"


@pytest.mark.parametrize("obj", ["received", "received_ro"])
def test_received_file_refused(env, obj):
    r = _share(env, obj)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "not_own_file"
    assert POST not in _via(env, obj)


def test_unbound_agent_refused(env):
    r = _share(env, "own", src="cloud-session:Fremd")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "not_granted"


def test_encrypted_refused(env):
    r = _share(env, "enc")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "encrypted_publish_not_allowed"
    assert POST not in _via(env, "enc")


def test_object_of_other_tenant_is_404(env):
    assert _share(env, "foreign").status_code == 404
    assert POST not in _via(env, "foreign")


def test_key_without_flag_stays_cross_family(env):
    r = _share(env, "own", key=CLOUD)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "cross_family"


@pytest.mark.parametrize("src,dst", [
    ("cloud-user:7", (POST,)),                       # a human scope is not an agent
    (AGENT, (POST, "content-post:2")),               # exactly one post
    (AGENT, (POST, OTHER)),                          # no mixing
    (AGENT, ("comm-message:1",)),                    # only content-post
    (POST, (AGENT,)),                                # one direction only
])
def test_other_cross_family_pairs_refused(env, src, dst):
    obj = "human" if src.startswith("cloud-user") else "own"
    r = _share(env, obj, src=src, dst=dst)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "cross_family"


def test_unshare_by_agent_removes_post_binding(env):
    assert _share(env, "own").status_code == 200
    r = env.c.post("/storage/grants/unshare", headers={"X-API-KEY": CONTENT},
                   json={"tenant": "arkturian", "object_id": env.ids["own"], "from_scope": AGENT, "scopes": [POST]})
    assert r.status_code == 200 and r.json()["revoked"] == [POST]
    assert POST not in _via(env, "own")


def test_agent_rename_keeps_revocable(env):
    assert _share(env, "own").status_code == 200
    r = env.c.post("/storage/grants/move", headers={"X-API-KEY": CONTENT},
                   json={"tenant": "arkturian", "from_scope": AGENT, "to_scope": "cloud-session:CloudV2-zeit"})
    assert r.status_code == 200
    assert _via(env, "own")[POST] == "share-ro:cloud-session:CloudV2-zeit"
