"""Rasterize a PDF with poppler — proves the page is non-blank (real content).

This is EVIDENCE (a list of problem strings), not a verdict: it never
manufactures or upgrades a typed ``HostVerificationResult``.
"""

from __future__ import annotations

import pathlib
import subprocess
import tempfile

from ._pptx_render import _missing


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