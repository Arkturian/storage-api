"""
Local Transcoding API Server

FastAPI server that wraps the transcoding package for local FFmpeg transcoding.
Provides the same API interface as the Mac Transcoding API.

Run with: uvicorn transcoding_api_server:app --host 127.0.0.1 --port 8087

Security (#2909): every route requires an internal caller (loopback by
default, or X-Transcoding-Token when TRANSCODING_API_TOKEN is set), and
uploaded file names are reduced to a basename that provably stays in the
job directory. Never bind this service to 0.0.0.0 or run it as root.
"""

import asyncio
import hmac
import ipaddress
import logging
import os
import uuid
import zipfile
from pathlib import Path
from typing import Dict, Optional
from datetime import datetime

from fastapi import Depends, FastAPI, HTTPException, File, UploadFile, Form, Request
from fastapi.responses import FileResponse

# Configure logging at module level
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
from pydantic import BaseModel
import httpx

# Try to import transcoding package
try:
    from transcoding import TranscoderFactory, TranscodingConfig, TranscodingMode
    TRANSCODING_AVAILABLE = True
except ImportError:
    TRANSCODING_AVAILABLE = False
    logging.warning("Transcoding package not available - install with: pip install -e /path/to/mac_transcoding_api")

def _safe_basename(name: Optional[str], default: str = "upload.bin") -> str:
    """Client file name -> one harmless path component (#2909)."""
    if not name:
        return default
    candidate = str(name).replace("\\", "/").split("/")[-1]
    candidate = "".join(ch for ch in candidate if ch >= " " and ch != "\x7f").strip().lstrip(".")
    if not candidate or candidate in (".", ".."):
        return default
    return candidate[:200]


def _safe_child(base: Path, name: Optional[str]) -> Path:
    base_real = base.resolve(strict=True)
    target = base_real / _safe_basename(name)
    if target.is_symlink() or not target.resolve().is_relative_to(base_real):
        raise HTTPException(status_code=400, detail="invalid_filename")
    return target


def _require_internal_caller(request: Request) -> None:
    """Loopback only, or X-Transcoding-Token when TRANSCODING_API_TOKEN is set."""
    token = os.getenv("TRANSCODING_API_TOKEN", "")
    if token:
        sent = request.headers.get("x-transcoding-token", "")
        if sent and hmac.compare_digest(sent.encode(), token.encode()):
            return
        raise HTTPException(status_code=401, detail="transcoding_token_required")
    try:
        addr = ipaddress.ip_address(request.client.host if request.client else "")
    except ValueError:
        raise HTTPException(status_code=403, detail="caller_not_allowed")
    if addr.is_loopback:
        return
    raise HTTPException(status_code=403, detail="caller_not_allowed")


app = FastAPI(
    title="Local Transcoding API",
    version="1.0.0",
    dependencies=[Depends(_require_internal_caller)],
)

# Job storage (in-memory, simple implementation)
jobs: Dict[str, dict] = {}
current_job_id: Optional[str] = None


class TranscodeRequest(BaseModel):
    """Transcoding job request"""
    job_id: str
    source_url: str
    callback_url: Optional[str] = None
    file_size_bytes: int
    original_filename: str
    storage_object_id: str
    download_headers: Optional[Dict[str, str]] = None


class JobStatus(BaseModel):
    """Job status response"""
    job_id: str
    status: str  # queued, downloading, transcoding, uploading, completed, failed
    progress: int  # 0-100
    message: Optional[str] = None
    error: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None


@app.get("/")
async def root():
    """Health check and status"""
    return {
        "service": "Local Transcoding API",
        "status": "online",
        "active_jobs": 1 if current_job_id else 0,
        "queue_size": len([j for j in jobs.values() if j["status"] == "queued"]),
        "current_job": current_job_id,
        "mode": "local_ffmpeg"
    }


@app.get("/health")
async def health():
    """Health check endpoint"""
    if not TRANSCODING_AVAILABLE:
        raise HTTPException(status_code=503, detail="Transcoding package not available")

    # Check if ffmpeg is available
    try:
        config = TranscodingConfig(mode=TranscodingMode.LOCAL)
        transcoder = TranscoderFactory.create(config)
        available = await transcoder.check_availability()

        if not available:
            raise HTTPException(status_code=503, detail="FFmpeg not available")

        return {"status": "healthy", "ffmpeg": "available"}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Health check failed: {str(e)}")


