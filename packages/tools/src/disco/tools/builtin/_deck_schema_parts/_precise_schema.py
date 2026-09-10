"""Layer 2 — precise positional representation consumed by the C3 renderer
(``_pptx_render.py``) and the future browser editor.  All positions are in EMU
(English Metric Units, 1 inch = 914,400).

Extracted from ``_deck_schema.py`` to reduce module size; the public facade
re-imports these names unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from disco.core.brand.tokens import Theme

from ._authoring_schema import ChartSpec, LayoutHint, SlideArchetype, TableSpec


@dataclass
class Element:
    """One positioned element on a slide.  All sizes in EMU.

    The renderer maps each kind to a native shape:
      text      → add_textbox with styled runs
      accent_bar → add_shape (rectangle, thin)
      image     → add_picture (if image_path set) or placeholder rect
      rect      → add_shape (filled rectangle — e.g. section bg)
    """

    id: str
    kind: Literal["text", "accent_bar", "image", "rect"]
    left: float
    top: float
    width: float
    height: float

    # ---- text fields --------------------------------------------------------
    text: str = ""
    font_name: str = ""  # first font in the CSS stack
    font_size_pt: float = 20.0
    hex_color: str = "#1a1813"
    bold: bool = False
    italic: bool = False
    align: str = "LEFT"  # "LEFT" | "CENTER" | "RIGHT"
    word_wrap: bool = True

    # ---- image fields -------------------------------------------------------
    image_prompt: str | None = None  # C7 wire: generate from this prompt
    image_path: str | None = None  # file path if already generated
    # C7 wire: the generated image BYTES, carried on the element so the renderer
    # embeds them directly (BytesIO → add_picture / data-URI) without resolving a
    # path against the sandbox — works identically for local + container sandboxes.
    image_bytes: bytes | None = None

    # ---- shape fields -------------------------------------------------------
    fill_hex: str = "#2563eb"
    border_hex: str | None = None


@dataclass
class Slide:
    """One slide in the precise Deck — layout resolved, elements positioned."""

    id: str
    type: str  # from AuthoredSlide.type (e.g. "bullets_cont")
    layout: LayoutHint  # resolved, never None
    archetype: SlideArchetype | None = None
    title: str = ""  # propagated from AuthoredSlide.title (for c8 duck-typing)
    elements: list[Element] = field(default_factory=list)
    notes: str | None = None
    chart: ChartSpec | None = None  # propagated when AuthoredSlide.chart is set
    table: TableSpec | None = None  # propagated when AuthoredSlide.table is set
    # BW-13: the ORIGINAL authored-body index of each rendered body line, in render
    # order — the SAME ``body_index_map`` the editor pointer model (lower_deck_for_editor)
    # uses. ``render_html`` stamps ``data-element-id="{slide}:body:{orig}"`` from this so
    # a rendered editor slide's element ids match the pointer model 1:1, even for
    # overflow / continuation / non-contiguous column spill (where render position !=
    # authored index). Empty (default) → identity: stamp by render position (simple
    # slides, hand-built Slides, the MinimalDeck shim path).
    body_index_map: list[int] = field(default_factory=list)


@dataclass
class Deck:
    """Full precise deck consumed by the C3 renderer."""

    id: str
    title: str
    theme: Theme  # resolved via core.brand.resolve_theme
    slides: list[Slide] = field(default_factory=list)
    size: tuple[int, int] = (12_192_000, 6_858_000)  # 16:9 EMU
