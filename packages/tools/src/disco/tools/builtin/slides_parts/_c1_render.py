"""The C1-deck-render path: renders an already-built C1 ``Deck`` (produced by
the structured C2 pipeline) to HTML / native PPTX / PDF via the C3 renderer
(``_pptx_render.py``), and assembles the ``ToolOutcome`` for each format.

Extracted from ``SlidesTool._render_c1_deck`` to shrink both that method (it
was over the 100-logical-line callable cap) and the ``SlidesTool`` class span
(it was over the 350-logical-line class cap) — this was by far the largest
method on the class. ``slides.py`` keeps a thin ``_render_c1_deck`` method
that calls straight through to :func:`_render_c1_deck_impl`; the method's
public signature (and its docstring) stay on the class since
``test_slides_pipeline.py`` calls ``tool._render_c1_deck(...)`` directly as a
bound method.

The original method body was ALSO one flat per-format if/elif/elif chain
comfortably over the callable line cap on its own — moving it here unchanged
would just relocate that same violation, so it is further split one function
per output format (html / pptx / pdf) plus a small shared image-metadata
helper, with :func:`_render_c1_deck_impl` reduced to plain setup + dispatch.

None of this touches ``self`` (the original method never did either), so
these are plain module-level functions, not bound methods taking ``self``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...anatomy import ToolContext, ToolOutcome
    from ..slides import SlidesGenerateArgs


def _render_c1_image_meta(image_stats) -> tuple[str, dict]:
    """Compute the shared image note (for CONTENT) + structured image fields (for
    `structured.images`) from an ImageGenStats, or empty defaults when images were
    never attempted (`image_stats is None`)."""
    img_note = image_stats.note() if image_stats is not None else ""
    img_structured: dict = (
        {
            "images": {
                "configured": image_stats.configured,
                "wanted": image_stats.wanted,
                "generated": image_stats.generated,
                "failed": len(image_stats.failed),
                "sample_error": image_stats.sample_error,
            }
        }
        if image_stats is not None
        else {}
    )
    return img_note, img_structured


async def _render_c1_html(
    deck, args: SlidesGenerateArgs, sbx, out_filename: str, img_note: str, img_structured: dict,
    editable: dict,
) -> ToolOutcome:
    from disco.tools.builtin._pptx_render import render_html

    from ...anatomy import ToolOutcome

    html_str = render_html(deck)
    await sbx.write_file(out_filename, html_str.encode("utf-8"))
    return ToolOutcome(
        success=True,
        content=(
            f"Slide deck '{args.filename}' written to {out_filename}\n"
            f"Format: HTML (C3 brand renderer)\n"
            f"Slides: {len(deck.slides)}{img_note}"
        ),
        artifacts=[out_filename],
        structured={
            "filename": out_filename,
            "base_name": args.filename,
            "format": "html",
            "slide_count": len(deck.slides),
            "renderer": "c3-brand",
            **img_structured,
            # A2.0/A2.2: editable_source (the AuthoredDeck sidecar) is present
            # ONLY when the sidecar write actually succeeded — its presence is
            # what gates the in-app deck editor tab.
            **editable,
            "slides": [{"type": s.type, "layout": s.layout} for s in deck.slides],
        },
    )


async def _render_c1_pptx(
    deck,
    args: SlidesGenerateArgs,
    ctx: ToolContext,
    sbx,
    out_filename: str,
    img_note: str,
    img_structured: dict,
    editable: dict,
) -> ToolOutcome:
    from disco.tools.builtin._pptx_render import convert_to_pdf, render_html, render_pptx

    from ...anatomy import ToolOutcome

    pptx_bytes = render_pptx(deck)
    await sbx.write_file(out_filename, pptx_bytes)

    # Also write brand HTML alongside
    html_name = f"{args.filename}.html"
    try:
        html_str = render_html(deck)
        await sbx.write_file(html_name, html_str.encode("utf-8"))
        artifacts = [out_filename, html_name]
    except Exception:
        artifacts = [out_filename]

    # Attempt PDF via LibreOffice
    pdf_note = ""
    pdf_ok, pdf_err = await convert_to_pdf(ctx, out_filename)
    if pdf_ok:
        pdf_name = f"{args.filename}.pdf"
        artifacts.append(pdf_name)
    else:
        pdf_note = f"\nPDF: {pdf_err}"

    return ToolOutcome(
        success=True,
        content=(
            f"Slide deck '{args.filename}' written to {out_filename}\n"
            f"Format: PPTX (C3 native editable — real text boxes)\n"
            f"Slides: {len(deck.slides)}{img_note}{pdf_note}"
        ),
        artifacts=artifacts,
        structured={
            "filename": out_filename,
            "base_name": args.filename,
            "format": "pptx",
            "slide_count": len(deck.slides),
            "renderer": "pptx-native",
            **img_structured,
            # A2.0/A2.2: present only when the sidecar write actually succeeded.
            **editable,
            "slides": [{"type": s.type, "layout": s.layout} for s in deck.slides],
        },
    )


async def _render_c1_pdf(
    deck,
    args: SlidesGenerateArgs,
    ctx: ToolContext,
    sbx,
    out_filename: str,
    img_note: str,
    img_structured: dict,
    editable: dict,
) -> ToolOutcome:
    from disco.tools.builtin._pptx_render import convert_to_pdf, render_pptx

    from ...anatomy import ToolOutcome

    # Render PPTX first, then convert
    pptx_bytes = render_pptx(deck)
    pptx_name = f"{args.filename}.pptx"
    await sbx.write_file(pptx_name, pptx_bytes)
    pdf_ok, pdf_err = await convert_to_pdf(ctx, pptx_name)
    if not pdf_ok:
        return ToolOutcome(
            success=False,
            content=f"PDF conversion failed: {pdf_err}",
            error=pdf_err,
        )
    return ToolOutcome(
        success=True,
        content=(
            f"Slide deck '{args.filename}' written to {out_filename}\n"
            f"Format: PDF (via LibreOffice + C3 PPTX)\n"
            f"Slides: {len(deck.slides)}{img_note}"
        ),
        artifacts=[out_filename, pptx_name],
        structured={
            "filename": out_filename,
            "base_name": args.filename,
            "format": "pdf",
            "slide_count": len(deck.slides),
            "renderer": "libreoffice",
            **img_structured,
            # A2: a fresh sidecar makes even a pdf-format deck editable (the
            # editor re-renders html/pptx; the pdf is flagged stale on save).
            **editable,
        },
    )


async def _render_c1_deck_impl(
    deck,
    args: SlidesGenerateArgs,
    ctx: ToolContext,
    fmt: str,
    *,
    editable_source: str | None = None,
    image_stats=None,
) -> ToolOutcome:
    """Render a C1 Deck to the sandbox and return a ToolOutcome.

    ``editable_source`` is the ``{name}.authored.json`` path IFF this generation
    freshly wrote it (from generate_deck). It gates the in-app editor: a swallowed
    sidecar-write failure → None → no "Edit Slides" affordance, and never an
    affordance pointing at a stale leftover sidecar (no false affordance).
    ``image_stats`` (ImageGenStats) carries the honest image outcome — its note
    goes in the CONTENT (so the agent knows images failed / were unconfigured)
    and its numbers in `structured.images`."""
    from ...anatomy import ToolOutcome

    if ctx.sandbox is None:
        return ToolOutcome(
            success=False,
            content="No sandbox available to write the slide deck.",
        )
    sbx = ctx.sandbox
    out_filename = f"{args.filename}.{fmt}"
    editable: dict[str, str] = {"editable_source": editable_source} if editable_source else {}
    img_note, img_structured = _render_c1_image_meta(image_stats)

    if fmt == "html":
        return await _render_c1_html(
            deck, args, sbx, out_filename, img_note, img_structured, editable
        )
    if fmt == "pptx":
        return await _render_c1_pptx(
            deck, args, ctx, sbx, out_filename, img_note, img_structured, editable
        )
    if fmt == "pdf":
        return await _render_c1_pdf(
            deck, args, ctx, sbx, out_filename, img_note, img_structured, editable
        )

    return ToolOutcome(
        success=False,
        content=f"Unsupported format: {fmt!r}",
        error=f"Unsupported format: {fmt!r}",
    )