@app.post("/transcode")
async def transcode(
    file: UploadFile = File(...),
    response_mode: str = Form(default="path")
):
    """
    Submit a transcoding job via file upload

    Args:
        file: Video file to transcode
        response_mode: Response format - "path" (return paths) or "zip" (return ZIP file)

    Response modes:
        - path: Returns JSON with output_dir and variant paths (for local usage)
        - zip: Returns ZIP file containing all transcoded files (for remote usage)

    NOTE: Job is processed synchronously (awaited) instead of using BackgroundTasks
    because background tasks get cancelled when HTTP response is sent in uvicorn workers.
    The caller should use httpx with a long timeout.
    """
    if not TRANSCODING_AVAILABLE:
        raise HTTPException(status_code=503, detail="Transcoding package not available")

    # Generate job ID
    job_id = str(uuid.uuid4())

    # Save uploaded file to temp directory
    temp_dir = Path(f"/tmp/transcode_{job_id}")
    temp_dir.mkdir(parents=True, exist_ok=True)

    original_filename = _safe_basename(file.filename)
    source_file = _safe_child(temp_dir, original_filename)

    # Write uploaded file
    with open(source_file, "wb") as f:
        content = await file.read()
        f.write(content)

    logging.info(f"📥 Received file: {file.filename} ({len(content)} bytes)")

    # Create job record
    job = {
        "job_id": job_id,
        "status": "queued",
        "progress": 0,
        "message": "Job queued",
        "source_file": str(source_file),
        "original_filename": original_filename,
        "file_size": len(content),
        "started_at": datetime.utcnow().isoformat(),
        "completed_at": None,
        "error": None
    }

    jobs[job_id] = job

    logging.info(f"📤 Starting transcoding job: {job_id} - {file.filename}")

    # Process job synchronously (await completion)
    await process_job_from_file(job_id)

    job_result = jobs[job_id]

    # Check if transcoding failed
    if job_result["status"] == "failed":
        return {
            "job_id": job_id,
            "status": job_result["status"],
            "message": job_result["message"],
            "error": job_result.get("error")
        }

    # Return based on response_mode
    if response_mode == "zip":
        # Create ZIP file with all transcoded files
        output_dir = Path(job_result["output_dir"])
        zip_path = temp_dir / f"{job_id}_transcoded.zip"

        logging.info(f"📦 Creating ZIP archive: {zip_path}")

        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            # Add all files from output directory
            for file_path in output_dir.rglob('*'):
                if file_path.is_file():
                    arcname = file_path.relative_to(output_dir)
                    zipf.write(file_path, arcname)
                    logging.info(f"   Added to ZIP: {arcname}")

        logging.info(f"✅ ZIP created: {zip_path.stat().st_size} bytes")

        # Return ZIP file
        return FileResponse(
            path=str(zip_path),
            media_type="application/zip",
            filename=f"{Path(original_filename).stem}_transcoded.zip",
            headers={
                "X-Job-ID": job_id,
                "X-Transcoding-Status": job_result["status"],
                "X-Variant-Count": str(len(job_result.get("variants", [])))
            }
        )
    else:
        # Return JSON with paths (default mode)
        return {
            "job_id": job_id,
            "status": job_result["status"],
            "message": job_result["message"],
            "error": job_result.get("error"),
            "output_dir": job_result.get("output_dir"),
            "variants": job_result.get("variants", [])
        }


@app.get("/status/{job_id}")
async def get_status(job_id: str):
    """Get job status"""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    job = jobs[job_id]

    return JobStatus(
        job_id=job["job_id"],
        status=job["status"],
        progress=job["progress"],
        message=job.get("message"),
        error=job.get("error"),
        started_at=job.get("started_at"),
        completed_at=job.get("completed_at")
    )


@app.post("/cancel/{job_id}")
async def cancel_job(job_id: str):
    """Cancel a job"""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    job = jobs[job_id]

    if job["status"] in ["completed", "failed"]:
        return {"message": "Job already finished", "status": job["status"]}

    job["status"] = "cancelled"
    job["message"] = "Job cancelled by user"
    job["completed_at"] = datetime.utcnow().isoformat()

    logging.info(f"🚫 Job cancelled: {job_id}")

    return {"message": "Job cancelled", "job_id": job_id}


async def process_job_from_file(job_id: str):
    """
    Process a transcoding job from uploaded file

    Steps:
    1. File already uploaded
    2. Transcode using local FFmpeg
    3. Upload results back (TODO: implement upload to storage)
    4. Call callback URL (if provided)
    """
    global current_job_id

    job = jobs[job_id]
    current_job_id = job_id

    try:
        source_file = Path(job["source_file"])

        logging.info(f"📁 Processing file: {source_file} ({job['file_size']} bytes)")

        # Step 1: Transcode
        job["status"] = "transcoding"
        job["progress"] = 30
        job["message"] = "Transcoding video"
        logging.info(f"🎬 Transcoding: {source_file}")

        # Create output directory
        output_dir = source_file.parent / "output"
        output_dir.mkdir(exist_ok=True)

        # Create transcoder
        config = TranscodingConfig(mode=TranscodingMode.LOCAL)
        transcoder = TranscoderFactory.create(config)

        # Transcode
        result = await transcoder.transcode(source_file, output_dir)

        if not result.success:
            raise Exception(f"Transcoding failed: {result.error}")

        logging.info(f"✅ Transcoded {len(result.variants)} variants")
        for variant in result.variants:
            logging.info(f"   - {variant.name}: {variant.resolution} @ {variant.bitrate_mbps:.1f} Mbps")

        # Job completed
        job["status"] = "completed"
        job["progress"] = 100
        job["message"] = f"Transcoding completed - {len(result.variants)} variants created"
        job["completed_at"] = datetime.utcnow().isoformat()
        job["output_dir"] = str(output_dir)
        job["variants"] = [
            {
                "name": v.name,
                "resolution": v.resolution,
                "bitrate_mbps": v.bitrate_mbps,
                "file_path": str(v.output_path) if v.output_path else None
            }
            for v in result.variants
        ]

        logging.info(f"✅ Job completed: {job_id}")

    except Exception as e:
        logging.error(f"❌ Job failed: {job_id} - {str(e)}")
        import traceback
        traceback.print_exc()

        job["status"] = "failed"
        job["progress"] = 0
        job["error"] = str(e)
        job["message"] = "Transcoding failed"
        job["completed_at"] = datetime.utcnow().isoformat()

    finally:
        current_job_id = None


if __name__ == "__main__":
    import uvicorn

    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )

    # Run server
    uvicorn.run(app, host="127.0.0.1", port=8087)
