"""
Async Pipeline Manager for Arkturian Knowledge Graph System

This module provides async task management for AI analysis and knowledge graph building.
It supports two modes:
- fast: Single flash model for everything (quick results)
- quality: Flash for safety check, Pro with thinking for embeddings (best results)
"""

import asyncio
import uuid
import json
import traceback
from datetime import datetime
from typing import Optional, Dict, Any, Tuple
from enum import Enum
from dataclasses import dataclass, asdict
from io import BytesIO

import httpx
from PIL import Image
from storage.pillow_plugins import ensure_heif_support

ensure_heif_support()
from sqlalchemy.orm import Session

from config import settings
from models import StorageObject, AsyncTask


class TaskStatus(str, Enum):
    """Task status enum"""
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class AnalysisMode(str, Enum):
    """Analysis mode enum"""
    FAST = "fast"  # Single flash model for everything
    QUALITY = "quality"  # Flash safety + Pro embeddings with thinking


class ProcessingPhase(str, Enum):
    """Processing phase enum"""
    SAFETY_CHECK = "safety_check"
    AI_ANALYSIS = "ai_analysis"
    BUILDING_KNOWLEDGE_GRAPH = "building_knowledge_graph"
    PROCESSING_URIS = "processing_uris"
    COMPLETE = "complete"


@dataclass
class TaskInfo:
    """Task information"""
    task_id: str
    object_id: int
    status: TaskStatus
    mode: AnalysisMode
    current_phase: Optional[ProcessingPhase] = None
    progress: int = 0
    created_at: str = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    error: Optional[str] = None
    result: Optional[Dict[str, Any]] = None

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.utcnow().isoformat()

    def to_dict(self):
        """Convert to dict for API responses"""
        return {k: v.value if isinstance(v, Enum) else v for k, v in asdict(self).items()}


