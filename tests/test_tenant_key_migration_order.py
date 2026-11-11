"""tenant_api_keys-Spalten kommen VOR dem Tenant-Bootstrap dazu (P3, 11.11.2026).

Vorher lief bootstrap_tenant_registry() in connect_db(), die ALTERs erst danach in
main.startup(): auf einer alten Datenbank scheiterte der erste Start mit
"no such column tenant_api_keys.sign_scopes".
"""
import asyncio
import sys
from pathlib import Path

from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import database  # noqa: E402
import tenancy.config  # noqa: E402

ALT = (
    "CREATE TABLE tenant_api_keys (id INTEGER PRIMARY KEY, api_key VARCHAR, "
    "tenant_id VARCHAR, label VARCHAR, is_active BOOLEAN, created_at DATETIME)"
)


def _cols(eng):
    with eng.connect() as c:
        return {r[1] for r in c.execute(text("PRAGMA table_info(tenant_api_keys)"))}


def test_spalten_vor_dem_bootstrap(tmp_path, monkeypatch):
    eng = create_engine(f"sqlite:///{tmp_path/'alt.db'}")
    with eng.connect() as c:
        c.execute(text(ALT))
        c.commit()

    gesehen = {}

    async def _connect():
        return None

    monkeypatch.setattr(database, "engine", eng)
    monkeypatch.setattr(database.database, "connect", _connect)
    monkeypatch.setattr(database, "create_tables", lambda: None)
    monkeypatch.setattr(database.settings, "DATABASE_URL", f"sqlite:///{tmp_path/'alt.db'}")
    monkeypatch.setattr(tenancy.config, "bootstrap_tenant_registry", lambda: gesehen.update(cols=_cols(eng)))

    asyncio.run(database.connect_db())

    assert {"is_service", "sign_tenants", "sign_scopes"} <= gesehen["cols"]


def test_migration_idempotent_und_ohne_tabelle(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path/'x.db'}")
    with eng.connect() as c:
        database.migrate_tenant_api_keys(c)  # keine Tabelle: nichts tun, kein Fehler
        c.execute(text(ALT))
        database.migrate_tenant_api_keys(c)
        database.migrate_tenant_api_keys(c)
        c.commit()
    assert {"is_service", "sign_tenants", "sign_scopes"} <= _cols(eng)
