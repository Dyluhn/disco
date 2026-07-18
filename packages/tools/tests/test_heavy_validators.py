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


def _contentful_pptx_bytes() -> bytes:
    """A real deck with MEANINGFUL extractable content (C9-01: a blank deck renders to a
    text-free PDF, which the PDF validator rightly rejects — the fixture must carry real
    text; the validator must not be weakened)."""
    import io

    from pptx import Presentation

    prs = Presentation()
    title = prs.slides.add_slide(prs.slide_layouts[0])
    title.shapes.title.text = "Heavy Validator Fixture Deck"
    title.placeholders[1].text = "Rendered by headless LibreOffice in the G17 lane"
    body = prs.slides.add_slide(prs.slide_layouts[1])
    body.shapes.title.text = "Extractable Content"
    body.placeholders[1].text_frame.text = "This bullet proves pdftotext sees real text."
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def test_validate_pptx_renders_guards_when_soffice_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """The unavailable-tool guard, exercised WITHOUT skipping regardless of whether the
    host has LibreOffice (C9-01): an isolated empty PATH makes ``soffice`` unresolvable
    for this test only."""
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    assert shutil.which("soffice") is None
    problems = validate_pptx_renders("anything.pptx")
    assert problems == [
        "soffice unavailable — run this heavy validator on the VM 201 evidence host"
    ]


@pytest.mark.skipif(shutil.which("soffice") is None, reason="LibreOffice not installed")
def test_validate_pptx_renders_real_clean_pptx():
    """When soffice IS present, a CONTENTFUL deck renders with no problems — the render
    path is exercised end-to-end (pinned-filter convert → PDF → page/text checks)."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "deck.pptx"
        p.write_bytes(_contentful_pptx_bytes())
        assert validate_pptx_renders(str(p)) == []


def test_validate_pptx_renders_flags_corrupt_zip():
    """A zip-INVALID .pptx must be flagged even though LibreOffice EXITS ZERO on
    "source file could not be loaded" (C9-01): the pinned Impress import filter blocks
    the generic text-import fallback, and the fresh per-invocation outdir + isolated
    user profile mean a missing output is THIS invocation's failure — never a stale or
    foreign PDF standing in."""
    if shutil.which("soffice") is None:
        pytest.skip("LibreOffice not installed")
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "broken.pptx"
        p.write_bytes(b"this is not a pptx")
        problems = validate_pptx_renders(str(p))
        assert problems, "corrupt pptx produced an empty problem list"
        assert "did not render" in problems[0] or "convert failed" in problems[0]
