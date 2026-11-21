"""#3597 (aiserver hotfix on e25cb2d): a tenant key switched off in the DB stays
off across a restart.

The old bootstrap seeded two fixed keys from _DEFAULT_MAP and set them active
on every start, and re-imported tenant_config.json with upsert (is_active=True),
so switching a leaked key off only lasted until the next restart."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import tenancy.config as tc  # noqa: E402
from models import Base, Tenant, TenantAPIKey  # noqa: E402

OFF_KEY = "switched-off-key-0001"
ON_KEY = "still-active-key-0002"


@pytest.fixture
def Session(tmp_path, monkeypatch):
    eng = create_engine(f"sqlite:///{tmp_path/'t.db'}")
    Base.metadata.create_all(eng)
    S = sessionmaker(bind=eng)
    s = S()
    s.add_all([Tenant(id="oneal", is_active=True, default_api_key=None),
               Tenant(id="gsg", is_active=True, default_api_key=ON_KEY)])
    s.add_all([TenantAPIKey(api_key=OFF_KEY, tenant_id="oneal", is_active=False),
               TenantAPIKey(api_key=ON_KEY, tenant_id="gsg", is_active=True)])
    s.commit()
    s.close()
    monkeypatch.setattr(tc, "SessionLocal", S)
    # A legacy snapshot that still lists the switched-off key, as on aiserver.
    snap = tmp_path / "tenant_config.json"
    snap.write_text(json.dumps({OFF_KEY: "oneal", ON_KEY: "gsg"}))
    monkeypatch.setattr(tc, "TENANT_CONFIG_PATH", snap)
    monkeypatch.setattr(tc, "_LEGACY_IMPORT_ENABLED", False, raising=False)
    monkeypatch.setattr(tc, "_LEGACY_SNAPSHOT_ENABLED", False, raising=False)
    return S


def _state(S):
    s = S()
    try:
        keys = {k.api_key: bool(k.is_active) for k in s.query(TenantAPIKey).all()}
        defaults = {t.id: t.default_api_key for t in s.query(Tenant).all()}
        return keys, defaults
    finally:
        s.close()


def test_switched_off_key_stays_off_after_restart(Session):
    tc.bootstrap_tenant_registry()
    tc.bootstrap_tenant_registry()  # second start
    keys, defaults = _state(Session)
    assert keys[OFF_KEY] is False
    assert defaults["oneal"] is None          # no default re-seeded
    assert tc.tenant_id_for_api_key(OFF_KEY) is None


def test_bootstrap_creates_no_keys(Session):
    before, _ = _state(Session)
    tc.bootstrap_tenant_registry()
    after, _ = _state(Session)
    assert set(after) == set(before)          # nothing seeded from code
    assert after[ON_KEY] is True              # active keys untouched


def test_default_tenants_exist_without_keys(Session):
    tc.bootstrap_tenant_registry()
    _, defaults = _state(Session)
    assert "arkturian" in defaults and "oneal" in defaults


def test_no_fixed_keys_in_code():
    assert not hasattr(tc, "_DEFAULT_MAP")
    assert all(isinstance(t, str) for t in tc._DEFAULT_TENANTS)


def test_snapshot_not_written_by_default(Session):
    tc.bootstrap_tenant_registry()
    data = json.loads(tc.TENANT_CONFIG_PATH.read_text())
    assert data == {OFF_KEY: "oneal", ON_KEY: "gsg"}   # untouched, not rewritten
