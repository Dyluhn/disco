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
import posixpath
import shutil
import subprocess
import tempfile
import uuid
import xml.etree.ElementTree as ET
import zipfile

# The OPC package-relationships namespace (ECMA-376 Part 2).
_OPC_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"


def _missing(tool: str) -> bool:
    return shutil.which(tool) is None


def _opc_integrity_problems(path: str) -> list[str]:
    """Deterministic OOXML/OPC structural pre-check (C9-01 defect-1 correction): a
    ``.pptx`` must be a valid ZIP whose internal relationship targets all resolve to
    parts that actually exist in the package.

    This catches two corruption classes the RENDER step alone cannot, because
    LibreOffice silently RECOVERS them: ZIP-invalid bytes, and a valid ZIP whose slide
    (or any part) references a missing part via a ``_rels/*.rels`` entry. It is a
    bounded relationship-integrity walk over the package's own manifest — NOT a general
    OOXML parser and NOT a render. External relationships (``TargetMode="External"``)
    are skipped; every internal target is resolved (POSIX, ``..``-aware, root-relative
    when absolute) and required to be present in the zip namelist."""
    try:
        with zipfile.ZipFile(path) as zf:
            if zf.testzip() is not None:
                return ["pptx is a corrupt zip archive (a CRC check failed)"]
            names = set(zf.namelist())
            problems: list[str] = []
            for rels_name in sorted(n for n in names if n.endswith(".rels") and "_rels/" in n):
                base = rels_name.rsplit("_rels/", 1)[0].rstrip("/")
                try:
                    root = ET.fromstring(zf.read(rels_name))
                except ET.ParseError:
                    problems.append(f"pptx relationship part {rels_name} is not valid XML")
                    continue
                for rel in root.iter(_OPC_REL_NS):
                    if (rel.get("TargetMode") or "Internal") == "External":
                        continue
                    target = rel.get("Target") or ""
                    resolved = (
                        target.lstrip("/")
                        if target.startswith("/")
                        else posixpath.normpath(posixpath.join(base, target))
                    )
                    if resolved not in names:
                        problems.append(
                            f"pptx references a missing part: {rels_name} -> {target} "
                            f"(resolved {resolved})"
                        )
            return problems
    except zipfile.BadZipFile:
        return ["pptx is not a valid zip archive (not an OOXML package)"]
    except OSError as exc:
        return [f"pptx could not be read: {exc}"]


def validate_pptx_renders(path: str, *, workdir: str | None = None) -> list[str]:
    """Render a ``.pptx`` to PDF with headless LibreOffice — proves the deck actually opens
    and lays out AS A PRESENTATION, then reuses the cheap PDF checks on the result.

    Hardened for the C9-01 findings (2026-07-17):

    * A deterministic OPC relationship-integrity pre-check (``_opc_integrity_problems``)
      runs FIRST: ZIP-invalid bytes and a valid ZIP whose parts reference a missing part
      are rejected up front, because LibreOffice silently RECOVERS both and renders them
      (independent verification demonstrated a slide pointing at a deleted slideLayout
      rendering clean).
    * The IMPORT filter is pinned to Impress's pptx filter and the EXPORT filter to
      ``impress_pdf_Export`` — corrupt bytes can no longer succeed through LibreOffice's
      generic import fallback (which happily renders garbage as a text document).
    * Every invocation gets an ISOLATED LibreOffice user profile
      (``-env:UserInstallation``), so a concurrently running soffice instance can never
      absorb the request and return without producing our output.
    * The output directory is FRESH and unique per invocation, and the expected PDF must
      exist there afterwards. LibreOffice exits 0 on "source file could not be loaded",
      so the exit code alone is NEVER trusted as success.
    """
    if _missing("soffice"):
        return ["soffice unavailable — run this heavy validator on the VM 201 evidence host"]
    structural = _opc_integrity_problems(path)
    if structural:
        return structural
    # `workdir` (documented optional) may be relative; resolve it before building the
    # profile file URI, which requires an absolute path (C9-01 defect-2 correction).
    work = (
        pathlib.Path(workdir).resolve()
        if workdir
        else pathlib.Path(tempfile.mkdtemp(prefix="deckrender-"))
    )
    invocation = work / f"render-{uuid.uuid4().hex}"
    outdir = invocation / "out"
    profile = invocation / "profile"
    outdir.mkdir(parents=True)
    profile.mkdir(parents=True)
    proc = subprocess.run(
        [
            "soffice",
            "--headless",
            f"-env:UserInstallation={profile.as_uri()}",
            "--infilter=Impress MS PowerPoint 2007 XML",
            "--convert-to",
            "pdf:impress_pdf_Export",
            "--outdir",
            str(outdir),
            path,
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if proc.returncode != 0:
        return [f"soffice convert failed: {proc.stderr.strip()[:200]}"]
    pdf = outdir / (pathlib.Path(path).stem + ".pdf")
    if not pdf.exists() or pdf.stat().st_size == 0:
        detail = (proc.stderr.strip() or proc.stdout.strip())[:200]
        return [
            "pptx did not render to a pdf (deck does not open as a presentation"
            + (f": {detail}" if detail else "")
            + ")"
        ]
    from disco.tools.verify.artifact_validators import validate_pdf

    return validate_pdf(str(pdf))


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