class AsyncPipelineManager:
    """
    Manages async AI analysis and knowledge graph building.

    This is a singleton that maintains a task queue and executes
    AI analysis and KG building in the background.
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        # Only track running tasks in memory (not persistent)
        self.running_tasks: Dict[str, asyncio.Task] = {}
        self._initialized = True

        # Log file for debugging
        self.log_file = "/tmp/async_pipeline.log"
        self._log("AsyncPipelineManager initialized (database-backed)")

    @staticmethod
    def _summarize_kg_result(kg_result: Optional[Dict[str, Any] | Any]) -> Optional[Dict[str, Any]]:
        """Convert KG pipeline result to a JSON-safe summary."""
        if not kg_result:
            return None

        summary: Dict[str, Any] = {}

        try:
            summary["storage_object_id"] = getattr(kg_result, "storage_object_id", None)
        except Exception:
            summary["storage_object_id"] = None

        try:
            if hasattr(kg_result, "to_dict"):
                dict_payload = kg_result.to_dict()
                summary.update({k: v for k, v in dict_payload.items() if k not in summary})
            else:
                embedding = getattr(kg_result, "embedding", None)
                if embedding is not None:
                    summary["embedding_text_len"] = len(getattr(embedding, "embedding_text", "") or "")
                    summary["metadata_keys"] = list((getattr(embedding, "metadata", {}) or {}).keys())
        except Exception:
            pass

        # Fallback: if kg_result is already dict-like, merge shallow copy
        if isinstance(kg_result, dict):
            summary.update(kg_result)

        # Remove None values so JSON stays clean
        return {k: v for k, v in summary.items() if v not in (None, "")}

    @staticmethod
    def _json_default(obj: Any) -> Any:
        """Fallback serializer for task result payloads."""
        try:
            if hasattr(obj, "to_dict"):
                return obj.to_dict()
            if hasattr(obj, "__dict__"):
                return {
                    k: v for k, v in obj.__dict__.items()
                    if not callable(v) and not k.startswith("__")
                }
        except Exception:
            pass
        return str(obj)

    def _log(self, message: str):
        """Write log message to file"""
        try:
            with open(self.log_file, "a") as f:
                timestamp = datetime.utcnow().isoformat()
                f.write(f"[{timestamp}] {message}\n")
                f.flush()
        except Exception as e:
            print(f"Failed to write log: {e}")

    def _save_task(self, task_info: TaskInfo, db: Session):
        """Save or update task in database"""
        try:
            # Normalize timestamp fields for SQLAlchemy (expect datetime objects)
            def _to_dt(val):
                if val is None:
                    return None
                if isinstance(val, datetime):
                    return val
                try:
                    return datetime.fromisoformat(val)
                except Exception:
                    return None

            created_dt = _to_dt(task_info.created_at) or datetime.utcnow()
            started_dt = _to_dt(task_info.started_at)
            completed_dt = _to_dt(task_info.completed_at)

            # Check if task already exists
            existing = db.query(AsyncTask).filter(AsyncTask.task_id == task_info.task_id).first()

            if existing:
                # Update existing task
                existing.status = task_info.status.value if isinstance(task_info.status, Enum) else task_info.status
                existing.mode = task_info.mode.value if isinstance(task_info.mode, Enum) else task_info.mode
                existing.current_phase = task_info.current_phase.value if isinstance(task_info.current_phase, Enum) else task_info.current_phase
                existing.progress = task_info.progress
                existing.started_at = started_dt
                existing.completed_at = completed_dt
                existing.error = task_info.error
                existing.result = json.dumps(task_info.result, default=self._json_default) if task_info.result else None
            else:
                # Create new task
                new_task = AsyncTask(
                    task_id=task_info.task_id,
                    object_id=task_info.object_id,
                    status=task_info.status.value if isinstance(task_info.status, Enum) else task_info.status,
                    mode=task_info.mode.value if isinstance(task_info.mode, Enum) else task_info.mode,
                    current_phase=task_info.current_phase.value if isinstance(task_info.current_phase, Enum) and task_info.current_phase else None,
                    progress=task_info.progress,
                    created_at=created_dt,
                    started_at=started_dt,
                    completed_at=completed_dt,
                    error=task_info.error,
                    result=json.dumps(task_info.result, default=self._json_default) if task_info.result else None
                )
                db.add(new_task)

            db.commit()
        except Exception as e:
            self._log(f"Error saving task {task_info.task_id}: {e}")
            db.rollback()
            raise

    def _load_task(self, task_id: str, db: Session) -> Optional[TaskInfo]:
        """Load task from database"""
        try:
            db_task = db.query(AsyncTask).filter(AsyncTask.task_id == task_id).first()
            if not db_task:
                return None

            return TaskInfo(
                task_id=db_task.task_id,
                object_id=db_task.object_id,
                status=TaskStatus(db_task.status),
                mode=AnalysisMode(db_task.mode),
                current_phase=ProcessingPhase(db_task.current_phase) if db_task.current_phase else None,
                progress=db_task.progress,
                created_at=db_task.created_at,
                started_at=db_task.started_at,
                completed_at=db_task.completed_at,
                error=db_task.error,
                result=json.loads(db_task.result) if db_task.result else None
            )
        except Exception as e:
            self._log(f"Error loading task {task_id}: {e}")
            return None

    async def start_task(
        self,
        object_id: int,
        mode: str = "quality",
        db: Session = None,
        ai_tasks_str: Optional[str] = None,
        vision_mode: Optional[str] = None,
        context_role: Optional[str] = None,
        context_overrides: Optional[Dict[str, Any]] = None,
        trim_before_analysis: bool = False,
        trim_delivery_default: bool = False,
    ) -> str:
        """
        Start a new async processing task.

        Args:
            object_id: Storage object ID to process
            mode: "fast" or "quality"
            db: Database session

        Returns:
            Task ID for status tracking
        """
        task_id = str(uuid.uuid4())

        # Validate mode
        try:
            analysis_mode = AnalysisMode(mode)
        except ValueError:
            analysis_mode = AnalysisMode.QUALITY

        context_payload: Dict[str, Any] = {}
        if db is not None:
            try:
                storage_obj = db.query(StorageObject).filter(StorageObject.id == object_id).first()
            except Exception:
                storage_obj = None
            if storage_obj is not None:
                tenant = getattr(storage_obj, "tenant_id", None)
                if not tenant:
                    meta_json = getattr(storage_obj, "metadata_json", None) or {}
                    tenant = meta_json.get("tenant_id", "arkturian")
                context_payload = await self._build_analysis_context(storage_obj, tenant or "arkturian")

        if context_overrides:
            if not context_payload:
                context_payload = {}

            override_metadata = context_overrides.get("metadata")
            if isinstance(override_metadata, dict):
                existing_meta = context_payload.get("metadata")
                if isinstance(existing_meta, dict):
                    existing_meta.update(override_metadata)
                else:
                    context_payload["metadata"] = override_metadata

            for key, value in context_overrides.items():
                if key == "metadata":
                    continue
                context_payload[key] = value

        config_dict = {
            "ai_tasks": ai_tasks_str,
            "vision_mode": vision_mode or "auto",
            "context_role": context_role,
            "trim": {
                "enabled": bool(trim_before_analysis),
                "delivery_default": bool(trim_delivery_default),
            },
        }
        if context_payload:
            config_dict["context"] = context_payload

        # Create task info
        task_info = TaskInfo(
            task_id=task_id,
            object_id=object_id,
            status=TaskStatus.QUEUED,
            mode=analysis_mode,
            result={
                "config": config_dict
            }
        )

        # Save to database
        self._save_task(task_info, db)
        self._log(f"Created task {task_id} for object {object_id} in {mode} mode")

        # Start background processing
        task = asyncio.create_task(self._process_task(task_id, db))
        self.running_tasks[task_id] = task

        # Clean up when done
        task.add_done_callback(lambda t: self._cleanup_task(task_id))

        return task_id

    def _cleanup_task(self, task_id: str):
        """Remove task from running tasks when complete"""
        if task_id in self.running_tasks:
            del self.running_tasks[task_id]
            self._log(f"Cleaned up task {task_id}")

    async def _run_image_analysis_with_retry(
        self,
        storage_obj: StorageObject,
        tenant_id: str,
        analyze_content,
        cfg: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Attempt vision analysis with progressively smaller/simpler variants."""
        from storage.service import load_image_bytes_for_analysis

        attempts = [
            {"max_edge": 1300, "target_format": "jpeg", "quality": 85},
            {"max_edge": 1024, "target_format": "jpeg", "quality": 75},
            {"max_edge": 768, "target_format": "png", "quality": 90},
        ]

        attempt_log = []
        last_result: Optional[Dict[str, Any]] = None

        trim_meta: Dict[str, Any] = {}

        for attempt in attempts:
            try:
                data, data_mime, prep_meta = await load_image_bytes_for_analysis(
                    storage_obj,
                    tenant_id,
                    max_edge=attempt["max_edge"],
                    target_format=attempt["target_format"],
                    quality=attempt["quality"],
                    trim_config=cfg.get("trim"),
                )
                if prep_meta:
                    trim_meta = prep_meta
            except Exception as exc:
                attempt_log.append({**attempt, "status": "load_failed", "error": str(exc)})
                last_result = {
                    "category": "error",
                    "mode": "image_load_error",
                    "embedding_info": {"metadata": {"error": str(exc)}}
                }
                continue

            result = await analyze_content(
                data,
                data_mime,
                object_id=storage_obj.id,
                context=cfg.get("context"),
                vision_mode=cfg.get("vision_mode", "auto"),
                ai_tasks_str=cfg.get("ai_tasks"),
                context_role=cfg.get("context_role")
            )

            attempt_entry = {
                **attempt,
                "status": "ok",
                "mime_type": data_mime,
                "result_mode": result.get("mode"),
                "result_category": result.get("category"),
                "error": (result.get("embedding_info", {}) or {}).get("metadata", {}).get("error"),
                "trim_applied": trim_meta.get("applied") if trim_meta else False,
            }
            attempt_log.append(attempt_entry)

            last_result = result

            if result.get("mode") not in {"error", "vision_comprehensive_error", "image_load_error"}:
                break

        if last_result is None:
            last_result = {
                "category": "error",
                "mode": "analysis_unknown_failure",
                "embedding_info": {"metadata": {"error": "Analysis produced no result"}}
            }

        embedding_info = last_result.setdefault("embedding_info", {}) or {}
        metadata = embedding_info.setdefault("metadata", {}) or {}
        metadata["analysisAttempts"] = attempt_log

        if trim_meta:
            last_result.setdefault("trim_metadata", trim_meta)

        return last_result

    async def _build_analysis_context(self, storage_obj: StorageObject, tenant_id: str) -> Dict[str, Any]:
        """Gather contextual metadata and image information for prompting."""
        context: Dict[str, Any] = {}

        try:
            meta_source = storage_obj.ai_context_metadata or {}

            metadata_block: Dict[str, Any] = {}
            base_metadata = meta_source.get("metadata")
            if isinstance(base_metadata, dict):
                metadata_block.update(base_metadata)

            # Optional additional structured data (technical tables, features)
            technical_data = meta_source.get("technical_data")
            if technical_data:
                metadata_block["technical_data"] = technical_data

            features = meta_source.get("features")
            if features:
                metadata_block["features"] = features

            description = meta_source.get("description")
            if description:
                metadata_block["description"] = description

            if metadata_block:
                context["metadata"] = metadata_block

            context_text = meta_source.get("context_text")
            if context_text:
                context["context_text"] = context_text

            width = getattr(storage_obj, "width", None)
            height = getattr(storage_obj, "height", None)
            if not width or not height:
                width, height = await self._get_image_dimensions(storage_obj, tenant_id)

            if width and height:
                context.setdefault("image", {})
                context["image"]["width_px"] = int(width)
                context["image"]["height_px"] = int(height)

        except Exception as exc:
            self._log(f"Context build failed for object {getattr(storage_obj, 'id', 'unknown')}: {exc}")

        return context

    async def _get_image_dimensions(self, storage_obj: StorageObject, tenant_id: str) -> tuple[Optional[int], Optional[int]]:
        """Derive natural image dimensions from local storage or remote URI."""
        from storage.service import generic_storage

        # Try local file first
        try:
            if getattr(storage_obj, "object_key", None):
                path = generic_storage.absolute_path_for_key(storage_obj.object_key, tenant_id or "arkturian")
                if path.exists():
                    with Image.open(path) as img:
                        return img.width, img.height
        except Exception as exc:
            self._log(f"Local dimension lookup failed: {exc}")

        # External URI fallback
        external_uri = getattr(storage_obj, "external_uri", None)
        if external_uri:
            try:
                async with httpx.AsyncClient(timeout=20.0) as client:
                    resp = await client.get(external_uri)
                    resp.raise_for_status()
                    with Image.open(BytesIO(resp.content)) as img:
                        return img.width, img.height
            except Exception as exc:
                self._log(f"External dimension lookup failed: {exc}")

        # Storage media derivative fallback
        base_url = settings.BASE_URL.rstrip("/")
        media_url = f"{base_url}/storage/media/{getattr(storage_obj, 'id', '')}"
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(media_url)
                resp.raise_for_status()
                with Image.open(BytesIO(resp.content)) as img:
                    return img.width, img.height
        except Exception as exc:
            self._log(f"Storage media dimension lookup failed: {exc}")

        return None, None

    async def _process_task(self, task_id: str, db: Session):
        """
        Process a task through the complete pipeline.

        This is the main async processing function that orchestrates
        the entire AI analysis and KG building process.
        """
        # Load task from database
        task_info = self._load_task(task_id, db)
        if not task_info:
            self._log(f"Task {task_id}: Not found in database")
            return

        try:
            # Mark as processing
            task_info.status = TaskStatus.PROCESSING
            task_info.started_at = datetime.utcnow().isoformat()
            task_info.progress = 5
            self._save_task(task_info, db)

            self._log(f"Task {task_id}: Starting processing for object {task_info.object_id}")

            # Get storage object from database
            storage_obj = db.query(StorageObject).filter(
                StorageObject.id == task_info.object_id
            ).first()

            if not storage_obj:
                raise ValueError(f"Storage object {task_info.object_id} not found")

            # Process based on mode
            if task_info.mode == AnalysisMode.FAST:
                result = await self._process_fast_mode(task_info, storage_obj, db)
            else:
                result = await self._process_quality_mode(task_info, storage_obj, db)

            # Mark as completed
            task_info.status = TaskStatus.COMPLETED
            task_info.completed_at = datetime.utcnow().isoformat()
            task_info.current_phase = ProcessingPhase.COMPLETE
            task_info.progress = 100
            task_info.result = result
            self._save_task(task_info, db)

            self._log(f"Task {task_id}: Completed successfully")

        except Exception as e:
            # Mark as failed
            task_info.status = TaskStatus.FAILED
            task_info.completed_at = datetime.utcnow().isoformat()
            task_info.error = str(e)
            task_info.progress = 0
            task_info.result = {"error": str(e)}
            self._save_task(task_info, db)

            error_trace = traceback.format_exc()
            self._log(f"Task {task_id}: Failed with error: {e}\n{error_trace}")

    async def _process_fast_mode(
        self,
        task_info: TaskInfo,
        storage_obj: StorageObject,
        db: Session
    ) -> Dict[str, Any]:
        """
        Fast mode: Process with AI analysis first, then knowledge graph pipeline.

        Steps:
        1. Run AI analysis (analyze_content)
        2. Save results to database
        3. Build knowledge graph from AI results
        """
        self._log(f"Task {task_info.task_id}: Fast mode processing")

        # STEP 1: AI ANALYSIS
        task_info.current_phase = ProcessingPhase.AI_ANALYSIS
        task_info.progress = 10
        self._save_task(task_info, db)

        self._log(f"Task {task_info.task_id}: Running AI analysis")

        # Load file from storage and run AI analysis with retries for images
        from storage.service import generic_storage
        tenant_id = getattr(storage_obj, 'tenant_id', None)
        if not tenant_id:
            meta = getattr(storage_obj, 'metadata_json', None) or {}
            tenant_id = meta.get('tenant_id')
        tenant_id = tenant_id or 'arkturian'
        mime_type = (storage_obj.mime_type or "")

        from ai_analysis.service import analyze_content
        cfg = (task_info.result or {}).get("config", {}) if task_info.result else {}
        cfg_trim = (cfg.get("trim") or {}) if cfg else {}

        if mime_type.lower().startswith("image/"):
            analysis_result = await self._run_image_analysis_with_retry(
                storage_obj,
                tenant_id,
                analyze_content,
                cfg
            )
        else:
            object_key = getattr(storage_obj, 'object_key', None)
            if not object_key:
                raise FileNotFoundError(
                    f"No local object key for storage object {storage_obj.id}"
                )
            file_path = generic_storage.absolute_path_for_key(object_key, tenant_id)
            if not file_path.exists():
                raise FileNotFoundError(f"File missing from storage: {file_path}")

            with open(file_path, "rb") as f:
                data = f.read()

            analysis_result = await analyze_content(
                data,
                mime_type,
                object_id=storage_obj.id,
                context=cfg.get("context"),
                vision_mode=cfg.get("vision_mode", "auto"),
                ai_tasks_str=cfg.get("ai_tasks"),
                context_role=cfg.get("context_role")
            )

        # Ensure object is attached to this session
        storage_obj = db.merge(storage_obj)

        # Save AI analysis results to database (like sync route does)
        storage_obj.ai_category = analysis_result.get("category")
        storage_obj.ai_danger_potential = analysis_result.get("danger_potential")

        if "safety_info" in analysis_result:
            storage_obj.safety_info = analysis_result.get("safety_info")
            safety_info = analysis_result.get("safety_info", {})
            storage_obj.ai_safety_rating = "safe" if safety_info.get("isSafe", True) else "unsafe"

        storage_obj.ai_title = analysis_result.get("ai_title")
        storage_obj.ai_subtitle = analysis_result.get("ai_subtitle")
        storage_obj.ai_tags = analysis_result.get("ai_tags", [])
        storage_obj.ai_collections = analysis_result.get("ai_collections", [])

        # Store extracted tags and embedding info for Knowledge Graph
        if "extracted_tags" in analysis_result or "embedding_info" in analysis_result or analysis_result.get("trim_metadata"):
            context_meta = storage_obj.ai_context_metadata.copy() if storage_obj.ai_context_metadata else {}
            if "extracted_tags" in analysis_result or "embedding_info" in analysis_result:
                context_meta["extracted_tags"] = analysis_result.get("extracted_tags", {})
                context_meta["embedding_info"] = analysis_result.get("embedding_info", {})
                context_meta["mode"] = analysis_result.get("mode", "")
                context_meta["prompt"] = analysis_result.get("prompt", "")
                context_meta["response"] = analysis_result.get("ai_response", "")

            trim_meta = analysis_result.get("trim_metadata")
            if trim_meta:
                context_meta["trim_bounds"] = trim_meta
            if cfg_trim.get("delivery_default"):
                context_meta["trim_delivery_default"] = True
            else:
                context_meta.pop("trim_delivery_default", None)

            storage_obj.ai_context_metadata = context_meta

        db.commit()
        db.refresh(storage_obj)

        self._log(f"Task {task_info.task_id}: AI analysis complete, saved to database")

        # STEP 2: KNOWLEDGE GRAPH
        task_info.current_phase = ProcessingPhase.BUILDING_KNOWLEDGE_GRAPH
        task_info.progress = 50
        self._save_task(task_info, db)

        from knowledge_graph.pipeline import kg_pipeline

        self._log(f"Task {task_info.task_id}: Building knowledge graph")
        kg_result = await kg_pipeline.process_storage_object(storage_obj, db)

        task_info.progress = 90
        self._save_task(task_info, db)

        # Get stats from database
        external_objects = db.query(StorageObject).filter(
            StorageObject.link_id == str(storage_obj.id)
        ).count() if kg_result else 0

        kg_summary = self._summarize_kg_result(kg_result)
        embeddings_created = 0
        if isinstance(kg_result, dict):
            embeddings_created = kg_result.get("embeddings_created", 0)
        elif kg_result is not None:
            embeddings_created = 1

        return {
            "mode": "fast",
            "kg_result": kg_summary,
            "embeddings_created": embeddings_created,
            "external_objects_created": external_objects
        }

    async def _process_quality_mode(
        self,
        task_info: TaskInfo,
        storage_obj: StorageObject,
        db: Session
    ) -> Dict[str, Any]:
        """
        Quality mode: Process with AI analysis first, then knowledge graph pipeline.

        Steps:
        1. Run AI analysis (analyze_content)
        2. Save results to database
        3. Build knowledge graph from AI results

        In the future, this could use Pro model with thinking
        by passing parameters to analyze_content.
        """
        self._log(f"Task {task_info.task_id}: Quality mode processing")

        # STEP 1: AI ANALYSIS
        task_info.current_phase = ProcessingPhase.AI_ANALYSIS
        task_info.progress = 10
        self._save_task(task_info, db)

        self._log(f"Task {task_info.task_id}: Running AI analysis")

        # Load file from storage and run AI analysis with retries for images
        from storage.service import generic_storage
        tenant_id = getattr(storage_obj, 'tenant_id', None)
        if not tenant_id:
            meta = getattr(storage_obj, 'metadata_json', None) or {}
            tenant_id = meta.get('tenant_id')
        tenant_id = tenant_id or 'arkturian'
        mime_type = (storage_obj.mime_type or "")

        from ai_analysis.service import analyze_content
        cfg = (task_info.result or {}).get("config", {}) if task_info.result else {}
        cfg_trim = (cfg.get("trim") or {}) if cfg else {}

        if mime_type.lower().startswith("image/"):
            analysis_result = await self._run_image_analysis_with_retry(
                storage_obj,
                tenant_id,
                analyze_content,
                cfg
            )
        else:
            object_key = getattr(storage_obj, 'object_key', None)
            if not object_key:
                raise FileNotFoundError(
                    f"No local object key for storage object {storage_obj.id}"
                )
            file_path = generic_storage.absolute_path_for_key(object_key, tenant_id)
            if not file_path.exists():
                raise FileNotFoundError(f"File missing from storage: {file_path}")

            with open(file_path, "rb") as f:
                data = f.read()

            analysis_result = await analyze_content(
                data,
                mime_type,
                object_id=storage_obj.id,
                context=None,
                vision_mode=cfg.get("vision_mode", "auto"),
                ai_tasks_str=cfg.get("ai_tasks"),
                context_role=cfg.get("context_role")
            )

        # Ensure object is attached to this session
        storage_obj = db.merge(storage_obj)

        # Save AI analysis results to database (like sync route does)
        storage_obj.ai_category = analysis_result.get("category")
        storage_obj.ai_danger_potential = analysis_result.get("danger_potential")

        if "safety_info" in analysis_result:
            storage_obj.safety_info = analysis_result.get("safety_info")
            safety_info = analysis_result.get("safety_info", {})
            storage_obj.ai_safety_rating = "safe" if safety_info.get("isSafe", True) else "unsafe"

        storage_obj.ai_title = analysis_result.get("ai_title")
        storage_obj.ai_subtitle = analysis_result.get("ai_subtitle")
        storage_obj.ai_tags = analysis_result.get("ai_tags", [])
        storage_obj.ai_collections = analysis_result.get("ai_collections", [])

        # Store extracted tags and embedding info for Knowledge Graph
        if "extracted_tags" in analysis_result or "embedding_info" in analysis_result or analysis_result.get("trim_metadata"):
            context_meta = storage_obj.ai_context_metadata.copy() if storage_obj.ai_context_metadata else {}
            if "extracted_tags" in analysis_result or "embedding_info" in analysis_result:
                context_meta["extracted_tags"] = analysis_result.get("extracted_tags", {})
                context_meta["embedding_info"] = analysis_result.get("embedding_info", {})
                context_meta["mode"] = analysis_result.get("mode", "")
                context_meta["prompt"] = analysis_result.get("prompt", "")
                context_meta["response"] = analysis_result.get("ai_response", "")

            trim_meta = analysis_result.get("trim_metadata")
            if trim_meta:
                context_meta["trim_bounds"] = trim_meta
            if cfg_trim.get("delivery_default"):
                context_meta["trim_delivery_default"] = True
            else:
                context_meta.pop("trim_delivery_default", None)

            storage_obj.ai_context_metadata = context_meta

        db.commit()
        db.refresh(storage_obj)

        self._log(f"Task {task_info.task_id}: AI analysis complete, saved to database")

        task_info.progress = 40
        self._save_task(task_info, db)

        # STEP 2: KNOWLEDGE GRAPH
        task_info.current_phase = ProcessingPhase.BUILDING_KNOWLEDGE_GRAPH
        task_info.progress = 50
        self._save_task(task_info, db)

        from knowledge_graph.pipeline import kg_pipeline

        self._log(f"Task {task_info.task_id}: Building knowledge graph")
        kg_result = await kg_pipeline.process_storage_object(storage_obj, db)

        task_info.progress = 90
        self._save_task(task_info, db)

        # Get stats from database
        external_objects = db.query(StorageObject).filter(
            StorageObject.link_id == str(storage_obj.id)
        ).count() if kg_result else 0

        kg_summary = self._summarize_kg_result(kg_result)
        embeddings_created = 0
        if isinstance(kg_result, dict):
            embeddings_created = kg_result.get("embeddings_created", 0)
        elif kg_result is not None:
            embeddings_created = 1

        return {
            "mode": "quality",
            "kg_result": kg_summary,
            "embeddings_created": embeddings_created,
            "external_objects_created": external_objects
        }

    def get_task_status(self, task_id: str, db: Session) -> Optional[Dict[str, Any]]:
        """
        Get status of a task.

        Args:
            task_id: Task ID to check
            db: Database session

        Returns:
            Task info dict or None if not found
        """
        task_info = self._load_task(task_id, db)
        if not task_info:
            return None

        return task_info.to_dict()

    def get_all_tasks(self, db: Session, limit: int = 100, object_id: int = None) -> list:
        """Get all tasks (most recent first), optionally filtered by object_id"""
        try:
            query = db.query(AsyncTask)

            # Filter by object_id if provided
            if object_id is not None:
                query = query.filter(AsyncTask.object_id == object_id)

            db_tasks = query.order_by(
                AsyncTask.created_at.desc()
            ).limit(limit).all()

            return [
                TaskInfo(
                    task_id=t.task_id,
                    object_id=t.object_id,
                    status=TaskStatus(t.status),
                    mode=AnalysisMode(t.mode),
                    current_phase=ProcessingPhase(t.current_phase) if t.current_phase else None,
                    progress=t.progress,
                    created_at=t.created_at,
                    started_at=t.started_at,
                    completed_at=t.completed_at,
                    error=t.error,
                    result=json.loads(t.result) if t.result else None
                ).to_dict()
                for t in db_tasks
            ]
        except Exception as e:
            self._log(f"Error loading tasks: {e}")
            return []


# Global singleton instance
pipeline_manager = AsyncPipelineManager()
