"""#3631: what a service key with a route profile may call.

A tenant key without a profile authenticates on most routes, so "this key is
for signing and grants" was only a label. A profile makes it a positive list,
enforced by the outermost middleware in main.py before auth or a handler runs.
An unknown profile name allows nothing.

Who holds which profile (read from the callers' code, 21.11.2026):
  portal-signer  cloud-api, media_signing.py
  post-signer    content-api, services/storage_signing.py
  ingest         comm-api, incoming attachments
  project-uploader  project-api, attachments in the Project Canvas (23.11.2026):
                 stores private files as its own machine account, binds them to
                 the project's agents and signs them for the Canvas, own objects only
  youtube-uploader  youtube-api (#3958, Alex 25.11.2026): stores private
                 transcripts, frames and media as its own machine account, bound
                 to cloud-session:YouTube only, and lists its own objects as a
                 download cache. No sign, no delete, no grants.
  cloud-node     cloud-api on a node outside the core (studio, #4157, Cloud
                 27.11.2026): stores private and encrypted attachments and
                 history exports as its own machine account, reads and deletes
                 only those. No sign, no grants, no listing, nothing public.
"""
import re
from typing import Callable, Iterable, Mapping, Optional

_OBJECT = r"/storage/objects/[0-9]+"

PORTAL_SIGNER = "portal-signer"
POST_SIGNER = "post-signer"
INGEST = "ingest"
PROJECT_UPLOADER = "project-uploader"
YOUTUBE_UPLOADER = "youtube-uploader"
CLOUD_NODE = "cloud-node"

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
    PROJECT_UPLOADER: (
        ("POST", "/storage/upload"),
        ("POST", "/storage/sign"),
        ("POST", _OBJECT + "/grants"),
        ("DELETE", _OBJECT + "/grants"),
    ),
    YOUTUBE_UPLOADER: (
        ("POST", "/storage/upload"),
        ("GET", "/storage/list"),
    ),
    CLOUD_NODE: (
        ("POST", "/storage/upload"),
        ("GET", _OBJECT),
        ("DELETE", _OBJECT),
        ("GET", r"/storage/media/[0-9]+"),
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
# project-uploader binds to agents and to the scopes its key may sign, nothing else.
_PROJECT_SCOPE_PREFIXES = ("cloud-session:", "cloud-session-x:")
_INGEST_REFUSED_HEADERS = ("x-on-behalf-of", "x-compute-focal")
# youtube-uploader: the ingest fields plus what its download cache looks up by
# (collection_id, link_id) and the two AI switches, pinned to their defaults.
# reuse_existing may stay on: it only ever matches the caller's own objects.
_YOUTUBE_FIELDS = _INGEST_FIELDS | {"collection_id", "link_id", "ai_mode", "analyze"}
YOUTUBE_SCOPE = "cloud-session:YouTube"
_YOUTUBE_TAG_RE = re.compile(r"[A-Za-z0-9._-]{1,80}")
# cloud-node: exactly the fields cloud-api sends (_upload_to_storage,
# _upload_verschluesselt). Agent scopes only, as for ingest.
_CLOUD_NODE_FIELDS = (_INGEST_FIELDS - {"original_filename"}) | {"collection_id", "ai_mode", "ttl_hours", "encrypted"}
_CLOUD_NODE_SCOPE_PREFIXES = ("cloud-session:", "cloud-session-x:")


def route_allowed(profile: str, method: str, path: str) -> bool:
    return any(m == method and p.fullmatch(path) for m, p in _COMPILED.get(profile, ()))


def ingest_upload_problem(form, headers: Mapping[str, str]) -> Optional[str]:
    """Why this ingest upload is refused, or None. Runs before anything is stored."""
    return _private_upload_problem(
        form, headers, lambda scope: scope.startswith(_INGEST_SCOPE_PREFIXES),
        "grant_scope must be an agent or comm-message scope")


def project_scope_allowed(scope: str, own_scopes: Iterable[str]) -> bool:
    """A scope project-uploader may bind: an agent, or one its key may sign."""
    return scope.startswith(_PROJECT_SCOPE_PREFIXES) or scope in set(own_scopes)


def project_upload_problem(form, headers: Mapping[str, str], own_scopes: Iterable[str]) -> Optional[str]:
    """Why this project-uploader upload is refused, or None. Runs before anything is stored."""
    own = set(own_scopes)
    return _private_upload_problem(
        form, headers, lambda scope: project_scope_allowed(scope, own),
        "grant_scope must be an agent scope or this key's own scope")


def youtube_upload_problem(form, headers: Mapping[str, str]) -> Optional[str]:
    """Why this youtube-uploader upload is refused, or None. Runs before anything is stored."""
    problem = _private_upload_problem(
        form, headers, lambda scope: scope == YOUTUBE_SCOPE,
        f"grant_scope must be {YOUTUBE_SCOPE}", fields=_YOUTUBE_FIELDS, reuse_values=("true", "false", None))
    if problem:
        return problem
    if form.get("ai_mode", "none") != "none":
        return "ai_mode must be none"
    if form.get("analyze", "false") != "false":
        return "analyze must be false"
    for name in ("collection_id", "link_id"):
        value = form.get(name)
        if value is not None and not _YOUTUBE_TAG_RE.fullmatch(str(value)):
            return f"{name} must be 1-80 of A-Z a-z 0-9 . _ -"
    return None


def cloud_node_upload_problem(form, headers: Mapping[str, str]) -> Optional[str]:
    """Why this cloud-node upload is refused, or None. Runs before anything is stored.

    Never public. private may be missing: cloud-api stores history exports
    with is_public=false only, which no anonymous or foreign caller reads."""
    problem = _private_upload_problem(
        form, headers, lambda scope: scope.startswith(_CLOUD_NODE_SCOPE_PREFIXES),
        "grant_scope must be an agent scope", fields=_CLOUD_NODE_FIELDS,
        private_values=("true", None))
    if problem:
        return problem
    if form.get("ai_mode", "none") != "none":
        return "ai_mode must be none"
    if form.get("encrypted", "false") not in ("true", "false"):
        return "encrypted must be true or false"
    ttl = form.get("ttl_hours")
    if ttl is not None and not re.fullmatch(r"[0-9]{1,6}", str(ttl)):
        return "ttl_hours must be a whole number of hours"
    value = form.get("collection_id")
    if value is not None and not _YOUTUBE_TAG_RE.fullmatch(str(value)):
        return "collection_id must be 1-80 of A-Z a-z 0-9 . _ -"
    return None


def _private_upload_problem(form, headers: Mapping[str, str], scope_ok: Callable[[str], bool],
                            scope_error: str, fields=_INGEST_FIELDS,
                            reuse_values=("false",), private_values=("true",)) -> Optional[str]:
    for header in _INGEST_REFUSED_HEADERS:
        if headers.get(header) is not None:
            return f"header not allowed: {header}"
    names = [name for name, _ in form.multi_items()]
    if len(names) != len(set(names)):
        return "a form field was sent twice"
    unknown = sorted(set(names) - set(fields))
    if unknown:
        return f"form field not allowed: {unknown[0]}"
    if "file" not in names or isinstance(form.get("file"), str):
        return "a file is required"
    if form.get("is_public", "false") != "false":
        return "is_public must be false"
    if form.get("private") not in private_values:
        return "private must be true"
    if form.get("reuse_existing") not in reuse_values:
        return "reuse_existing must be false"
    scopes = form.get("grant_scope")
    if scopes is not None:
        parts = [part.strip() for part in str(scopes).split(",") if part.strip()]
        if not parts or not all(scope_ok(part) for part in parts):
            return scope_error
    return None
