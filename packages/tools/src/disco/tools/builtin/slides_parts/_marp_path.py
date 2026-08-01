"""The Marp-CLI-in-sandbox helpers: presence detection + running the render.

The deck markdown is agent-generated (injection-tainted). Marp drives a full
Chromium to render PDF/PPTX; running that on the host would let a crafted deck
(file:// refs, local URLs) exfiltrate host files during render. So marp runs in
the jailed sandbox via exec_shell — the binary + Chromium ship in the image
(deploy/sandbox/Dockerfile, RP-10 layer). The output lands directly in the
workspace, so there is no host temp file and no read-back.

Extracted from ``slides.py`` to reduce module complexity; the public facade
re-imports both names unchanged. ``SlidesTool._run_marp_path`` /
``_render_with_marp`` (which call these) stay physically defined in
``slides.py`` itself and reference these names unqualified, so a test that
monkeypatches ``disco.tools.builtin.slides._marp_available`` keeps working —
the patch rebinds the name in the FACADE module's namespace, which is exactly
where those callers resolve it from at call time.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...anatomy import ToolContext


async def _marp_available(ctx: ToolContext) -> bool:
    """True if the marp CLI is present INSIDE the sandbox."""
    assert ctx.sandbox is not None  # slides tool declares runs_in="sandbox"
    try:
        res = await ctx.sandbox.exec_shell("command -v marp", timeout_s=10)
    except Exception:
        return False
    return res.exit_code == 0


async def _marp_render_in_sandbox(
    ctx: ToolContext,
    src_name: str,
    out_name: str,
    fmt: str,
    *,
    timeout_s: int = 180,
) -> tuple[bool, str]:
    """Run `marp <src> -o <out>` INSIDE the sandbox (workdir = workspace). Both
    paths are workspace-relative names. Returns (ok, error_message)."""
    assert ctx.sandbox is not None  # slides tool declares runs_in="sandbox"
    pptx = "--pptx " if fmt == "pptx" else ""
    cmd = f"marp {pptx}{shlex.quote(src_name)} -o {shlex.quote(out_name)}"
    res = await ctx.sandbox.exec_shell(cmd, timeout_s=timeout_s)
    if res.timed_out:
        return False, f"marp render timed out after {timeout_s}s"
    if res.exit_code != 0:
        return False, (res.stderr or "").strip() or f"marp exited with code {res.exit_code}"
    return True, ""
