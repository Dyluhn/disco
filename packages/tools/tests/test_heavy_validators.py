"""W14b — heavy (render-it) validators. poppler (pdftoppm/pdfinfo) is present in CI-lite and
on the VM 201 host, so the PDF rasterizer is unit-tested here; LibreOffice (soffice) is
VM-only, so the pptx renderer's guard path is tested here and the real render runs on VM 201
(proven live against a generated W3-clean deck — see test-record/e2e-full/artifacts/)."""

from __future__ import annotations

import pathlib
import shutil

import pytest
from disco.tools.verify.heavy_validators import validate_pdf_renders, validate_pptx_renders

_REPO = pathlib.Path(__file__).resolve().parents[3]
_PDF = _REPO / "packages/tools/tests/fixtures/artifacts/sample_report.pdf"


@pytest.mark.skipif(shutil.which("pdftoppm") is None, reason="poppler not installed")
def test_validate_pdf_renders_real_pdf_nonblank():
    assert validate_pdf_renders(str(_PDF)) == []


@pytest.mark.skipif(shutil.which("pdftoppm") is None, reason="poppler not installed")
def test_validate_pdf_renders_flags_missing():
    problems = validate_pdf_renders("/no/such/file.pdf")
    assert problems and "failed" in problems[0]


def test_validate_pptx_renders_guards_when_soffice_absent(monkeypatch):
    """When soffice is missing (the PR gate), the heavy validator returns a clear note rather
    than raising — so it degrades to a skip instead of a false failure."""
    real_which = shutil.which
    monkeypatch.setattr(
        shutil,
        "which",
        lambda command: None if command == "soffice" else real_which(command),
    )
    problems = validate_pptx_renders("anything.pptx")
    assert problems == [
        "soffice unavailable — run this heavy validator on the VM 201 evidence host"
    ]


@pytest.mark.skipif(shutil.which("soffice") is None, reason="LibreOffice not installed")
def test_validate_pptx_renders_real_clean_pptx():
    """codex round-3: when soffice IS present, a clean pptx renders with no problems — the
    render path is actually exercised (the guard-only test never ran it)."""
    import io
    import tempfile

    from pptx import Presentation

    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "deck.pptx"
        buf = io.BytesIO()
        Presentation().save(buf)
        p.write_bytes(buf.getvalue())
        assert validate_pptx_renders(str(p)) == []


def test_validate_pptx_renders_flags_corrupt_zip():
    """A zip-INVALID .pptx must be flagged (soffice convert fails), not pass."""
    if shutil.which("soffice") is None:
        pytest.skip("LibreOffice not installed")
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "broken.pptx"
        p.write_bytes(b"this is not a pptx")
        assert validate_pptx_renders(str(p))  # non-empty problems
