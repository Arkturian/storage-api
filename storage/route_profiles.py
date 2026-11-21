"""#3631: what a service key with a route profile may call.

A tenant key without a profile authenticates on most routes, so "this key is
for signing and grants" was only a label. A profile makes it a positive list,
enforced by the outermost middleware in main.py before auth or a handler runs.
An unknown profile name allows nothing.

Who holds which profile (read from the callers' code, 21.11.2026):
  portal-signer  cloud-api, media_signing.py
  post-signer    content-api, services/storage_signing.py
  ingest         comm-api, incoming attachments
"""
import re
from typing import Mapping, Optional

_OBJECT = r"/storage/objects/[0-9]+"

PORTAL_SIGNER = "portal-signer"
POST_SIGNER = "post-signer"
INGEST = "ingest"

ROUTE_PROFILES = {
    PORTAL_SIGNER: (
        ("POST", "/storage/sign"),
        ("POST", "/storage/grants/move"),
        ("POST", "/storage/grants/share"),
        ("POST", "/storage/grants/unshare"),
        ("POST", "/storage/grants/lookup"),
        ("POST", _OBJECT + "/share-link"),
        ("GET", _OBJECT + "/share-links"),
        ("DELETE", _OBJECT + "/share-links/[0-9]+"),
    ),
    POST_SIGNER: (
        ("POST", "/storage/sign"),
        ("POST", "/storage/grants/share"),
        ("POST", "/storage/grants/unshare"),
        ("POST", _OBJECT + "/grants"),
        ("DELETE", _OBJECT + "/grants"),
    ),
    INGEST: (
        ("POST", "/storage/upload"),
    ),
}

_COMPILED = {
    name: tuple((method, re.compile(pattern)) for method, pattern in routes)
    for name, routes in ROUTE_PROFILES.items()
}

# The only form fields an ingest upload may carry. A positive list, so a field
# added to /storage/upload later is refused until someone decides otherwise.
_INGEST_FIELDS = {"file", "original_filename", "context", "is_public", "private", "reuse_existing", "grant_scope"}
_INGEST_SCOPE_PREFIXES = ("cloud-session:", "cloud-session-x:", "comm-message:")
_INGEST_REFUSED_HEADERS = ("x-on-behalf-of", "x-compute-focal")


def route_allowed(profile: str, method: str, path: str) -> bool:
    return any(m == method and p.fullmatch(path) for m, p in _COMPILED.get(profile, ()))


def ingest_upload_problem(form, headers: Mapping[str, str]) -> Optional[str]:
    """Why this ingest upload is refused, or None. Runs before anything is stored."""
    for header in _INGEST_REFUSED_HEADERS:
        if headers.get(header) is not None:
            return f"header not allowed: {header}"
    names = [name for name, _ in form.multi_items()]
    if len(names) != len(set(names)):
        return "a form field was sent twice"
    unknown = sorted(set(names) - _INGEST_FIELDS)
    if unknown:
        return f"form field not allowed: {unknown[0]}"
    if "file" not in names or isinstance(form.get("file"), str):
        return "a file is required"
    if form.get("is_public", "false") != "false":
        return "is_public must be false"
    if form.get("private") != "true":
        return "private must be true"
    if form.get("reuse_existing") != "false":
        return "reuse_existing must be false"
    scopes = form.get("grant_scope")
    if scopes is not None:
        parts = [part.strip() for part in str(scopes).split(",") if part.strip()]
        if not parts or not all(part.startswith(_INGEST_SCOPE_PREFIXES) for part in parts):
            return "grant_scope must be an agent or comm-message scope"
    return None
