"""#3597: example and template files must never carry a real key.

`.env.aiserver.example` shipped the live storage master key of aiserver and the
O'Neal API key; every checkout and rollback archive on the customer VMs held
them. These tests keep that from coming back:

1. In every ``*.example`` file (``.env.example``, ``x.example.json`` ...) a
   setting whose name says key/secret/token/password holds a placeholder
   (empty, ``<setzen>``, ``{{SECRET}}``, ``${VAR}``, ``null``) or a file path,
   never a value.
2. No ``*.example`` file holds a URL with credentials or a token with a
   well-known provider prefix.
3. The keys burnt in #3597 appear nowhere in the tree (compared by SHA-256, so
   this file does not repeat them). The git history still has them; only the
   rotation (#2937) makes them worthless.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", "venv", ".venv", "node_modules", "__pycache__", ".pytest_cache"}

# SHA-256 of keys that leaked through the repo (#3597). Never add the values.
BURNT_KEY_HASHES = {
    "060bb4cf7fc51b2ac58a59780ecfda1ccff8912f55d23d278945c7828994e031",  # aiserver API_KEY
    "c579de24b524e572e19b7363825e2135857d904adc4865b2a90ffc85b190d460",  # O'Neal API key
}

_SECRET_NAME = re.compile(r"(key|secret|token|passw(or)?d|pwd|credential)", re.IGNORECASE)
_PLACEHOLDER = re.compile(r"^(|<[^<>]*>|\{\{[^{}]*\}\}|\$\{[^{}]*\}|null|none|/.*)$", re.IGNORECASE)
_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")
_KV_LINE = re.compile(r"""^\s*["']?([A-Za-z_][A-Za-z0-9_.-]*)["']?\s*:\s*(.*?)\s*,?\s*$""")
_URL_CREDS = re.compile(r"://[^/\s:@<>{}$]+:(?!<|\{\{|\$\{)[^@\s/]+@")
_PROVIDER_TOKEN = re.compile(r"\b(sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
                             r"|xox[abp]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,})")
_TOKEN = re.compile(r"[A-Za-z0-9_.+/=-]{6,128}")


def _walk():
    stack = [ROOT]
    while stack:
        d = stack.pop()
        for p in d.iterdir():
            if p.is_dir():
                if p.name not in SKIP_DIRS and not p.is_symlink():
                    stack.append(p)
            elif p.is_file():
                yield p


def _example_files():
    return [p for p in _walk() if ".example" in p.name]


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return v.strip()


def find_problems(text: str) -> list[str]:
    """Return one line per finding, naming the setting but never its value."""
    problems = []
    for n, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "//", ";")):
            continue
        m = _ENV_LINE.match(line) or _KV_LINE.match(line)
        if m and _SECRET_NAME.search(m.group(1)):
            value = _unquote(m.group(2).split(" #", 1)[0])
            if value not in ("{", "[") and not _PLACEHOLDER.match(value):
                problems.append(f"line {n}: {m.group(1)} has a value, expected a placeholder like <setzen>")
        if _URL_CREDS.search(line):
            problems.append(f"line {n}: URL with credentials")
        if _PROVIDER_TOKEN.search(line):
            problems.append(f"line {n}: provider token")
    return problems


def test_example_files_hold_placeholders_only():
    files = _example_files()
    assert any(p.name == ".env.aiserver.example" for p in files), "scanner found no example files"
    found = {}
    for p in files:
        problems = find_problems(p.read_text(encoding="utf-8", errors="replace"))
        if problems:
            found[str(p.relative_to(ROOT))] = problems
    assert not found, found


def test_burnt_keys_appear_nowhere():
    hits = []
    for p in _walk():
        try:
            if p.stat().st_size > 2_000_000:
                continue
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for tok in set(_TOKEN.findall(text)):
            if hashlib.sha256(tok.encode()).hexdigest() in BURNT_KEY_HASHES:
                hits.append(str(p.relative_to(ROOT)))
                break
    assert not hits, f"a key burnt in #3597 is back in: {hits}"


def test_checker_flags_real_values():
    fake = "x" * 4 + "7" * 4 + "q" * 12  # built at runtime: no key-shaped literal here
    bad = "\n".join([
        f"API_KEY={fake}",
        f'"client_secret": "{fake}",',
        f"DATABASE_URL=postgresql://user:{fake}@localhost/db",
        "OPENAI_API_KEY=" + "sk-" + fake,
    ])
    problems = find_problems(bad)
    assert len([p for p in problems if "placeholder" in p]) == 3, problems
    assert any("URL with credentials" in p for p in problems)
    assert any("provider token" in p for p in problems)
    assert all(fake not in p for p in problems)


def test_checker_accepts_placeholders():
    good = "\n".join([
        "API_KEY=<setzen>", "GEMINI_API_KEY=", 'API_KEY: "{{SECRET}}"', "TOKEN=${STORAGE_TOKEN}",
        "SSL_KEY=/etc/ssl/private/x.key", "DATABASE_URL=postgresql://user:<setzen>@localhost/db",
        "# OPENAI_API_KEY=commented-out-is-ignored", "TRANSCODING_MODE=disabled",
    ])
    assert find_problems(good) == []
