"""C3 — Native editable PPTX renderer.

Renders a Deck (C1 Layer-2 precise representation) to:
  - .pptx  via python-pptx (real text boxes, brand fonts/colors — NOT image-per-slide)
  - 16:9 brand HTML (self-contained, OFL font-face via file:// abs URLs)
  - PDF   via LibreOffice headless inside the sandbox (gated on soffice present;
          graceful failure when absent — no false affordance)

C1 integration: ``render_pptx(deck: Deck)`` and ``render_html(deck: Deck)``
now consume the full C1 Layer-2 Deck from ``_deck_schema.py``.  Each slide's
``elements`` list is iterated and each Element is mapped to a python-pptx shape
by ``_render_element``.

Backward compat: ``MinimalDeck`` / ``DeckSlide`` remain exported and the old
``render_pptx`` / ``render_html`` signatures still accept them.  Internally
they convert via ``_minimal_to_authored`` → ``lower_deck`` → the C1 path.
Existing tests continue to pass without modification.

Layering: disco.tools → disco.core (legal downward import).
          disco.tools ≠→ disco.agent_server (upward import — illegal, not done here).

This module is the public compatibility/export facade over
:mod:`disco.tools.builtin._pptx_render_parts`, which holds the cohesive
private implementation (python-pptx primitives, the MinimalDeck/C1
native-pptx layout + archetype renderers, the C1 Element renderer, the
HTML/preview-rendering side, and PDF conversion) so every public symbol
keeps its import path (``disco.tools.builtin._pptx_render.render_pptx``
and friends). Every other private helper lives in ``_pptx_render_parts``
and is re-imported below unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# C1 imports (same package — legal same-layer import)
from disco.tools.builtin._deck_schema import (
    AuthoredDeck,
    AuthoredSlide,
    ChartSpec,
    Deck,
    TableSpec,
    lower_deck,
)

# --- compatibility re-exports (PKG-10-MEDIA) --------------------------------
# Names this module exposed BEFORE the parts split. Consumers and the test
# suite reach several of them through this module object (including via
# monkeypatch), so the facade must keep exposing every one. Restored
# programmatically by diffing this module's top-level names against its
# parent commit; PKG-13-FACADES owns their eventual deletion.
#
# Two parent-era top-level names are deliberately NOT restored here, as a
# recorded root override of the facade-surface differential (Epic 10-A):
# `ToolContext` was `TYPE_CHECKING`-only at e51b9deb and so was never a
# runtime attribute of this module (proven by a runtime `dir()` capture), and
# `importlib` was an incidental binding from `import importlib.resources`
# whose only use moved to `_pptx_render_parts/_pdf_conversion.py`. Neither has
# any consumer in the tree, this module has no `__all__` and is never
# star-imported, and re-adding the now-unused `importlib` import would require
# a per-line lint suppression the engineering rules forbid.
# ---------------------------------------------------------------------------
# Every name below is implemented in `_pptx_render_parts` and re-imported
# here so this module keeps re-exporting the exact same surface it always has
# — both for external importers (render_pptx, render_html, convert_to_pdf,
# render_deck, DeckSlide, MinimalDeck, strip_element_ids) and for the code
# below that still calls a moved helper by its bare name (an unqualified name
# resolves via the CALLING function's own module globals, so render_pptx /
# render_html below keep working because _render_pptx_c1 / _render_html_c1
# are bound into THIS module's namespace by the imports below).
# ---------------------------------------------------------------------------
from ._pptx_render_parts._geometry import (
    _BRAND_DOT as _BRAND_DOT,
)
from ._pptx_render_parts._geometry import (
    _CH as _CH,
)
from ._pptx_render_parts._geometry import (
    _CW as _CW,
)
from ._pptx_render_parts._geometry import (
    _MARGIN as _MARGIN,
)
from ._pptx_render_parts._geometry import (
    _PIPELINE_GENERATOR_MARKER as _PIPELINE_GENERATOR_MARKER,
)
from ._pptx_render_parts._geometry import (
    _SLIDE_H as _SLIDE_H,
)
from ._pptx_render_parts._geometry import (
    _SLIDE_W as _SLIDE_W,
)
from ._pptx_render_parts._geometry import (
    _TITLE_H as _TITLE_H,
)
from ._pptx_render_parts._html_archetypes import (
    BodyEidFn as BodyEidFn,
)
from ._pptx_render_parts._html_archetypes import (
    Callable as Callable,
)
from ._pptx_render_parts._html_archetypes import (
    Element as Element,
)
from ._pptx_render_parts._html_archetypes import (
    Slide as Slide,
)
from ._pptx_render_parts._html_archetypes import (
    Theme as Theme,
)
from ._pptx_render_parts._html_archetypes import (
    _art_fallback_svg as _art_fallback_svg,
)
from ._pptx_render_parts._html_archetypes import (
    _html_big_number as _html_big_number,
)
from ._pptx_render_parts._html_archetypes import (
    _html_body_text as _html_body_text,
)
from ._pptx_render_parts._html_archetypes import (
    _html_default_bullets_layout as _html_default_bullets_layout,
)
from ._pptx_render_parts._html_archetypes import (
    _html_for_archetype as _html_for_archetype,
)
from ._pptx_render_parts._html_archetypes import (
    _html_for_c1_slide as _html_for_c1_slide,
)
from ._pptx_render_parts._html_archetypes import (
    _html_image_layout as _html_image_layout,
)
from ._pptx_render_parts._html_archetypes import (
    _html_quote as _html_quote,
)
from ._pptx_render_parts._html_archetypes import (
    _html_section_header_layout as _html_section_header_layout,
)
from ._pptx_render_parts._html_archetypes import (
    _html_timeline as _html_timeline,
)
from ._pptx_render_parts._html_archetypes import (
    _html_title_style_layout as _html_title_style_layout,
)
from ._pptx_render_parts._html_archetypes import (
    _html_two_by_two as _html_two_by_two,
)
from ._pptx_render_parts._html_archetypes import (
    _html_two_column_layout as _html_two_column_layout,
)
from ._pptx_render_parts._html_archetypes import (
    _img_src as _img_src,
)
from ._pptx_render_parts._html_archetypes import (
    html as html,
)
from ._pptx_render_parts._html_shell import (
    _STRIP_ELEMENT_ID_RE as _STRIP_ELEMENT_ID_RE,
)
from ._pptx_render_parts._html_shell import (
    _STRIP_SLIDE_ID_RE as _STRIP_SLIDE_ID_RE,
)
from ._pptx_render_parts._html_shell import (
    _TAG_RE as _TAG_RE,
)
from ._pptx_render_parts._html_shell import (
    TYPE_CHECKING as TYPE_CHECKING,
)
from ._pptx_render_parts._html_shell import (
    _brand_marks_html as _brand_marks_html,
)
from ._pptx_render_parts._html_shell import (
    _bullets_html as _bullets_html,
)
from ._pptx_render_parts._html_shell import (
    _html_for_slide as _html_for_slide,
)
from ._pptx_render_parts._html_shell import (
    _render_html_c1 as _render_html_c1,
)
from ._pptx_render_parts._html_shell import (
    _render_html_document as _render_html_document,
)
from ._pptx_render_parts._html_shell import (
    re as re,
)
from ._pptx_render_parts._html_shell import (
    strip_element_ids as strip_element_ids,
)
from ._pptx_render_parts._pdf_conversion import convert_to_pdf as convert_to_pdf
from ._pptx_render_parts._pdf_conversion import (
    shlex as shlex,
)
from ._pptx_render_parts._pptx_archetypes import (
    _render_archetype_pptx as _render_archetype_pptx,
)
from ._pptx_render_parts._pptx_archetypes import (
    _render_big_number_pptx as _render_big_number_pptx,
)
from ._pptx_render_parts._pptx_archetypes import (
    _render_quote_pptx as _render_quote_pptx,
)
from ._pptx_render_parts._pptx_archetypes import (
    _render_timeline_pptx as _render_timeline_pptx,
)
from ._pptx_render_parts._pptx_archetypes import (
    _render_two_by_two_pptx as _render_two_by_two_pptx,
)
from ._pptx_render_parts._pptx_assembly import _render_pptx_c1 as _render_pptx_c1
from ._pptx_render_parts._pptx_assembly import (
    io as io,
)
from ._pptx_render_parts._pptx_c1_elements import (
    _is_bullet_el as _is_bullet_el,
)
from ._pptx_render_parts._pptx_c1_elements import (
    _render_accent_bar_element as _render_accent_bar_element,
)
from ._pptx_render_parts._pptx_c1_elements import (
    _render_bullet_group as _render_bullet_group,
)
from ._pptx_render_parts._pptx_c1_elements import (
    _render_element as _render_element,
)
from ._pptx_render_parts._pptx_c1_elements import (
    _render_image_element as _render_image_element,
)
from ._pptx_render_parts._pptx_c1_elements import (
    _render_rect_element as _render_rect_element,
)
from ._pptx_render_parts._pptx_c1_elements import (
    _render_slide_elements as _render_slide_elements,
)
from ._pptx_render_parts._pptx_c1_elements import (
    _render_text_element as _render_text_element,
)
from ._pptx_render_parts._pptx_minimal_layouts import (
    _MINIMAL_LAYOUT_FNS as _MINIMAL_LAYOUT_FNS,
)
from ._pptx_render_parts._pptx_minimal_layouts import (
    _layout_bullets_slide as _layout_bullets_slide,
)
from ._pptx_render_parts._pptx_minimal_layouts import (
    _layout_image_right_slide as _layout_image_right_slide,
)
from ._pptx_render_parts._pptx_minimal_layouts import (
    _layout_section_slide as _layout_section_slide,
)
from ._pptx_render_parts._pptx_minimal_layouts import (
    _layout_title_slide as _layout_title_slide,
)
from ._pptx_render_parts._pptx_primitives import (
    Path as Path,
)
from ._pptx_render_parts._pptx_primitives import (
    _add_accent_bar as _add_accent_bar,
)
from ._pptx_render_parts._pptx_primitives import (
    _add_pptx_text as _add_pptx_text,
)
from ._pptx_render_parts._pptx_primitives import (
    _add_textbox as _add_textbox,
)
from ._pptx_render_parts._pptx_primitives import (
    _draw_brand_marks as _draw_brand_marks,
)
from ._pptx_render_parts._pptx_primitives import (
    _draw_cover_colophon as _draw_cover_colophon,
)
from ._pptx_render_parts._pptx_primitives import (
    _draw_wordmark as _draw_wordmark,
)
from ._pptx_render_parts._pptx_primitives import (
    _first_font as _first_font,
)
from ._pptx_render_parts._pptx_primitives import (
    _font_dir as _font_dir,
)
from ._pptx_render_parts._pptx_primitives import (
    _image_placeholder as _image_placeholder,
)
from ._pptx_render_parts._pptx_primitives import (
    _rgb_from_hex as _rgb_from_hex,
)
from ._pptx_render_parts._pptx_primitives import (
    _set_run_style as _set_run_style,
)
from ._pptx_render_parts._pptx_primitives import (
    _set_slide_bg as _set_slide_bg,
)
from ._pptx_render_parts._slide_archetype_shared import (
    _body_strings as _body_strings,
)
from ._pptx_render_parts._slide_archetype_shared import (
    _plain_element_text as _plain_element_text,
)
from ._pptx_render_parts._slide_archetype_shared import (
    _slide_archetype as _slide_archetype,
)
from ._pptx_render_parts._slide_archetype_shared import (
    _split_timeline_label as _split_timeline_label,
)
from ._pptx_render_parts._slide_archetype_shared import (
    _title_element as _title_element,
)

# ---------------------------------------------------------------------------
# MinimalDeck / DeckSlide — backward compat shim (C1 Deck is the real thing)
# ---------------------------------------------------------------------------
# These types are kept so that test_pptx_render.py (and any external code that
# imports them) continues to work.  Internally, render_pptx/render_html convert
# MinimalDeck → AuthoredDeck → Deck via the C1 lowerer.

LayoutHint = Literal["title", "bullets", "section", "image_right", "chart", "table"]


@dataclass
class DeckSlide:
    """Compatibility shim — maps to an AuthoredSlide on the C1 path."""

    title: str
    bullets: list[str] = field(default_factory=list)
    layout: LayoutHint = "bullets"
    image_url: str | None = None
    notes: str | None = None
    chart: ChartSpec | None = None  # C8: chart data → routes to c8 chart layout
    table: TableSpec | None = None


@dataclass
class MinimalDeck:
    """Compatibility shim — converts to AuthoredDeck on the C1 path.

    theme_name + theme_mode resolve via core.brand.resolve_theme(name, mode).
    """

    title: str
    slides: list[DeckSlide] = field(default_factory=list)
    theme_name: str = "disco"
    theme_mode: str = "light"


def _minimal_to_authored(deck: MinimalDeck) -> AuthoredDeck:
    """Convert a MinimalDeck (compat shim) to an AuthoredDeck for the C1 path."""
    _LAYOUT_MAP = {
        "title": "title",
        "bullets": "bullets",
        "section": "section_header",
        "image_right": "image_right",
        "chart": "metrics",  # C8: chart → metrics layout
        "table": "table",  # C8: table → table layout
    }
    slides = []
    for ds in deck.slides:
        slides.append(
            AuthoredSlide(
                type=_LAYOUT_MAP.get(ds.layout, "bullets"),
                title=ds.title,
                body=ds.bullets,
                image_prompt=None,  # image_url is a path, not a prompt
                notes=ds.notes,
                chart=ds.chart,  # C8: propagate chart spec
                table=ds.table,  # C8: propagate table spec
            )
        )
    theme_str = f"{deck.theme_name}-{deck.theme_mode}"
    if theme_str not in ("disco-light", "disco-dark", "neutral-light"):
        theme_str = "disco-light"
    # Remap neutral-light → neutral
    if theme_str == "neutral-light":
        theme_str = "neutral"
    return AuthoredDeck(
        title=deck.title,
        theme=theme_str,  # type: ignore[arg-type]
        slides=slides,
    )


# ---------------------------------------------------------------------------
# render_pptx — accepts Deck (C1) or MinimalDeck (compat)
# ---------------------------------------------------------------------------


def render_pptx(deck: Deck | MinimalDeck) -> bytes:
    """Render *deck* to native editable .pptx bytes (python-pptx, real text boxes).

    Accepts either a C1 ``Deck`` (from ``_deck_schema.lower_deck``) or a
    ``MinimalDeck`` (backward-compat shim).  Returns raw bytes suitable for
    ``sandbox.write_file(name, bytes)``.  NEVER call ``.encode()`` on the
    result — it is already binary.
    """

    if isinstance(deck, MinimalDeck):
        # Convert via the C1 path
        authored = _minimal_to_authored(deck)
        c1_deck = lower_deck(authored)
        return _render_pptx_c1(c1_deck)
    # Already a C1 Deck
    return _render_pptx_c1(deck)


# ---------------------------------------------------------------------------
# render_html — 16:9 brand HTML (accepts Deck or MinimalDeck)
# ---------------------------------------------------------------------------


def render_html(deck: Deck | MinimalDeck) -> str:  # noqa: C901
    """Render *deck* to a self-contained 16:9 brand HTML string.

    Accepts a C1 ``Deck`` or a ``MinimalDeck`` (backward-compat shim).
    One ``<section class="slide">`` per slide.  Keyboard navigation (←/→).
    """
    if isinstance(deck, MinimalDeck):
        authored = _minimal_to_authored(deck)
        c1_deck = lower_deck(authored)
        return _render_html_c1(c1_deck)
    return _render_html_c1(deck)


# ---------------------------------------------------------------------------
# render_deck — convenience orchestrator
# ---------------------------------------------------------------------------


def render_deck(deck: Deck | MinimalDeck) -> dict[str, bytes | str]:
    """Render *deck* to pptx_bytes + html_str.

    Accepts a C1 ``Deck`` or ``MinimalDeck`` (backward-compat shim).
    PDF requires a sandbox context; call ``convert_to_pdf(ctx, pptx_name)``
    separately after writing the .pptx to the workspace.

    Returns:
        ``{"pptx_bytes": bytes, "html_str": str}``
    """
    return {
        "pptx_bytes": render_pptx(deck),
        "html_str": render_html(deck),
    }
