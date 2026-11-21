"""
Tenant configuration backed by the relational database.

This module keeps backwards compatibility with the previous JSON-based approach
by mirroring changes to ``tenant_config.json`` so that legacy tooling continues
to work, while the database is treated as the canonical source of truth.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Optional

from fastapi import Header, Depends
from sqlalchemy.orm import Session

from database import SessionLocal, get_db
from models import Tenant, TenantAPIKey


TENANT_CONFIG_PATH = Path(os.getenv("TENANT_CONFIG_PATH", "./tenant_config.json"))
DEFAULT_TENANT_ID = os.getenv("DEFAULT_TENANT_ID", "arkturian")
# #3597: tenant names only. The keys that stood here were public (bundles,
# repos) and the bootstrap re-activated them on every start.
_DEFAULT_TENANTS = ("arkturian", "oneal")
_LEGACY_SNAPSHOT_ENABLED = os.getenv("TENANT_LEGACY_SNAPSHOT", "0") == "1"
_LEGACY_IMPORT_ENABLED = os.getenv("TENANT_LEGACY_IMPORT", "0") == "1"


def _load_legacy_map() -> Dict[str, str]:
    """Load the legacy JSON mapping (without injecting defaults)."""
    try:
        if TENANT_CONFIG_PATH.exists():
            with open(TENANT_CONFIG_PATH, "r") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
    except Exception:
        pass
    return {}


def _write_legacy_map(mapping: Dict[str, str]) -> None:
    """Persist the mapping to the legacy JSON file."""
    try:
        TENANT_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    try:
        with open(TENANT_CONFIG_PATH, "w") as f:
            json.dump(mapping, f, indent=2, sort_keys=True)
    except Exception as exc:
        print(f"Warning: Failed to persist tenant_config.json: {exc}")


@contextmanager
def _manage_session(db: Optional[Session] = None):
    if db is not None:
        yield db
        return
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _ensure_tenant(
    db: Session,
    tenant_id: str,
    *,
    display_name: Optional[str] = None,
    is_system: bool = False,
) -> Tenant:
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).one_or_none()
    if tenant:
        if display_name and not tenant.display_name:
            tenant.display_name = display_name
        if is_system and not tenant.is_system:
            tenant.is_system = True
        if not tenant.is_active:
            tenant.is_active = True
        return tenant

    tenant = Tenant(
        id=tenant_id,
        display_name=display_name or tenant_id.replace("_", " ").title(),
        is_active=True,
        is_system=is_system,
    )
    db.add(tenant)
    db.flush()
    return tenant


def _persist_legacy_snapshot(db: Session) -> None:
    """Mirror the DB state back into the legacy JSON file (best effort).

    Off by default (#3597, as #2911 on the main line): it wrote every active
    key in plain text, and the next start imported them back."""
    if not _LEGACY_SNAPSHOT_ENABLED:
        return
    try:
        mapping = list_tenant_keys(db)
        _write_legacy_map(mapping)
    except Exception as exc:
        print(f"Warning: Failed to write tenant_config.json snapshot: {exc}")


def tenant_id_for_api_key(api_key: Optional[str], db: Optional[Session] = None) -> Optional[str]:
    if not api_key:
        return None

    with _manage_session(db) as session:
        key = (
            session.query(TenantAPIKey)
            .filter(
                TenantAPIKey.api_key == api_key,
                TenantAPIKey.is_active.is_(True),
            )
            .one_or_none()
        )
        return key.tenant_id if key else None


def list_tenant_keys(db: Optional[Session] = None) -> Dict[str, str]:
    with _manage_session(db) as session:
        rows = (
            session.query(TenantAPIKey.api_key, TenantAPIKey.tenant_id)
            .filter(TenantAPIKey.is_active.is_(True))
            .all()
        )
        mapping = {api_key: tenant_id for api_key, tenant_id in rows}
        return mapping


def api_key_for_tenant(tenant_id: str, db: Optional[Session] = None) -> Optional[str]:
    if not tenant_id:
        return None
    with _manage_session(db) as session:
        tenant = session.query(Tenant).filter(Tenant.id == tenant_id).one_or_none()
        if not tenant:
            return None
        if tenant.default_api_key:
            return tenant.default_api_key
        key = (
            session.query(TenantAPIKey.api_key)
            .filter(
                TenantAPIKey.tenant_id == tenant_id,
                TenantAPIKey.is_active.is_(True),
            )
            .first()
        )
        return key[0] if key else None


def upsert_tenant_key(
    api_key: str,
    tenant_id: str,
    *,
    label: Optional[str] = None,
    db: Optional[Session] = None,
) -> Dict[str, str]:
    api_key = str(api_key).strip()
    tenant_id = str(tenant_id).strip().lower()
    if not api_key or not tenant_id:
        raise ValueError("api_key and tenant_id are required")

    with _manage_session(db) as session:
        try:
            tenant = _ensure_tenant(session, tenant_id)

            record = session.query(TenantAPIKey).filter(TenantAPIKey.api_key == api_key).one_or_none()
            if record:
                record.tenant_id = tenant_id
                record.is_active = True
                if label is not None:
                    record.label = label
            else:
                record = TenantAPIKey(api_key=api_key, tenant_id=tenant_id, label=label)
                session.add(record)

            if not tenant.default_api_key:
                tenant.default_api_key = api_key

            session.commit()
            _persist_legacy_snapshot(session)
        except Exception:
            session.rollback()
            raise

    return list_tenant_keys(db)


def delete_tenant_key(api_key: str, db: Optional[Session] = None) -> Dict[str, str]:
    api_key = str(api_key).strip()
    if not api_key:
        return list_tenant_keys(db)

    with _manage_session(db) as session:
        try:
            record = (
                session.query(TenantAPIKey)
                .filter(TenantAPIKey.api_key == api_key)
                .one_or_none()
            )
            if record:
                tenant = record.tenant
                session.delete(record)
                session.flush()
                if tenant and tenant.default_api_key == api_key:
                    tenant.default_api_key = None
            session.commit()
            _persist_legacy_snapshot(session)
        except Exception:
            session.rollback()
            raise

    return list_tenant_keys(db)


def bootstrap_tenant_registry() -> None:
    """Ensure default tenants exist; never create or re-activate keys (#3597).

    The old version seeded two fixed keys and set them active on every start,
    and re-imported tenant_config.json with upsert (is_active=True). A key
    switched off in the DB came back with the next restart."""
    session = SessionLocal()
    try:
        for tenant_id in _DEFAULT_TENANTS:
            _ensure_tenant(session, tenant_id, is_system=True)

        # Import from legacy JSON only when explicitly enabled.
        if _LEGACY_IMPORT_ENABLED:
            for api_key, tenant_id in _load_legacy_map().items():
                upsert_tenant_key(api_key, tenant_id, db=session)

        session.commit()
        _persist_legacy_snapshot(session)
    except Exception as exc:
        session.rollback()
        print(f"Warning: Tenant bootstrap failed: {exc}")
    finally:
        session.close()


def sync_legacy_snapshot(db: Optional[Session] = None) -> None:
    """Persist the current tenant-key mapping into the legacy JSON file."""
    with _manage_session(db) as session:
        _persist_legacy_snapshot(session)


def get_tenant_id(
    api_key: str = Header(None, alias="X-API-KEY"),
    db: Session = Depends(get_db),
) -> str:
    """
    FastAPI dependency: resolve tenant identifier from API key.
    Defaults to 'arkturian' for backwards compatibility.
    """
    tenant_id = tenant_id_for_api_key(api_key, db)
    return tenant_id or DEFAULT_TENANT_ID


def get_tenant_id_optional(
    api_key: str = Header(None, alias="X-API-KEY"),
    db: Session = Depends(get_db),
) -> Optional[str]:
    """
    FastAPI dependency: resolve optional tenant identifier.
    Returns None when no API key was provided.
    """
    return tenant_id_for_api_key(api_key, db) or DEFAULT_TENANT_ID
