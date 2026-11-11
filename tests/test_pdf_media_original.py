"""PDFs kommen über /storage/media im Original, die PNG-Vorschau nur auf Bildwunsch (#2724)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage import routes  # noqa: E402


def _req(**kw):
    base = dict(variant=None, format=None, width=None, height=None,
                aspect_ratio=None, trim=None, download=False)
    base.update(kw)
    return routes._pdf_preview_requested(**base)


def test_original_ohne_bildparameter():
    assert not _req()
    assert not _req(variant="full")
    assert not _req(format="original")
    assert not _req(trim=False)


def test_download_liefert_immer_das_original():
    assert not _req(download=True)
    assert not _req(download=True, variant="thumbnail")


def test_vorschau_bei_bildwunsch():
    assert _req(variant="thumbnail")
    assert _req(variant="Medium")
    assert _req(format="jpg")
    assert _req(format="webp")
    assert _req(width=400)
    assert _req(height=300)
    assert _req(aspect_ratio="16:9")
    assert _req(trim=True)
