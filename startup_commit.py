"""#3508 (Beitrag 6262, Entscheidung 4): the commit this process runs.

Read once at start, reported by GET /health as ``commit``. Never guessed:

1. ``.deployed-rev`` next to main.py, written by the deploy before the
   restart (deploy-arkserver.sh). On arkserver the live directory is not a
   real checkout: its git HEAD still says 28421fc (11.08.) while the files are
   the deployed release, so this file is the only truthful source there.
   Present but unreadable or not a full SHA → ``None``, no fallback to git.
2. Without that file: ``git rev-parse HEAD`` of this directory, only when it
   is the top of its own checkout and no tracked file differs from HEAD. A
   modified checkout runs code that is not HEAD, so it reports ``None``.
3. Anything else → ``None``.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Optional

MARKER = ".deployed-rev"
_SHA = re.compile(r"^[0-9a-f]{40}$")


def _git(base: Path, *args: str) -> Optional[subprocess.CompletedProcess]:
    try:
        return subprocess.run(["git", "-C", str(base), *args], capture_output=True,
                              text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def read_startup_commit(base: Path) -> Optional[str]:
    marker = base / MARKER
    if marker.exists():
        try:
            sha = marker.read_text(encoding="ascii").strip()
        except (OSError, UnicodeDecodeError):
            return None
        return sha if _SHA.match(sha) else None

    head = _git(base, "rev-parse", "--show-toplevel", "HEAD")
    if head is None or head.returncode != 0:
        return None
    lines = head.stdout.splitlines()
    # A directory inside some other repository is not "this checkout".
    if len(lines) != 2 or Path(lines[0]).resolve() != base.resolve():
        return None
    dirty = _git(base, "status", "--porcelain", "--untracked-files=no")
    if dirty is None or dirty.returncode != 0 or dirty.stdout.strip():
        return None
    sha = lines[1]
    return sha if _SHA.match(sha) else None
