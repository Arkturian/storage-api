"""AI-cost-free face-detection focal-point computation.

Self-contained OpenCV-YuNet face detector that computes a normalized focal
point (the center of the *primary* face) for an image. Runs entirely locally on
CPU via a vendored YuNet ONNX model — **zero external AI cost, no network
call**. This is the storage-api side of the focal-point feature requested in
Content-Post #4225 (WebConverter consumer contract).

Design goals
------------
* **Dependency-light + self-contained** — only needs ``opencv-python(-headless)``,
  ``numpy`` and ``Pillow``, all already installed. The module has no storage-api
  imports so it can be copy-pasted 1:1 into ``tools-api`` (or any service) that
  wants an ad-hoc ``POST /face-detect`` endpoint on arbitrary URLs.
* **Opt-in / default-off** — nothing in here runs unless a caller explicitly
  asks for a focal point (lazy ``GET /focal`` or ``X-Compute-Focal`` on upload).

Consumer contract (matches WebConverter, Content-Post #4225)
------------------------------------------------------------
``compute_focal_point(path)`` returns::

    {
      "focal_point": {"x_pct": 50.2, "y_pct": 24.7} | None,
      "faces_detected": 1,
      "faces": [
        {"x_pct": 41.0, "y_pct": 12.0, "w_pct": 18.4, "h_pct": 25.1,
         "confidence": 0.94, "is_primary": true}
      ],
      "image_width": 1200,
      "image_height": 1600,
      "model": "yunet_2023mar",
      "computed_at": "2026-07-11T18:40:00Z"
    }

Semantics
* ``focal_point`` = **center** of the primary face, ``x_pct``/``y_pct`` in
  ``0..100``. ``None`` when 0 faces are found — the consumer then falls back to
  ``object-position: top``.
* ``faces[].x_pct``/``y_pct`` = **top-left** corner of the face box;
  ``w_pct``/``h_pct`` = box size (all ``0..100``). The focal point is derived as
  ``x + w/2`` / ``y + h/2`` of the primary face.
* **Primary face** = largest box; ties broken by proximity to the image center
  (portrait framing convention).
* EXIF orientation is applied **before** detection so coordinates match the
  visually-upright image.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
from PIL import Image, ImageOps

try:  # HEIC/HEIF support if the plugin is present (storage-api ships pillow_heif)
    import pillow_heif  # type: ignore

    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover - optional
    pass

import cv2

MODEL_NAME = "yunet_2023mar"
_MODEL_PATH = Path(__file__).resolve().parent / "face_models" / "face_detection_yunet_2023mar.onnx"

# Detection is run on a downscaled copy so full-resolution portraits (which can
# be 6000px+) neither blow memory nor waste CPU. Normalized (percent) output is
# scale-invariant, so downscaling does not change the result. Only the shortest
# path that keeps faces detectable matters.
_MAX_DETECT_SIDE = 1024
_SCORE_THRESHOLD = 0.6
_NMS_THRESHOLD = 0.3
_TOP_K = 5000

# YuNet's FaceDetectorYN carries mutable input-size state and is not safe for
# concurrent detect() calls; FastAPI runs sync endpoints in a threadpool, so we
# serialize detection behind a lock and reuse one lazily-loaded detector.
_detector: Optional["cv2.FaceDetectorYN"] = None
_detector_lock = threading.Lock()


class FaceDetectionUnavailable(RuntimeError):
    """Raised when the YuNet model file is missing or fails to load."""


def _get_detector() -> "cv2.FaceDetectorYN":
    global _detector
    if _detector is None:
        if not _MODEL_PATH.exists():
            raise FaceDetectionUnavailable(f"YuNet model not found at {_MODEL_PATH}")
        try:
            _detector = cv2.FaceDetectorYN.create(
                str(_MODEL_PATH),
                "",
                (320, 320),
                _SCORE_THRESHOLD,
                _NMS_THRESHOLD,
                _TOP_K,
            )
        except Exception as exc:  # pragma: no cover - defensive
            raise FaceDetectionUnavailable(f"Failed to load YuNet model: {exc}") from exc
    return _detector


def _load_bgr(image_path: Path) -> np.ndarray:
    """Load an image as an EXIF-corrected BGR numpy array (OpenCV order)."""
    with Image.open(image_path) as img:
        img = ImageOps.exif_transpose(img)  # orientation BEFORE detection
        img = img.convert("RGB")
        rgb = np.asarray(img)
    # PIL is RGB, OpenCV expects BGR
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)


def compute_focal_point(image_path: Path | str) -> Dict[str, Any]:
    """Detect faces and compute the normalized focal point for one image.

    Args:
        image_path: Path to a local image file (EXIF orientation is honored).

    Returns:
        The consumer-contract dict described in the module docstring.

    Raises:
        FaceDetectionUnavailable: model file missing / unloadable.
        ValueError: the file could not be decoded as an image.
    """
    path = Path(image_path)
    try:
        bgr = _load_bgr(path)
    except FaceDetectionUnavailable:
        raise
    except Exception as exc:
        raise ValueError(f"Failed to decode image {path}: {exc}") from exc

    orig_h, orig_w = bgr.shape[:2]
    if orig_w == 0 or orig_h == 0:
        raise ValueError(f"Image has zero dimension: {path}")

    # Downscale for detection (percent output is scale-invariant).
    scale = min(1.0, _MAX_DETECT_SIDE / float(max(orig_w, orig_h)))
    if scale < 1.0:
        det_w = max(1, int(round(orig_w * scale)))
        det_h = max(1, int(round(orig_h * scale)))
        det_img = cv2.resize(bgr, (det_w, det_h), interpolation=cv2.INTER_AREA)
    else:
        det_w, det_h = orig_w, orig_h
        det_img = bgr

    detector = _get_detector()
    with _detector_lock:
        detector.setInputSize((det_w, det_h))
        _retval, raw = detector.detect(det_img)

    faces: List[Dict[str, Any]] = []
    if raw is not None:
        for row in raw:
            # YuNet row layout: [x, y, w, h, 5x landmark (x,y)..., score]
            x, y, w, h = float(row[0]), float(row[1]), float(row[2]), float(row[3])
            score = float(row[-1])
            # Clamp box into image bounds, then normalize to percent.
            x = max(0.0, min(x, det_w))
            y = max(0.0, min(y, det_h))
            w = max(0.0, min(w, det_w - x))
            h = max(0.0, min(h, det_h - y))
            if w <= 0 or h <= 0:
                continue
            faces.append(
                {
                    "x_pct": round(x / det_w * 100.0, 2),
                    "y_pct": round(y / det_h * 100.0, 2),
                    "w_pct": round(w / det_w * 100.0, 2),
                    "h_pct": round(h / det_h * 100.0, 2),
                    "confidence": round(score, 4),
                    "is_primary": False,
                    # private pixel-space fields for primary selection, stripped below
                    "_area": (w / det_w) * (h / det_h),
                    "_cx": (x + w / 2.0) / det_w,
                    "_cy": (y + h / 2.0) / det_h,
                }
            )

    focal_point: Optional[Dict[str, float]] = None
    if faces:
        # Primary = largest area; tie-break = closest to image center.
        def _rank(f: Dict[str, Any]):
            dist_to_center = (f["_cx"] - 0.5) ** 2 + (f["_cy"] - 0.5) ** 2
            return (-f["_area"], dist_to_center)

        primary = min(faces, key=_rank)
        primary["is_primary"] = True
        focal_point = {
            "x_pct": round(primary["_cx"] * 100.0, 1),
            "y_pct": round(primary["_cy"] * 100.0, 1),
        }

    # Strip private helper fields before returning.
    for f in faces:
        f.pop("_area", None)
        f.pop("_cx", None)
        f.pop("_cy", None)

    return {
        "focal_point": focal_point,
        "faces_detected": len(faces),
        "faces": faces,
        "image_width": orig_w,
        "image_height": orig_h,
        "model": MODEL_NAME,
        "computed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
