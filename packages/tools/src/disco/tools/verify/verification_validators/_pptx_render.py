"""Render a ``.pptx`` to PDF with headless LibreOffice — proves the deck opens.

This is EVIDENCE (a list of problem strings), not a verdict: it never
manufactures or upgrades a typed ``HostVerificationResult``.

Hardened for the C9-01 findings (2026-07-17):

* A deterministic OPC relationship-integrity pre-check (``_opc_integrity_problems``)
  runs FIRST: ZIP-invalid bytes and a valid ZIP whose parts reference a missing part
  are rejected up front, because LibreOffice silently RECOVERS both and renders them.
* Beyond OPC integrity, the package must contain the core presentation parts
  (``[Content_Types].xml``, ``ppt/presentation.xml``) — a valid zip that is not a
  presentation package is rejected before, and independently of, the renderer.
* The IMPORT filter is pinned to Impress's pptx filter and the EXPORT filter to
  ``impress_pdf_Export`` — corrupt bytes can no longer succeed through LibreOffice's
  generic import fallback.
* Every invocation gets an ISOLATED LibreOffice user profile
  (``-env:UserInstallation``), so a concurrently running soffice instance can never
  absorb the request and return without producing our output.
* The output directory is FRESH and unique per invocation, and the expected PDF must
  exist there afterwards. LibreOffice exits 0 on "source file could not be loaded",
  so the exit code alone is NEVER trusted as success.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import tempfile
import uuid
import zipfile

from ._opc_integrity import _opc_integrity_problems


def _missing(tool: str) -> bool:
    return shutil.which(tool) is None


def validate_pptx_renders(path: str, *, workdir: str | None = None) -> list[str]:
    """Render a ``.pptx`` to PDF with headless LibreOffice — proves the deck actually opens
    and lays out AS A PRESENTATION, then reuses the cheap PDF checks on the result.

    Hardened for the C9-01 findings (2026-07-17):

    * A deterministic OPC relationship-integrity pre-check (``_opc_integrity_problems``)
      runs FIRST: ZIP-invalid bytes and a valid ZIP whose parts reference a missing part
      are rejected up front, because LibreOffice silently RECOVERS both and renders them
      (independent verification demonstrated a slide pointing at a deleted slideLayout
      rendering clean).
    * Beyond OPC integrity, the package must contain the core presentation parts
      (``[Content_Types].xml``, ``ppt/presentation.xml``) — a valid zip that is not a
      presentation package is rejected before, and independently of, the renderer.
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
    # The structural OPC pre-check needs no renderer, so it runs FIRST — corruption is
    # detected identically on the PR gate and the VM host, and a corrupt package never
    # depends on LibreOffice being present to be rejected.
    structural = _opc_integrity_problems(path)
    if structural:
        return structural
    # LibreOffice is deliberately permissive: corrupt/plaintext bytes carrying a .pptx
    # suffix can be opened via its generic import fallback (e.g. as a Writer document)
    # and exported with exit code 0. Beyond zip/OPC integrity, the package must
    # actually BE a presentation — and that rejection must not depend on the renderer
    # either, so it also precedes the soffice guard (callers rightly filter the
    # unavailable-tool note, which must never hide a corrupt package). The CRC/
    # decompression proof this check once forced by reading the required parts is
    # subsumed by the bounded per-member stream validation above.
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        return [f"corrupt pptx: {exc}"]
    if not {"[Content_Types].xml", "ppt/presentation.xml"} <= names:
        return ["corrupt pptx: required presentation package parts are missing"]
    if _missing("soffice"):
        return ["soffice unavailable — run this heavy validator on the VM 201 evidence host"]
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

    # A deck made entirely of images/shapes (and even a deliberately blank
    # template) may have no extractable PDF text. That is valid for PPTX; retain
    # the structural page/open checks while leaving visual non-blank assertions
    # to deck-specific fixture tests.
    return [
        problem for problem in validate_pdf(str(pdf)) if problem != "pdf has no extractable text"
    ]