"""
Storage API - Media Storage & AI Analysis Service

Handles:
- Object Storage (files, images, videos)
- Media Transformation (resize, format conversion)
- AI Analysis (Gemini Vision API)
- Knowledge Graph (embeddings, similarity search)
- Multi-Tenancy (tenant isolation)
"""

# Configure logging FIRST - before any other imports
import logging
import sys

# Configure root logger to output to stderr (captured by gunicorn error log)
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stderr)]
)

# Also configure uvicorn loggers
logging.getLogger("uvicorn.error").setLevel(logging.DEBUG)
logging.getLogger("uvicorn.access").setLevel(logging.INFO)

import os
import re
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from storage import routes as storage_routes
from tenancy import routes as tenant_routes
from admin import routes as admin_routes
from database import connect_db, disconnect_db, SessionLocal
from config import settings
from models import StorageObject

app = FastAPI(
    title="Storage API",
    version="1.0.0",
    description="Media storage and AI analysis service with multi-tenancy support"
)

# Database lifecycle events
@app.on_event("startup")
async def startup():
    await connect_db()
    # Idempotent schema migration (no Alembic): add the tombstoned_at column for
    # two-phase / cascade delete if it doesn't exist yet. SQLite ALTER ADD COLUMN.
    try:
        from database import engine
        from sqlalchemy import text as _sql_text
        with engine.connect() as _conn:
            _cols = [row[1] for row in _conn.execute(_sql_text("PRAGMA table_info(storage_objects)")).fetchall()]
            if "tombstoned_at" not in _cols:
                _conn.execute(_sql_text("ALTER TABLE storage_objects ADD COLUMN tombstoned_at DATETIME"))
                _conn.commit()
                print("✅ migration: added storage_objects.tombstoned_at")
    except Exception as _mig_exc:
        print(f"⚠️ tombstoned_at migration check failed: {_mig_exc}")

@app.on_event("shutdown")
async def shutdown():
    await disconnect_db()

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=[
        "Content-Length",
        "Content-Range",
        "X-HLS-URL",
        "X-Transcoding-Status",
        "X-Mime-Type",
        "X-Transcoded-Size",
    ],
)

_MEDIA_ID_RE = re.compile(r"^/storage/media/(\d+)/?$")
_EXPOSE_HEADERS = "Content-Length, Content-Range, X-HLS-URL, X-Transcoding-Status, X-Mime-Type, X-Transcoded-Size"

# Query params that change the served representation. For HEAD requests carrying
# any of these, the raw source file_size_bytes/mime is NOT the truth — reporting
# it is the "HEAD lies" bug (issue #61). GLB transforms get the real cached size
# via X-Transcoded-Size; image transforms get X-Transcoding-Status=dynamic.
_GLB_HEAD_PARAMS = ("decimate", "texture_format", "texture_quality", "texture_max_size", "mesh_compression", "output", "preset")
_IMG_HEAD_PARAMS = ("variant", "width", "height", "format", "quality", "aspect_ratio", "trim", "display_for")


def _apply_cors_for_head(headers: dict, request: Request) -> dict:
    """
    Manually attach CORS response headers because our HEAD-intercept middleware
    runs OUTER of CORSMiddleware in Starlette's stack — returning a Response
    here bypasses CORSMiddleware's send-wrap, so it never sees the response
    to inject Access-Control-Expose-Headers / Allow-Origin. We mirror the
    behavior here for HEAD /storage/media/{id} only.
    """
    origin = request.headers.get("origin")
    if origin:
        allowed = settings.CORS_ORIGINS or []
        if "*" in allowed or origin in allowed:
            headers["Access-Control-Allow-Origin"] = origin
            headers["Vary"] = "Origin"
            headers["Access-Control-Allow-Credentials"] = "true"
    headers["Access-Control-Expose-Headers"] = _EXPOSE_HEADERS
    return headers


