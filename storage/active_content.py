"""Neutralise active documents served from storage origins (#2916).

/storage/media/{id} and /storage/files/{id} hand out uploaded bytes with their
stored MIME type, inline, from api-storage.arkturian.com and files.arkturian.com.
An uploaded HTML or SVG file therefore ran its scripts on a storage origin
(stored XSS). Blocking the types would break the 124 HTML and 302 SVG objects
people do view; instead every response whose Content-Type is an active
document type gets

    Content-Security-Policy: sandbox allow-scripts allow-forms allow-popups
                             allow-modals allow-downloads
    X-Content-Type-Options: nosniff

`sandbox` without allow-same-origin gives the document an opaque origin: its
scripts may run (prototypes keep working) but can neither act as the storage
origin nor read its cookies, storage or same-origin responses. When an SVG is
embedded via <img>, scripts never run there anyway.

Implemented as a raw ASGI middleware so streaming and file responses are not
buffered.
"""

from __future__ import annotations

_ACTIVE_TYPES = {
    "text/html",
    "application/xhtml+xml",
    "image/svg+xml",
    "text/xml",
    "application/xml",
    "application/javascript",
    "text/javascript",
    "application/ecmascript",
    "text/ecmascript",
    "application/x-javascript",
}

# allow-scripts WITHOUT allow-same-origin: interactive HTML prototypes people
# open by link (e.g. 124899, a Solmatch test app) keep working, but their
# scripts run in an opaque origin. They cannot act as, or read anything of,
# api-storage.arkturian.com / files.arkturian.com, which is what made an
# uploaded HTML/SVG file a stored-XSS vector.
_CSP = b"sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads"

# The interactive API docs are the service's own HTML and need scripts.
_EXEMPT_PREFIXES = ("/docs", "/redoc")


def is_active_content_type(value: str) -> bool:
    base = (value or "").split(";", 1)[0].strip().lower()
    return base in _ACTIVE_TYPES or base.endswith("+xml")


class ActiveContentGuard:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("path", "").startswith(_EXEMPT_PREFIXES):
            await self.app(scope, receive, send)
            return

        async def guarded_send(message):
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers") or [])
                ctype = next((v for k, v in headers if k.lower() == b"content-type"), b"")
                if is_active_content_type(ctype.decode("latin-1")):
                    headers = [(k, v) for k, v in headers
                               if k.lower() not in (b"content-security-policy", b"x-content-type-options")]
                    headers.append((b"content-security-policy", _CSP))
                    headers.append((b"x-content-type-options", b"nosniff"))
                    message = dict(message, headers=headers)
            await send(message)

        await self.app(scope, receive, guarded_send)
