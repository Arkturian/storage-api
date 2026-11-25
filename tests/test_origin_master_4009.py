"""#4009 (a): customer instances upload with the storage-api master key.

X-Caller-Tenant is believed from the instance's own master key (full operator
anyway), never from the old public default value (#3969) and never from a
plain key without may_assert_origin / may_assert_scope.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import tests.test_tenant_wall_4007 as base  # noqa: E402
from tests.test_tenant_wall_4007 import _origin, _upload  # noqa: E402

env = base.env
MASTER = "test-master-4009-not-public"


@pytest.fixture
def master(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "API_KEY", MASTER)
    return MASTER


def test_master_with_header_marks_origin(env, master):
    c, eng, _ = env
    r = _upload(c, {"X-API-KEY": master, "X-Caller-Tenant": "steiner"}, "m1.bin")
    assert r.status_code == 200, r.text
    assert _origin(eng, r.json()["id"]) == "steiner"
    assert r.json()["origin_tenant"] == "steiner"


def test_master_without_header_marks_nothing(env, master):
    c, eng, _ = env
    r = _upload(c, {"X-API-KEY": master}, "m2.bin")
    assert r.status_code == 200, r.text
    assert _origin(eng, r.json()["id"]) is None


def test_master_with_malformed_header_marks_nothing(env, master):
    c, eng, _ = env
    r = _upload(c, {"X-API-KEY": master, "X-Caller-Tenant": "Not A Tenant!"}, "m3.bin")
    assert r.status_code == 200, r.text
    assert _origin(eng, r.json()["id"]) is None


def test_plain_key_still_cannot_mark(env, master):
    c, eng, _ = env
    r = _upload(c, {"X-API-KEY": base.PLAIN, "X-Caller-Tenant": "steiner"}, "m4.bin")
    assert r.status_code == 200, r.text
    assert _origin(eng, r.json()["id"]) is None


def test_public_default_as_master_is_not_believed(env, monkeypatch):
    """Counter-probe: an instance still running the old public default gets no mark."""
    from config import settings
    from tests.public_default_fixture import PUBLIC_DEFAULT_TEST
    monkeypatch.setattr(settings, "API_KEY", PUBLIC_DEFAULT_TEST)
    from storage import origin_tenant
    assert origin_tenant._is_master(PUBLIC_DEFAULT_TEST) is False
    assert origin_tenant._is_master("something-else") is False


def test_master_builds_no_wall(env, master):
    """The master key has no may_assert_scope row: listings stay as before."""
    c, _, ids = env
    h = {"X-API-KEY": master, "X-Caller-Tenant": "lesser",
         "X-Grant-Scope-Restrict": "cloud-session:Blender", "X-Grant-Scope-Tenants": "lesser"}
    got = base.listed(c, h, ids)
    assert {"op_pub", "ark_pub", "edera_pub"} <= got