@app.middleware("http")
async def storage_media_head_headers(request: Request, call_next):
    """
    For HEAD /storage/media/{id} requests, return a body-less response with
    X-Mime-Type, X-Transcoding-Status, X-HLS-URL headers so HLS-aware
    frontends can pick HLS vs progressive MP4 without a second round-trip.

    Bypasses FastAPI's Starlette auto-HEAD handler which strips custom
    headers from FileResponse. Runs before any route handler.
    """
    if request.method == "HEAD":
        m = _MEDIA_ID_RE.match(request.url.path)
        if m:
            object_id = int(m.group(1))
            db = SessionLocal()
            try:
                obj = db.query(StorageObject).filter(StorageObject.id == object_id).first()
            finally:
                db.close()
            if not obj:
                return Response(status_code=404, headers=_apply_cors_for_head({}, request))

            # Safety quarantine — mirror /storage/media GET behavior.
            # We don't have a current_user in middleware (auth happens in
            # the route handler), so HEAD from anonymous origins always
            # gets the public-only quarantine policy. Owner/admin clients
            # can still GET (auth via X-API-KEY in the route handler) and
            # will see the actual file.
            danger = obj.ai_danger_potential or 0
            threshold = int(os.getenv("QUARANTINE_DANGER_THRESHOLD", "7"))
            is_unsafe_blocked = (obj.ai_safety_rating == "unsafe" and danger >= threshold)
            is_pending_public = (obj.is_public and obj.ai_safety_status in ("pending", "processing", "failed"))
            if is_unsafe_blocked or is_pending_public:
                quarantine_headers = _apply_cors_for_head({}, request)
                if obj.ai_safety_status:
                    quarantine_headers["X-Transcoding-Status"] = obj.ai_safety_status
                return Response(status_code=451, headers=quarantine_headers)

            # Issue #61: for representation-changing requests, the raw source
            # size/mime is not what GET returns. Report the real transcoded size
            # for cached GLB variants (X-Transcoded-Size); never report the
            # misleading raw Content-Length for an uncached/parametrized variant.
            qp = request.query_params
            has_glb = any(k in qp for k in _GLB_HEAD_PARAMS)
            has_img = any(k in qp for k in _IMG_HEAD_PARAMS)
            if has_glb or has_img:
                vheaders = {"Content-Disposition": "inline"}
                status = "dynamic"
                transcoded_size = None
                out_mime = None
                if has_glb:
                    try:
                        _f = lambda x: float(x) if x not in (None, "") else None
                        _i = lambda x: int(x) if x not in (None, "") else None
                        resolved = storage_routes._resolve_glb_params(
                            _f(qp.get("decimate")), qp.get("texture_format"),
                            _i(qp.get("texture_quality")), _i(qp.get("texture_max_size")),
                            qp.get("mesh_compression"), qp.get("output"), qp.get("preset"),
                        )
                        pv = storage_routes._get_threed_pipeline_version(
                            os.getenv("THREED_API_URL", "http://127.0.0.1:8065")
                        )
                        cache_path = storage_routes._glb_cache_path_for(obj, resolved, pv)
                        out_mime = "application/zip" if resolved["output"] == "zip" else "model/gltf-binary"
                        if cache_path.exists():
                            transcoded_size = cache_path.stat().st_size
                            status = "hit"
                        else:
                            status = "miss"
                    except Exception:
                        status = "dynamic"
                vheaders["X-Transcoding-Status"] = status
                if out_mime:
                    vheaders["X-Mime-Type"] = out_mime
                if transcoded_size is not None:
                    vheaders["X-Transcoded-Size"] = str(transcoded_size)
                    vheaders["Content-Length"] = str(transcoded_size)
                    vheaders["Accept-Ranges"] = "bytes"
                _apply_cors_for_head(vheaders, request)
                return Response(
                    status_code=200,
                    media_type=(out_mime or obj.mime_type or "application/octet-stream"),
                    headers=vheaders,
                )

            headers = {"Content-Disposition": "inline", "Accept-Ranges": "bytes"}
            if obj.mime_type:
                headers["X-Mime-Type"] = obj.mime_type
            if obj.transcoding_status:
                headers["X-Transcoding-Status"] = obj.transcoding_status
            if obj.hls_url and obj.transcoding_status == "completed":
                headers["X-HLS-URL"] = obj.hls_url
            if obj.file_size_bytes:
                headers["Content-Length"] = str(obj.file_size_bytes)
            _apply_cors_for_head(headers, request)
            return Response(
                status_code=200,
                media_type=(obj.mime_type or "application/octet-stream"),
                headers=headers,
            )
    return await call_next(request)


# Include routers
app.include_router(storage_routes.router, prefix="/storage", tags=["Storage"])
# Include tenant routes (router already has '/tenants' prefix)
app.include_router(tenant_routes.router, tags=["Tenancy"])
app.include_router(admin_routes.router, prefix="/admin", tags=["Admin"])

@app.get("/")
def root():
    return {
        "service": "storage-api",
        "version": "1.0.0",
        "description": "Media storage and AI analysis service"
    }

@app.get("/health")
def health():
    return {"status": "healthy", "service": "storage-api"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002)
