from sqlalchemy import Column, Integer, String, Boolean, Float, Text, DateTime, ForeignKey, JSON, Index
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
from datetime import datetime
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from enum import Enum

Base = declarative_base()


class Tenant(Base):
    __tablename__ = "tenants"

    id = Column(String(50), primary_key=True)
    display_name = Column(String(255), nullable=True)
    description = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True)
    is_system = Column(Boolean, default=False)
    default_api_key = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    api_keys = relationship("TenantAPIKey", back_populates="tenant", cascade="all, delete-orphan")


class TenantAPIKey(Base):
    __tablename__ = "tenant_api_keys"

    api_key = Column(String, primary_key=True)
    tenant_id = Column(String(50), ForeignKey("tenants.id"), index=True, nullable=False)
    label = Column(String(255), nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_used_at = Column(DateTime, nullable=True)

    tenant = relationship("Tenant", back_populates="api_keys")


# SQLAlchemy Models
class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True)
    display_name = Column(String)
    password_hash = Column(String)
    api_key = Column(String, unique=True, index=True)
    trust_level = Column(String, default="new_user")  # new_user, trusted, moderator, admin
    device_ids = Column(JSON, default=list)  # List of device IDs

    # Quotas
    storage_bytes_used = Column(Integer, default=0)
    storage_bytes_limit = Column(Integer, default=5368709120)  # 5GB
    uploads_this_month = Column(Integer, default=0)
    upload_limit_per_month = Column(Integer, default=1000)

    created_at = Column(DateTime, default=datetime.utcnow)
    last_active_at = Column(DateTime, default=datetime.utcnow)


class StorageObject(Base):
    __tablename__ = "storage_objects"

    id = Column(Integer, primary_key=True, index=True)
    owner_user_id = Column(Integer, ForeignKey("users.id"), index=True)
    object_key = Column(String, unique=True, index=True)
    original_filename = Column(String)
    file_url = Column(String)
    thumbnail_url = Column(String, nullable=True)
    webview_url = Column(String, nullable=True)  # Web-optimized image URL
    mime_type = Column(String)
    file_size_bytes = Column(Integer)
    checksum = Column(String)
    is_public = Column(Boolean, default=False)
    context = Column(String, nullable=True)
    collection_id = Column(String, nullable=True)
    link_id = Column(String, nullable=True, index=True)  # For linking related files together
    title = Column(Text, nullable=True)
    description = Column(Text, nullable=True)
    likes = Column(Integer, default=0)
    width = Column(Integer, nullable=True)
    height = Column(Integer, nullable=True)
    duration_seconds = Column(Float, nullable=True)
    bit_rate = Column(Integer, nullable=True)
    latitude = Column(Float, nullable=True, index=True)
    longitude = Column(Float, nullable=True, index=True)
    ai_safety_rating = Column(String, nullable=True)
    metadata_json = Column(JSON, default=dict)
    download_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    expires_at = Column(DateTime, nullable=True, index=True)  # TTL: NULL = never expires

    # AI Analysis Fields
    ai_category = Column(String, nullable=True)
    ai_danger_potential = Column(Integer, nullable=True)  # e.g., 1-10 scale

    # Enhanced AI Metadata Fields (v2.0)
    ai_title = Column(String(500), nullable=True)  # AI-generated title
    ai_subtitle = Column(Text, nullable=True)      # Instagram-style subtitle
    ai_tags = Column(JSON, nullable=True)          # Array of tags
    ai_collections = Column(JSON, nullable=True)   # Array of collection suggestions
    safety_info = Column(JSON, nullable=True)      # Safety check results

    # HLS Transcoding Fields
    hls_url = Column(String, nullable=True)
    transcoding_status = Column(String, nullable=True)  # pending, processing, completed, failed
    transcoding_progress = Column(Integer, nullable=True)  # 0-100
    transcoding_error = Column(Text, nullable=True)
    transcoded_file_size_bytes = Column(Integer, nullable=True)
    ai_safety_status = Column(String, nullable=True)
    ai_safety_error = Column(Text, nullable=True)

    # Storage Mode Fields (v3.0)
    storage_mode = Column(String, default="copy")  # "copy", "reference", or "external"
    reference_path = Column(String, nullable=True, index=True)  # Filesystem path when using reference mode
    external_uri = Column(String, nullable=True, index=True)  # External web URI when using external mode
    ai_context_metadata = Column(JSON, nullable=True)  # Context for AI analysis (file_path, semantic hints, etc.)

    # Multi-Tenancy (v4.0)
    tenant_id = Column(String(50), default="arkturian", index=True)  # Tenant identifier for multi-tenancy

    # Two-Phase-Delete / Cascade-Delete (v5.0) — soft-delete tombstone. When set,
    # the object is treated as gone (media endpoint 404s) but bytes stay intact
    # so a SWFME cascade-delete saga can untombstone (compensate) before the
    # final hard-delete. NULL = live.
    tombstoned_at = Column(DateTime, nullable=True, index=True)


