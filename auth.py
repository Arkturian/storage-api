from fastapi import HTTPException, Header, Depends
from sqlalchemy.orm import Session
from passlib.context import CryptContext
from datetime import datetime
import secrets
import string
from typing import Optional

from database import get_db
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

def get_current_user(
    api_key: str = Header(None, alias="X-API-KEY"),
    db: Session = Depends(get_db)
) -> User:
    """Get current user from API key"""
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
    db: Session = Depends(get_db)
) -> Optional[User]:
    """Get current user from API key (optional - returns None if not authenticated)"""
    return verify_api_key(api_key, db)

def get_user_by_email(db: Session, email: str) -> Optional[User]:
    """Get user by email"""
    return db.query(User).filter(User.email == email).first()
