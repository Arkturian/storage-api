"""Keep the signature of signed media links out of the access log (Issue #2656).

A signed link works for whoever holds it until it expires, so `sig` must not be
written to a log. `exp` and `kid` stay readable, they carry no access and are
what a failed fetch is debugged with.
"""
import logging
import re

_SIG_RE = re.compile(r"([?&]sig=)[^&\s]*", re.IGNORECASE)


def redact_signature(text: str) -> str:
    """Return text with the value of every `sig` query parameter masked."""
    return _SIG_RE.sub(r"\1***", text)


class RedactSignatureFilter(logging.Filter):
    """Mask `sig` in uvicorn's access records.

    uvicorn passes (client, method, full_path, http_version, status) as args and
    formats them later, so the path has to be rewritten in the args, not in the
    message.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                redact_signature(arg) if isinstance(arg, str) else arg for arg in record.args
            )
        elif isinstance(record.msg, str):
            record.msg = redact_signature(record.msg)
        return True


def install_access_log_redaction() -> None:
    """Attach the filter to uvicorn's access logger, once."""
    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactSignatureFilter) for f in access_logger.filters):
        access_logger.addFilter(RedactSignatureFilter())
