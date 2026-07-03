"""Brand mark HTML partials — the disco definition mark + wordmark.

Ported from docs/evidence/_definition.html:53-60 (definition mark structure)
and lines 90-92 (wordmark).  CSS for these elements lives in
``print_skeleton_css()`` (the ``.defmark`` block) so themes apply
automatically.

Two public functions:

  definition_mark_html(scale)  — the full Latin definition mark, at one of
                                 three scales: "masthead" / "colophon" /
                                 "footer" (maps to s-lg / s-md / s-sm).

  wordmark_html()              — just the wordmark text with the accent dot,
                                 for the page footer.
"""

from __future__ import annotations

from typing import Literal

# SINGLE SOURCE OF TRUTH shared by the PPTX renderer chrome and the export
# render-correctness checker; a drift here once let a blank branded PPTX pass
# the finish gate. Matched at WORD BOUNDARIES by the checker, so bare "disco"
# does NOT strip real words like "discovery" — the colophon fragments are kept as
# WHOLE phrases ("from discere — to learn"), never split into common bigrams like
# "to learn" that would gut real prose.
BRAND_CHROME_TEXTS: tuple[str, ...] = (
    "Disco",
    "disco",
    "Latin · verb",
    "/ˈdɪs.koː/",
    "I learn; I become acquainted with.",
    "from discere — to learn",
)

# Non-content PLACEHOLDER strings the renderers stamp for empty slots (a failed/
# absent image, an empty chart/table). Counting them as slide text let a deck of
# empty placeholders read as non-blank, so the checker subtracts them too. Verbatim
# from _pptx_render.py ("[image]") and _c8_chart_layouts.py ("(no table data)" /
# "[no data]") — a test pins these against the renderers.
RENDER_PLACEHOLDERS: tuple[str, ...] = (
    "[image]",
    "(no table data)",
    "[no data]",
)

# Scale class map — mirrors _definition.html:43-45
_SCALE_CLASS: dict[str, str] = {
    "masthead": "s-lg",
    "colophon": "s-md",
    "footer": "s-sm",
}


def definition_mark_html(
    scale: Literal["masthead", "colophon", "footer"] = "colophon",
) -> str:
    """Return the disco definition mark HTML at the requested scale.

    Ported verbatim from docs/evidence/_definition.html:53-60 structure:
      .l1  — headword "disco" (Fraunces) + POS "Latin · verb" + IPA
      .rule — hairline with the accent tick
      .gloss — italic gloss
      .root  — etymology note

    The .defmark CSS is in print_skeleton_css() — callers must include it.
    """
    cls = _SCALE_CLASS.get(scale, "s-md")
    return (
        f'<div class="defmark {cls}">'
        '<div class="l1">'
        '<span class="hw">disco</span>'
        '<span class="pos">Latin<span class="sep">&middot;</span>verb</span>'
        '<span class="ipa">/&#712;d&#618;s.ko&#720;/</span>'
        "</div>"
        '<div class="rule"><i></i></div>'
        '<div class="gloss">&ldquo;I learn; I become acquainted with.&rdquo;</div>'
        '<div class="root">from <em>discere</em> &mdash; to learn</div>'
        "</div>"
    )


def wordmark_html() -> str:
    """Return the wordmark HTML: ``Disco<span class="dot">.</span>``

    Ported from docs/evidence/_definition.html:90-92.
    The .wordmark CSS is in print_skeleton_css().
    """
    return '<span class="wordmark">Disco<span class="dot">.</span></span>'
