"""PDF conversion — LibreOffice headless (sandbox-jailed).

Moved verbatim out of ``_pptx_render.py``.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from disco.tools.anatomy import ToolContext


async def convert_to_pdf(ctx: ToolContext, pptx_name: str) -> tuple[bool, str]:
    """Convert *pptx_name* (workspace-relative) to PDF via ``soffice`` inside the sandbox.

    Returns ``(ok, error_message)``.  When ``soffice`` is absent the return is a
    CLEAR failure — never a silent empty PDF (no false affordance).

    The PDF output lands in the sandbox workspace alongside the .pptx, named
    ``<pptx_name>.pdf`` (LibreOffice default).  The caller surfaces it as an
    additional artifact.

    Requires: ctx.sandbox is not None (tool must declare runs_in="sandbox").
    """
    assert ctx.sandbox is not None, "convert_to_pdf requires a sandbox context"

    # 1. Probe for soffice
    try:
        probe = await ctx.sandbox.exec_shell("command -v soffice", timeout_s=5)
    except Exception as exc:
        return False, (
            f"soffice probe failed ({exc}) — LibreOffice is not installed in the "
            "sandbox image; PDF conversion unavailable. "
            "Install via: apt-get install libreoffice-impress"
        )
    if probe.exit_code != 0:
        return False, (
            "soffice not found in the sandbox — LibreOffice is not installed. "
            "PDF conversion unavailable. Add libreoffice-impress to "
            "current/deploy/sandbox/Dockerfile to enable it."
        )

    # 2. Convert (--outdir . → PDF lands in cwd = workspace)
    cmd = "soffice --headless --convert-to pdf --outdir . " + shlex.quote(pptx_name)
    try:
        res = await ctx.sandbox.exec_shell(cmd, timeout_s=120)
    except Exception as exc:
        return False, f"soffice conversion error: {exc}"

    if res.timed_out:
        return False, "soffice conversion timed out after 120 s"
    if res.exit_code != 0:
        err = (res.stderr or "").strip() or f"soffice exited {res.exit_code}"
        return False, f"soffice conversion failed: {err}"

    return True, ""
