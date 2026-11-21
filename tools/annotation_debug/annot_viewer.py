#!/usr/bin/env python3
"""Quick visualizer for AI annotations stored in storage-api.

Usage:
  python annot_viewer.py --object-id 4601 --api-key <api-key>

The tool fetches the StorageObject metadata and annotations, downloads the
image, and draws anchors plus bounding boxes using matplotlib.  This is useful
to verify that the backend delivers pixel-accurate coordinates independent of
the web UI.
"""

from __future__ import annotations

import argparse
import io
from dataclasses import dataclass
from typing import List, Optional

import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
import numpy as np
import requests
from PIL import Image


DEFAULT_BASE_URL = "https://api-storage.arkturian.com"


@dataclass
class Annotation:
    label: str
    type: Optional[str]
    anchor: dict
    box: Optional[List[float]]
    confidence: Optional[float]
    source: Optional[str]


def _get_json(url: str, api_key: str) -> dict:
    resp = requests.get(url, headers={"X-API-KEY": api_key}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _download_bytes(url: str) -> bytes:
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.content


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render stored annotations on top of the image")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Storage API base URL")
    parser.add_argument("--api-key", required=True, help="API key to access the tenant")
    parser.add_argument("--object-id", type=int, required=True, help="Storage object ID to visualize")
    parser.add_argument("--no-box", action="store_true", help="Hide bounding boxes (anchors only)")
    parser.add_argument(
        "--trim-display",
        action="store_true",
        help="Crop transparent/empty borders before displaying (coordinates adjusted for view)",
    )
    parser.add_argument(
        "--save-trimmed",
        metavar="PATH",
        help="If provided together with --trim-display, write the cropped image to this file",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    base = args.base_url.rstrip("/")
    obj = _get_json(f"{base}/storage/objects/{args.object_id}", args.api_key)
    annotations_payload = _get_json(f"{base}/storage/objects/{args.object_id}/annotations", args.api_key)
    annotations = [Annotation(**ann) for ann in annotations_payload.get("annotations", [])]

    if not annotations:
        print(f"No annotations found for object {args.object_id}.")
        return

    img_url = obj.get("webview_url") or obj.get("file_url") or f"{base}/storage/media/{args.object_id}"
    image_bytes = _download_bytes(img_url)

    pil_image = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    # Replace transparent areas with light gray for visibility
    background = Image.new("RGBA", pil_image.size, (208, 212, 217, 255))
    background.paste(pil_image, mask=pil_image.split()[3])
    image = np.array(background) / 255.0
    original_height, original_width = image.shape[0], image.shape[1]
    height, width = original_height, original_width

    trim_offset = (0, 0)
    if args.trim_display:
        mask = None
        if image.ndim == 3 and image.shape[2] == 4:
            mask = image[:, :, 3] > 0
        elif image.ndim == 3:
            gray = image[:, :, :3].mean(axis=2)
            mask = gray < 0.99  # treat near-white as empty
        else:
            mask = image > 0

        nz = np.argwhere(mask)
        if nz.size:
            (y0, x0), (y1, x1) = nz.min(0), nz.max(0)
            y1 += 1
            x1 += 1
            image = image[y0:y1, x0:x1]
            trim_offset = (x0, y0)
            height, width = image.shape[0], image.shape[1]
            print(f"Trimmed display region to x:[{x0},{x1}) y:[{y0},{y1})")
            if args.save_trimmed:
                trimmed = image
                if trimmed.dtype != np.uint8:
                    trimmed = np.clip(trimmed, 0, 1)
                    trimmed = (trimmed * 255).astype(np.uint8)
                Image.fromarray(trimmed).save(args.save_trimmed)
                print(f"Saved trimmed image to {args.save_trimmed}")
        else:
            print("Warning: trim requested but no non-transparent pixels found; displaying full image")

    fig, ax = plt.subplots(figsize=(8, 8))
    fig.patch.set_facecolor('#d0d4d9')
    ax.set_facecolor('#d0d4d9')
    ax.imshow(image)
    # Outline the visible image bounds to make edges obvious
    ax.add_patch(
        Rectangle(
            (0, 0),
            width,
            height,
            linewidth=1.5,
            edgecolor="#1f2933",
            linestyle="-",
            facecolor="none",
        )
    )

    for ann in annotations:
        anchor_x_px = ann.anchor.get("x", 0) * original_width
        anchor_y_px = ann.anchor.get("y", 0) * original_height

        if args.trim_display:
            anchor_x = anchor_x_px - trim_offset[0]
            anchor_y = anchor_y_px - trim_offset[1]
        else:
            anchor_x = anchor_x_px
            anchor_y = anchor_y_px

        ax.scatter([anchor_x], [anchor_y], c="#10b981", s=70, edgecolors="#064e3b", linewidths=2)

        label = ann.label
        meta = ann.type or "feature"
        conf = ann.confidence
        subtitle = meta
        if isinstance(conf, (int, float)):
            subtitle += f" · {conf * 100:.0f}%"

        ax.text(
            anchor_x + 12,
            anchor_y - 12,
            f"{label}\n{subtitle}",
            color="white",
            fontsize=9,
            bbox=dict(facecolor="navy", alpha=0.75, boxstyle="round,pad=0.35"),
        )

        if not args.no_box and ann.box and len(ann.box) == 4:
            x1, y1, x2, y2 = ann.box
            rect_x1 = x1 * original_width - (trim_offset[0] if args.trim_display else 0)
            rect_y1 = y1 * original_height - (trim_offset[1] if args.trim_display else 0)
            rect_w = (x2 - x1) * original_width
            rect_h = (y2 - y1) * original_height
            rect = Rectangle(
                (rect_x1, rect_y1),
                rect_w,
                rect_h,
                linewidth=1.5,
                edgecolor="#3b82f6",
                linestyle="--",
                facecolor="none",
            )
            ax.add_patch(rect)

    trim_bounds = None
    context_meta = obj.get("ai_context_metadata")
    if isinstance(context_meta, dict):
        trim_bounds = context_meta.get("trim_bounds")

    if trim_bounds:
        normalized = trim_bounds.get("normalized") or [0.0, 0.0, 1.0, 1.0]
        if len(normalized) == 4:
            tx1 = normalized[0] * original_width
            ty1 = normalized[1] * original_height
            tx2 = normalized[2] * original_width
            ty2 = normalized[3] * original_height
            if args.trim_display:
                tx1 -= trim_offset[0]
                ty1 -= trim_offset[1]
                tx2 -= trim_offset[0]
                ty2 -= trim_offset[1]
            ax.add_patch(
                Rectangle(
                    (tx1, ty1),
                    tx2 - tx1,
                    ty2 - ty1,
                    linewidth=2,
                    edgecolor="#f97316",
                    linestyle="-.",
                    facecolor="none",
                )
            )

    ax.set_title(f"Storage Object {args.object_id} – {obj.get('original_filename')}")
    ax.axis("off")
    plt.tight_layout()
    plt.show()


if __name__ == "__main__":
    main()


