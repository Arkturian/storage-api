#!/usr/bin/env python3
"""Regression suite for the chunked-upload plausibility gate.

Incident 2026-08-09 (object 118592): Tschepp-Codex2 base64-encoded an already
base64-encoded PNG. Storage decoded once, got base64 TEXT back, and stored
2.3 MB of ASCII as `text/plain` under a `.png` name without complaint.

These tests pin the three behaviours that must hold:
  1. a correct upload still passes  (no false positives — the regression risk)
  2. a double-encoded upload is REJECTED
  3. a legitimate text file full of base64 characters still passes
"""
import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import HTTPException
from storage.routes import _assert_upload_plausible, _looks_like_base64_text

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x01\x00\x00\x00\x01\x00"
    b"\x08\x06\x00\x00\x00\\r\xa8f" + bytes(range(256)) * 20
)

failures = []


def check(name, fn):
    try:
        fn()
        print(f"  ok   {name}")
    except AssertionError as exc:
        print(f"  FAIL {name}: {exc}")
        failures.append(name)


def rejects(data, filename, sniffed, expect_code):
    try:
        _assert_upload_plausible(data, filename, sniffed)
    except HTTPException as exc:
        actual = exc.detail.get("code")
        assert actual == expect_code, f"expected code {expect_code}, got {actual}"
        return
    raise AssertionError(f"expected rejection ({expect_code}) but it passed")


def accepts(data, filename, sniffed):
    try:
        _assert_upload_plausible(data, filename, sniffed)
    except HTTPException as exc:
        raise AssertionError(f"expected pass, got {exc.detail}")


print("1. correct uploads must still pass (false-positive guard)")
check("real PNG bytes under .png", lambda: accepts(PNG, "shot.png", "image/png"))
check("real PDF bytes under .pdf", lambda: accepts(b"%PDF-1.4\n" + bytes(range(256)) * 20, "d.pdf", "application/pdf"))
check("small file below probe size", lambda: accepts(b"\x89PNG\r\n\x1a\n tiny", "t.png", "image/png"))
check("unknown extension is not judged", lambda: accepts(base64.b64encode(PNG), "blob.xyzzy", "text/plain"))
check("mp4 with ftyp at offset 4", lambda: accepts(b"\x00\x00\x00\x18ftypisom" + bytes(200), "clip.mp4", "video/mp4"))
check("webp RIFF....WEBP", lambda: accepts(b"RIFF\x00\x00\x00\x00WEBPVP8 " + bytes(200), "i.webp", "image/webp"))
check("glb", lambda: accepts(b"glTF\x02\x00\x00\x00" + bytes(200), "m.glb", "model/gltf-binary"))
check("bare-frame mp3 tolerated (soft signature)", lambda: accepts(b"\xff\xfb\x90\x00" + bytes(500), "a.mp3", "audio/mpeg"))
check("docx is a zip", lambda: accepts(b"PK\x03\x04" + bytes(300), "d.docx", None))

print("2. the incident itself must be rejected")
check(
    "double-encoded PNG under .png",
    lambda: rejects(base64.b64encode(PNG), "shot.png", "text/plain", "double_encoded"),
)
check(
    "double-encoded JPEG under .jpg",
    lambda: rejects(base64.b64encode(b"\xff\xd8\xff\xe0" + bytes(range(256)) * 20), "p.jpg", "text/plain", "double_encoded"),
)
check(
    "plain prose under .png (mismatch, not base64)",
    lambda: rejects(b"Dear team, this is a note. " * 200, "shot.png", "text/plain", "content_type_mismatch"),
)

print("3. legitimate text must NOT be caught")
check("base64-looking .txt is fine", lambda: accepts(base64.b64encode(PNG), "dump.txt", "text/plain"))
check("PEM certificate is legitimately base64", lambda: accepts(base64.b64encode(PNG), "cert.pem", "text/plain"))
check("markdown under .md", lambda: accepts(b"# Title\n\nSome text.\n" * 50, "readme.md", "text/plain"))
check("svg is text, not judged", lambda: accepts(b"<svg xmlns='http://www.w3.org/2000/svg'></svg>" * 40, "i.svg", "image/svg+xml"))
check("json is text, not judged", lambda: accepts(b'{"a":1}' * 200, "d.json", "application/json"))

print("4. the base64 sniffer itself")
check("detects long base64 run", lambda: (_ for _ in ()).throw(AssertionError("not base64")) if not _looks_like_base64_text(base64.b64encode(PNG)) else None)
check("rejects prose (has spaces)", lambda: (_ for _ in ()).throw(AssertionError("false positive")) if _looks_like_base64_text(b"hello world " * 100) else None)
check("rejects real binary", lambda: (_ for _ in ()).throw(AssertionError("false positive")) if _looks_like_base64_text(PNG) else None)
check("ignores too-short input", lambda: (_ for _ in ()).throw(AssertionError("false positive")) if _looks_like_base64_text(b"YWJj") else None)

print()
if failures:
    print(f"✗ {len(failures)} failed: {', '.join(failures)}")
    sys.exit(1)
print("✓ all plausibility tests passed")
