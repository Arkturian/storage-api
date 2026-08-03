from fastapi import HTTPException, Header, Depends
from sqlalchemy.orm import Session
from passlib.context import CryptContext
from datetime import datetime
import secrets
import string
from typing import Optional

from database import get_db
import logging

logger = logging.getLogger(__name__)
from models import User
from config import settings
from tenancy.config import tenant_id_for_api_key

# Password hashing
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against its hash"""
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password: str) -> str:
    """Hash a password"""
    return pwd_context.hash(password)

def generate_api_key() -> str:
    """Generate a random API key"""
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(32))

def verify_api_key(api_key: str, db: Session) -> Optional[User]:
    """Verify API key and return user"""
    if not api_key:
        return None

    # Check if it's the master API key (existing system)
    if api_key == settings.API_KEY:
        # Ensure a persistent 'system' admin user exists to satisfy FK constraints
        system_email = "system@api"
        user = db.query(User).filter(User.email == system_email).first()
        if not user:
            # Create a real DB user entry with admin trust level
            user = User(
                email=system_email,
                display_name="System",
                password_hash="",  # not used
                api_key=generate_api_key(),  # distinct from master key
                trust_level="admin",
                device_ids=[],
            )
            db.add(user)
            db.commit()
            db.refresh(user)
        # Update last active and return
        user.last_active_at = datetime.utcnow()
        db.commit()
        return user

    # Check user API keys
    user = db.query(User).filter(User.api_key == api_key).first()
    if user:
        # Update last active time
        user.last_active_at = datetime.utcnow()
        db.commit()
        return user

    # Auto-provision user accounts for API keys defined via tenant config
    tenant_id = tenant_id_for_api_key(api_key, db)
    if tenant_id:
        email = f"{tenant_id}@api"
        display_name = tenant_id.replace("_", " ").title()
        try:
            user = User(
                email=email,
                display_name=display_name,
                password_hash="",
                api_key=api_key,
                # NOT admin: tenant keys ship in public browser bundles, so an
                # admin bypass would hand every confidential object to anyone
                # who reads a frontend's JS. Tenant scoping happens via
                # tenant_id, not trust level.
                trust_level="user",
                device_ids=[],
            )
            db.add(user)
            db.commit()
            db.refresh(user)
            return user
        except Exception:
            db.rollback()
            user = db.query(User).filter(User.email == email).first()
            if user:
                try:
                    user.api_key = api_key
                    user.last_active_at = datetime.utcnow()
                    db.commit()
                    db.refresh(user)
                    return user
                except Exception:
                    db.rollback()
            user = db.query(User).filter(User.api_key == api_key).first()
            if user:
                return user

    return None



# --- X-On-Behalf-Of: service keys acting for a verified user (3DPresenter BFF) ---
# Contract agreed with 3DPresenter-Codex (Post 4437 q-7db4946ae2fd, 2026-08-03):
#   * ONLY a key explicitly flagged is_service in tenant_api_keys may set the
#     header. Master, admin or plain tenant keys alone are NOT enough.
#   * The value is a canonical e-mail (Storage provisions/resolves owners by
#     e-mail today). Unknown or inactive -> fail closed with 403.
#   * The resolved user authorises the request COMPLETELY — and never inherits
#     the service key's admin rights (see _ON_BEHALF_FLAG below).
#   * Presenting the header with a non-service key -> 403, never a silent ignore.
_ON_BEHALF_FLAG = "_storage_on_behalf_of"


def _key_is_service(api_key: str, db: Session) -> bool:
    """True only for a tenant key explicitly marked is_service."""
    if not api_key:
        return False
    try:
        from models import TenantAPIKey
        row = (
            db.query(TenantAPIKey)
            .filter(TenantAPIKey.api_key == api_key, TenantAPIKey.is_active.is_(True))
            .one_or_none()
        )
        return bool(row and getattr(row, "is_service", False))
    except Exception:
        return False


def resolve_on_behalf_of(api_key: Optional[str], on_behalf: Optional[str], db: Session) -> Optional[User]:
    """Resolve X-On-Behalf-Of to the acting user, or raise 403.

    Returns None when the header was not supplied at all.
    """
    if not on_behalf:
        return None
    value = on_behalf.strip()
    if not value:
        return None
    if not _key_is_service(api_key, db):
        raise HTTPException(
            status_code=403,
            detail={"error": "X-On-Behalf-Of requires a service key", "code": "not_a_service_key"},
        )
    user = db.query(User).filter(User.email == value).first()
    if not user:
        # Fail closed: never auto-provision from a header, that would let a
        # service key mint identities.
        raise HTTPException(
            status_code=403,
            detail={"error": "X-On-Behalf-Of user unknown", "code": "unknown_principal"},
        )
    # The acting user must not inherit admin powers from the service key, and an
    # admin principal must not be impersonated into admin-wide operations either.
    setattr(user, _ON_BEHALF_FLAG, True)
    logger.warning(
        "on-behalf-of: service_key=%s… acting_as=%s (user_id=%s)",
        (api_key or "")[:6], user.email, user.id,
    )
    return user


def get_current_user(
    api_key: str = Header(None, alias="X-API-KEY"),
    on_behalf_of: str = Header(None, alias="X-On-Behalf-Of"),
    db: Session = Depends(get_db)
) -> User:
    """Get current user from API key (or the principal a service key acts for)"""
    acting = resolve_on_behalf_of(api_key, on_behalf_of, db)
    if acting is not None:
        return acting
    user = verify_api_key(api_key, db)
    if not user:
        raise HTTPException(
            status_code=401,
            detail="Invalid API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user

def get_current_user_optional(
    api_key: str = Header(None, alias="X-API-KEY"),
    on_behalf_of: str = Header(None, alias="X-On-Behalf-Of"),
    db: Session = Depends(get_db)
) -> Optional[User]:
    """Get current user from API key (optional - returns None if not authenticated).

    An X-On-Behalf-Of header is still validated strictly: a non-service key
    presenting it gets 403 rather than being silently downgraded to anonymous.
    """
    acting = resolve_on_behalf_of(api_key, on_behalf_of, db)
    if acting is not None:
        return acting
    return verify_api_key(api_key, db)

def get_user_by_email(db: Session, email: str) -> Optional[User]:
    """Get user by email"""
    return db.query(User).filter(User.email == email).first()
