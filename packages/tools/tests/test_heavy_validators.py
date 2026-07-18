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
    for this test only. The input is a STRUCTURALLY VALID deck so it passes the OPC
    pre-check and actually reaches the soffice-missing guard."""
    empty = tmp_path / "empty-path"
    empty.mkdir()
    deck = tmp_path / "deck.pptx"
    deck.write_bytes(_contentful_pptx_bytes())
    monkeypatch.setenv("PATH", str(empty))
    assert shutil.which("soffice") is None
    problems = validate_pptx_renders(str(deck))
    assert problems == [
        "soffice unavailable — run this heavy validator on the VM 201 evidence host"
    ]


@pytest.mark.skipif(shutil.which("soffice") is None, reason="LibreOffice not installed")
def test_validate_pptx_renders_real_clean_pptx():
    """When soffice IS present, a CONTENTFUL deck renders with no problems — the render
    path is exercised end-to-end (pinned-filter convert → PDF → page/text checks). Also
    covers the C9-01 verifier defect-2 fix: a RELATIVE ``workdir`` is resolved before the
    profile file-URI is built (it used to raise ``ValueError``)."""
    import os
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "deck.pptx"
        p.write_bytes(_contentful_pptx_bytes())
        assert validate_pptx_renders(str(p)) == []
        # A relative workdir must be accepted (resolved), not raise.
        cwd = os.getcwd()
        try:
            os.chdir(d)
            os.mkdir("relwork")
            assert validate_pptx_renders(str(p), workdir="relwork") == []
        finally:
            os.chdir(cwd)


def test_validate_pptx_renders_flags_corrupt_zip():
    """A zip-INVALID .pptx must be flagged BEFORE LibreOffice is even consulted (C9-01):
    the OPC structural pre-check runs first, so ZIP-invalid bytes are rejected on any
    host — no renderer, no skip. (LibreOffice would otherwise EXIT ZERO on "source file
    could not be loaded" and, absent the pinned import filter, recover garbage.)"""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "broken.pptx"
        p.write_bytes(b"this is not a pptx")
        problems = validate_pptx_renders(str(p))
        assert problems, "corrupt pptx produced an empty problem list"
        assert "not a valid zip" in problems[0], problems


def test_validate_pptx_renders_flags_missing_part_that_libreoffice_recovers():
    """C9-01 verifier defect 1: a VALID zip whose slide references a MISSING part
    (a dangling ``_rels`` target) is structurally corrupt even though LibreOffice
    silently recovers and renders it. The OPC integrity pre-check must flag it —
    renderer-independent, so it needs no soffice and never skips."""
    import io
    import tempfile
    import zipfile

    clean = _contentful_pptx_bytes()
    # Drop a slideLayout part while a slide still references it → dangling relationship.
    with zipfile.ZipFile(io.BytesIO(clean)) as zf:
        names = zf.namelist()
        victim = next((n for n in names if "slideLayouts/slideLayout1.xml" in n), None)
        assert victim is not None, "fixture deck has no slideLayout1 to drop"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
            for n in names:
                if n == victim:
                    continue
                out.writestr(n, zf.read(n))
    corrupt = buf.getvalue()
    # Sanity: the mutated archive is still a VALID zip (the render path can't rely on
    # a CRC failure here — the reference-integrity check is what must catch it).
    assert zipfile.ZipFile(io.BytesIO(corrupt)).testzip() is None

    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "dangling.pptx"
        p.write_bytes(corrupt)
        problems = validate_pptx_renders(str(p))
        assert problems, "a slide referencing a missing part produced an empty problem list"
        assert any("missing part" in msg for msg in problems), problems
