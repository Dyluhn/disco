"""Export-render fact stamping: reads a produced deck artifact back from the
sandbox and derives ``ExportRenderFacts`` for the tool result's structured
payload. Best-effort: on any read/parse failure the payload is left unstamped
(the finish gate then falls through — absence is honest, never a fabricated
verdict).

Extracted from ``SlidesTool._stamp_export_render`` to bring its cyclomatic
complexity under the callable cap: the PDF/LibreOffice sibling-pptx lookup was
a deeply nested branch inside the original method, pulled out here as its own
small, independently-scored helper (:func:`_pdf_sibling_source_text`) rather
than merely relocated unchanged. ``slides.py`` keeps a thin
``_stamp_export_render`` method that calls straight through to
:func:`_stamp_export_render_impl`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from disco.core.contract.export_render import EXPORT_RENDER_KEY, check_export_render

if TYPE_CHECKING:
    from ...anatomy import ToolContext, ToolOutcome


async def _pdf_sibling_source_text(ctx: ToolContext, s: dict, fmt: str) -> str | None:
    """For a PDF produced via LibreOffice from a FRESH sibling .pptx written this
    same run (renderer=="libreoffice"), read that pptx back and use its own
    chrome/placeholder/media-aware verdict as the PDF's source_text hint: ""
    forces the PDF content check to refuse a blank deck, while None lets the byte
    floor pass a real deck (incl. a media-only visual deck whose text is empty).
    Gating on renderer=="libreoffice" means a stale leftover .pptx from an
    earlier run can never judge the current PDF."""
    if not (fmt == "pdf" and str(s.get("renderer") or "") == "libreoffice"):
        return None
    base_name = s.get("base_name")
    if not (isinstance(base_name, str) and base_name):
        return None
    sandbox = ctx.sandbox
    if sandbox is None:  # slides tool declares runs_in="sandbox"; guard kept from the parent
        return None
    try:
        pptx_data = await sandbox.read_file(f"{base_name}.pptx")
        if isinstance(pptx_data, str):
            pptx_data = pptx_data.encode("utf-8")
        if pptx_data.startswith(b"PK"):
            pf = check_export_render("pptx", pptx_data)
            return None if pf.non_blank else ""
    except Exception:
        return None
    return None


async def _stamp_export_render_impl(outcome: ToolOutcome, ctx: ToolContext) -> ToolOutcome:
    """Read the produced deck file back from the sandbox and add ExportRenderFacts
    to the tool result's structured payload. Best-effort: on any read/parse
    failure the payload is left unstamped (the gate then falls through — absence
    is honest, never a fabricated verdict)."""
    s = outcome.structured or {}
    filename = s.get("filename")
    fmt = str(s.get("format") or "")
    declared = s.get("slide_count") if isinstance(s.get("slide_count"), int) else None
    # The C1/native renderers set slide_count = len(deck.slides) (EXACT); the
    # markdown/Marp paths derive it from a separator split (HEURISTIC, can be
    # off by one) — so truncation is strict for the former, tolerant for the latter.
    declared_exact = str(s.get("renderer") or "") not in ("marp", "fallback")
    if not isinstance(filename, str) or not filename or ctx.sandbox is None:
        return outcome
    try:
        data = await ctx.sandbox.read_file(filename)
    except Exception:
        return outcome
    if isinstance(data, str):
        data = data.encode("utf-8")
    source_text = await _pdf_sibling_source_text(ctx, s, fmt)
    facts = check_export_render(
        fmt,
        data,
        text=source_text,
        declared_units=declared,
        declared_exact=declared_exact,
    )
    return outcome.model_copy(
        update={"structured": {**s, EXPORT_RENDER_KEY: facts.model_dump(mode="json")}}
    )
