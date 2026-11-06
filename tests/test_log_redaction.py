"""`sig` of a signed media link must never reach the access log (Issue #2656)."""
import logging

import pytest

from storage.log_redaction import (
    RedactSignatureFilter,
    install_access_log_redaction,
    redact_signature,
)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("/storage/media/1?exp=9&kid=k&sig=abc123", "/storage/media/1?exp=9&kid=k&sig=***"),
        ("/storage/media/1?sig=abc123&format=webp", "/storage/media/1?sig=***&format=webp"),
        ("/storage/media/1?exp=9&sig=abc&variant=large", "/storage/media/1?exp=9&sig=***&variant=large"),
        ("/storage/media/1?exp=9&sig=abc%22,%22x%22:%22y", "/storage/media/1?exp=9&sig=***"),
        ("/storage/media/1?SIG=abc", "/storage/media/1?SIG=***"),
        ("/storage/media/1?design=abc&signature=x", "/storage/media/1?design=abc&signature=x"),
        ("/storage/media/1", "/storage/media/1"),
    ],
)
def test_redact_signature(raw, expected):
    assert redact_signature(raw) == expected


def _access_record(path):
    return logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:0", "GET", path, "1.1", 200), None,
    )


def test_filter_masks_formatted_line_and_keeps_record():
    record = _access_record("/storage/media/1?exp=9&kid=k&sig=secretvalue")
    assert RedactSignatureFilter().filter(record) is True
    line = record.getMessage()
    assert "secretvalue" not in line
    assert line == '127.0.0.1:0 - "GET /storage/media/1?exp=9&kid=k&sig=*** HTTP/1.1" 200'


def test_filter_leaves_non_string_args_alone():
    record = _access_record("/x")
    RedactSignatureFilter().filter(record)
    assert record.args[-1] == 200


def test_install_is_idempotent():
    logger = logging.getLogger("uvicorn.access")
    install_access_log_redaction()
    install_access_log_redaction()
    assert sum(isinstance(f, RedactSignatureFilter) for f in logger.filters) == 1
