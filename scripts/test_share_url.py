"""Name rules for share links (storage.url_builder). Run: python scripts/test_share_url.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["STORAGE_SHARE_BASE_URL"] = "https://files.example.test/"

from storage.url_builder import _share_name, build_share_url  # noqa: E402

NAMES = {
    "Größe & Maß (final).png": "Groesse-Mass-final.png",
    "Bildschirmfoto 2026-10-08 um 22.57.26.png": "Bildschirmfoto-2026-10-08-um-22.57.26.png",
    "../../etc/passwd": "passwd",
    "C:\\x\\Foto 1.jpg": "Foto-1.jpg",
    "日本.png": "",
    " - .jpg": "",
    ".env": "",
    "README": "README",
    "x" * 300 + ".jpeg": "x" * 115 + ".jpeg",
    None: "",
}

failed = 0
for raw, want in NAMES.items():
    got = _share_name(raw)
    if got != want:
        failed += 1
        print(f"FAIL {raw!r}: {got!r} != {want!r}")

checks = [
    (build_share_url(7, "a b.pdf", True), "https://files.example.test/7/a-b.pdf"),
    (build_share_url(7, "日本.png", True), "https://files.example.test/7"),
    (build_share_url(7, "a.pdf", False), None),
]
os.environ["STORAGE_SHARE_BASE_URL"] = ""
checks.append((build_share_url(7, "a.pdf", True), None))
for got, want in checks:
    if got != want:
        failed += 1
        print(f"FAIL {got!r} != {want!r}")

print("ALL PASS" if not failed else f"{failed} FAILED")
sys.exit(1 if failed else 0)
