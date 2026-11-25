import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from public_default_fixture import PUBLIC_DEFAULT_TEST_SHA256  # noqa: E402


@pytest.fixture(autouse=True)
def _public_default_stand_in(monkeypatch):
    """Every test recognises PUBLIC_DEFAULT_TEST as the retired public default (#3969)."""
    from storage import public_default
    monkeypatch.setattr(public_default, "PUBLIC_DEFAULT_SHA256", PUBLIC_DEFAULT_TEST_SHA256)
