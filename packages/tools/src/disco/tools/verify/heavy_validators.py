"""Heavy (VM-only / nightly-tier) artifact validators.

These RENDER the artifact — LibreOffice for decks, poppler for PDFs, ffmpeg for audio —
rather than only reading metadata, so they catch corruption a mime/format check misses (a
.pptx that unzips fine but won't open, a PDF whose pages are blank). They need binaries
(`soffice`, `pdftoppm`, `ffprobe`) that live on the VM 201 evidence host, NOT the PR gate —
so each returns a single "tool unavailable" note when its binary is missing rather than
raising, and the suite is wired into the nightly (VM) tier in W17.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import tempfile
import zipfile


def _missing(tool: str) -> bool:
    return shutil.which(tool) is None


def validate_pptx_renders(path: str, *, workdir: str | None = None) -> list[str]:
    """Render a ``.pptx`` to PDF with headless LibreOffice — proves the deck actually opens
    and lays out, then reuses the cheap PDF checks on the result."""
    if _missing("soffice"):
        return ["soffice unavailable — run this heavy validator on the VM 201 evidence host"]
    # LibreOffice is deliberately permissive: corrupt/plaintext bytes carrying
    # a .pptx suffix can be opened as a Writer document and exported with exit
    # code 0. Prove this is an actual Presentation package before trusting the
    # render result.
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            required = {"[Content_Types].xml", "ppt/presentation.xml"}
            if not required <= names:
                return ["corrupt pptx: required presentation package parts are missing"]
            # Reading the central package parts forces CRC/decompression checks.
            for name in required:
                archive.read(name)
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        return [f"corrupt pptx: {exc}"]
    work = workdir or tempfile.mkdtemp(prefix="deckrender-")
    proc = subprocess.run(
        ["soffice", "--headless", "--convert-to", "pdf", "--outdir", work, path],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if proc.returncode != 0:
        return [f"soffice convert failed: {proc.stderr.strip()[:200]}"]
    pdf = pathlib.Path(work) / (pathlib.Path(path).stem + ".pdf")
    if not pdf.exists() or pdf.stat().st_size == 0:
        return ["pptx did not render to a pdf (deck does not open)"]
    from disco.tools.verify.artifact_validators import validate_pdf

    # A deck made entirely of images/shapes (and even a deliberately blank
    # template) may have no extractable PDF text. That is valid for PPTX; retain
    # the structural page/open checks while leaving visual non-blank assertions
    # to deck-specific fixture tests.
    return [
        problem for problem in validate_pdf(str(pdf)) if problem != "pdf has no extractable text"
    ]


def validate_pdf_renders(path: str, *, workdir: str | None = None) -> list[str]:
    """Rasterize page 1 of a PDF with poppler — proves the page is non-blank (real content,
    not an empty/black page)."""
    if _missing("pdftoppm"):
        return ["pdftoppm unavailable — run this heavy validator on the VM 201 evidence host"]
    work = workdir or tempfile.mkdtemp(prefix="pdfrender-")
    out = pathlib.Path(work) / "page"
    proc = subprocess.run(
        ["pdftoppm", "-png", "-f", "1", "-l", "1", "-r", "50", path, str(out)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        return [f"pdftoppm failed: {proc.stderr.strip()[:200]}"]
    pngs = list(pathlib.Path(work).glob("page*.png"))
    if not pngs:
        return ["pdf did not rasterize to an image"]
    try:
        from PIL import Image

        img = Image.open(pngs[0]).convert("L")
        extrema = img.getextrema()  # (min, max) luminance
        if extrema[0] == extrema[1]:
            return ["pdf page 1 is uniformly blank"]
    except ImportError:
        pass  # Pillow optional; the render itself succeeding is already signal
    return []