class AsyncTask(Base):
    __tablename__ = "async_tasks"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(String, unique=True, index=True)
    object_id = Column(Integer, ForeignKey("storage_objects.id"), index=True)
    status = Column(String, default="queued")  # queued, processing, completed, failed
    mode = Column(String, default="quality")  # fast, quality
    current_phase = Column(String, nullable=True)  # safety_check, ai_analysis, building_knowledge_graph, etc.
    progress = Column(Integer, default=0)  # 0-100
    error = Column(Text, nullable=True)
    result = Column(Text, nullable=True)  # JSON string
    created_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)


# Pydantic Models for API
class StorageObjectResponse(BaseModel):
    id: int
    owner_user_id: Optional[int] = None  # Some legacy objects have no owner
    object_key: str
    original_filename: str
    file_url: Optional[str] = None
    thumbnail_url: Optional[str] = None
    webview_url: Optional[str] = None
    mime_type: str
    file_size_bytes: int
    checksum: Optional[str] = None
    is_public: bool
    context: Optional[str] = None
    collection_id: Optional[str] = None
    link_id: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    likes: int
    width: Optional[int] = None
    height: Optional[int] = None
    duration_seconds: Optional[float] = None
    bit_rate: Optional[int] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    ai_safety_rating: Optional[str] = None
    metadata_json: Optional[Dict[str, Any]] = None
    download_count: int
    created_at: datetime
    updated_at: datetime
    expires_at: Optional[datetime] = None  # TTL: NULL = never expires
    ai_category: Optional[str] = None
    ai_danger_potential: Optional[int] = None
    ai_title: Optional[str] = None
    ai_subtitle: Optional[str] = None
    ai_tags: Optional[List[str]] = None
    ai_collections: Optional[List[str]] = None
    safety_info: Optional[Dict[str, Any]] = None
    hls_url: Optional[str] = None
    transcoding_status: Optional[str] = None
    transcoding_progress: Optional[int] = None
    transcoding_error: Optional[str] = None
    transcoded_file_size_bytes: Optional[int] = None
    ai_safety_status: Optional[str] = None
    ai_safety_error: Optional[str] = None
    storage_mode: str = "copy"
    reference_path: Optional[str] = None
    external_uri: Optional[str] = None
    ai_context_metadata: Optional[Dict[str, Any]] = None
    tenant_id: str = "arkturian"
    owner_email: Optional[str] = None  # Populated from join in search queries
    expires_at: Optional[datetime] = None  # TTL: NULL = never expires, future datetime = scheduled purge

    class Config:
        from_attributes = True


class StorageListResponse(BaseModel):
    items: List[StorageObjectResponse]
    total: int
    limit: int
    offset: int


class UserResponse(BaseModel):
    id: int
    email: str
    display_name: str
    trust_level: str
    created_at: datetime

    class Config:
        from_attributes = True
