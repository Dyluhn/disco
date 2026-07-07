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
"""

from __future__ import annotations

import html
import importlib.resources
import io
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable
from typing import TYPE_CHECKING, Literal

from disco.core.brand.tokens import Theme

if TYPE_CHECKING:
    from disco.tools.anatomy import ToolContext

# C1 imports (same package — legal same-layer import)
from disco.tools.builtin._deck_schema import (
    AuthoredDeck,
    AuthoredSlide,
    ChartSpec,
    Deck,
    Element,
    Slide,
    TableSpec,
    lower_deck,
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
    chart: ChartSpec | None = None   # C8: chart data → routes to c8 chart layout
    table: TableSpec | None = None   # C8: table data → routes to c8 table layout


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
        "chart": "metrics",    # C8: chart → metrics layout
        "table": "table",      # C8: table → table layout
    }
    slides = []
    for ds in deck.slides:
        slides.append(AuthoredSlide(
            type=_LAYOUT_MAP.get(ds.layout, "bullets"),
            title=ds.title,
            body=ds.bullets,
            image_prompt=None,  # image_url is a path, not a prompt
            notes=ds.notes,
            chart=ds.chart,    # C8: propagate chart spec
            table=ds.table,    # C8: propagate table spec
        ))
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
# EMU geometry — 16:9 canvas matching C1 spec (12 192 000 × 6 858 000 EMU)
# ---------------------------------------------------------------------------

_SLIDE_W = 12_192_000   # 13.33 inches
_SLIDE_H = 6_858_000    # 7.5 inches

# Margins / safe-area (0.5 in = 457 200 EMU)
_MARGIN = 457_200
_PIPELINE_GENERATOR_MARKER = "disco-slides-generate:pipeline-html"

# Content width / height (canvas minus symmetric margins)
_CW = _SLIDE_W - 2 * _MARGIN            # 11 277 600
_CH = _SLIDE_H - 2 * _MARGIN            # 5 943 600

# Title-strip height (≈15% of canvas)
_TITLE_H = 914_400                       # ≈ 1 in


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rgb_from_hex(hex_color: str):  # type: ignore[return]
    """Return a pptx RGBColor from a ``#rrggbb`` string."""
    from pptx.dml.color import RGBColor  # lazy import — avoids load-time dep
    h = hex_color.lstrip("#")
    return RGBColor(int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _first_font(stack: str) -> str:
    """Extract the first font name from a CSS font-family stack.

    ``"'Fraunces',Georgia,serif"`` → ``"Fraunces"``.
    """
    return stack.split(",")[0].strip().strip("'\"")


def _font_dir() -> Path:
    """Absolute path to the bundled OFL TTF fonts (via disco.core.brand)."""
    pkg = importlib.resources.files("disco.core.brand")
    return Path(str(pkg / "fonts"))


def _set_slide_bg(slide, hex_color: str) -> None:  # type: ignore[type-arg]
    """Fill a python-pptx slide background with a solid hex color."""
    bg = slide.background
    bg.fill.solid()
    bg.fill.fore_color.rgb = _rgb_from_hex(hex_color)


def _add_textbox(slide, left: int, top: int, w: int, h: int):  # type: ignore[return,type-arg]
    """Add a textbox at absolute EMU coordinates and return its text_frame.

    W-23: every frame gets ``TEXT_TO_FIT_SHAPE`` autofit (emits ``<a:normAutofit/>``
    in the slide XML) so that when a viewer substitutes the brand font (not
    installed in LibreOffice / Google Slides → different metrics → an extra wrap
    line) the text SHRINKS to stay inside the box instead of spilling out and
    overlapping the next element. Applies the safety to ordinary title / subtitle
    / kicker frames; grouped bullet frames add margins + anchor on top of this.
    """
    from pptx.enum.text import MSO_AUTO_SIZE
    from pptx.util import Emu
    shape = slide.shapes.add_textbox(Emu(left), Emu(top), Emu(w), Emu(h))
    tf = shape.text_frame
    tf.word_wrap = True
    tf.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE
    return tf


def _set_run_style(
    run,  # type: ignore[type-arg]
    text: str,
    font_name: str,
    size_pt: float,
    hex_color: str,
    bold: bool = False,
    italic: bool = False,
) -> None:
    """Apply text + font styling to a python-pptx run."""
    from pptx.util import Pt
    run.text = text
    run.font.name = font_name
    run.font.size = Pt(size_pt)
    run.font.color.rgb = _rgb_from_hex(hex_color)
    run.font.bold = bold
    run.font.italic = italic


def _add_accent_bar(slide, left: int, top: int, w: int, hex_color: str) -> None:
    """Add a thin horizontal accent rule (0.1 in tall)."""
    from pptx.util import Emu

    # Use add_shape with RECTANGLE (auto-shape type 1)
    bar = slide.shapes.add_shape(1, Emu(left), Emu(top), Emu(w), Emu(91_440))
    bar.fill.solid()
    bar.fill.fore_color.rgb = _rgb_from_hex(hex_color)
    bar.line.fill.background()   # no border line


# ---------------------------------------------------------------------------
# Brand marks — the default Disco template chrome that mirrors the PDF report:
#   • a small "Disco." wordmark on every slide (the PDF's running header), and
#   • the "disco — Latin · verb" definition-mark colophon on the cover/title
#     slide (the PDF cover's signature).
# Drawn at RENDER time (not as deck Elements) so the HTML title/bullet
# heuristics in _html_for_c1_slide stay untouched. Gated on theme.branded, so
# the neutral theme renders clean — exactly like serialize_pdf's brand gate.
# ---------------------------------------------------------------------------

_BRAND_DOT = 50_000  # ~0.055in accent dot on the wordmark


def _draw_wordmark(prs_slide, theme: Theme) -> None:  # type: ignore[type-arg]
    """Small 'Disco' + accent dot in the top margin band — the PDF running wordmark.

    Kept ENTIRELY ABOVE the title strip (titles start at _MARGIN = 0.5in): the box
    bottom must stay < _MARGIN so a long, right-reaching title never collides with it."""
    from pptx.enum.text import PP_ALIGN
    from pptx.util import Emu

    dot = _BRAND_DOT
    box_w = 1_500_000
    box_h = 259_080            # 0.283in
    top = 91_440              # 0.1in  → bottom 0.383in, clear of the 0.5in title line
    right = _SLIDE_W - _MARGIN
    text_box_right = right - dot - 36_576  # leave room for the trailing dot
    tf = _add_textbox(prs_slide, text_box_right - box_w, top, box_w, box_h)
    para = tf.paragraphs[0]
    para.alignment = PP_ALIGN.RIGHT
    run = para.add_run()
    _set_run_style(run, "Disco", _first_font(theme.font_display), 10.0, theme.text_faint)
    # Accent dot, aligned with the small wordmark text baseline.
    dot_top = top + 60_000
    d = prs_slide.shapes.add_shape(1, Emu(right - dot), Emu(dot_top), Emu(dot), Emu(dot))
    d.fill.solid()
    d.fill.fore_color.rgb = _rgb_from_hex(theme.accent)
    d.line.fill.background()


def _draw_cover_colophon(prs_slide, theme: Theme) -> None:  # type: ignore[type-arg]
    """The 'disco — Latin · verb' definition mark at the bottom of the cover/title
    slide — the PDF cover's signature colophon (headword + POS + gloss + etymology)."""
    from pptx.enum.text import PP_ALIGN

    top = int(_SLIDE_H * 0.74)
    # Short accent rule above the mark.
    _add_accent_bar(prs_slide, _MARGIN, top, 640_080, theme.accent)

    # Headword "disco" + part-of-speech / IPA on one line (two runs, two styles).
    hw_top = top + 140_000
    tf = _add_textbox(prs_slide, _MARGIN, hw_top, _CW, 360_000)
    para = tf.paragraphs[0]
    para.alignment = PP_ALIGN.LEFT
    r_hw = para.add_run()
    _set_run_style(r_hw, "disco   ", _first_font(theme.font_display), 16.0, theme.text)
    r_pos = para.add_run()
    _set_run_style(
        r_pos, "LATIN · VERB    /ˈdɪs.koː/",
        _first_font(theme.font_ui), 8.5, theme.text_faint,
    )

    # Italic gloss.
    gloss_top = hw_top + 430_000
    tf2 = _add_textbox(prs_slide, _MARGIN, gloss_top, _CW, 330_000)
    r_gloss = tf2.paragraphs[0].add_run()
    _set_run_style(
        r_gloss, "“I learn; I become acquainted with.”",
        _first_font(theme.font_reading), 12.0, theme.text_muted, italic=True,
    )

    # Etymology root.
    root_top = gloss_top + 365_000
    tf3 = _add_textbox(prs_slide, _MARGIN, root_top, _CW, 300_000)
    r_root = tf3.paragraphs[0].add_run()
    _set_run_style(
        r_root, "from discere — to learn",
        _first_font(theme.font_ui), 8.5, theme.text_faint,
    )


def _draw_brand_marks(prs_slide, slide: Slide, theme: Theme) -> None:  # type: ignore[type-arg]
    """Apply the default-template brand chrome to a rendered slide (no-op when the
    theme is unbranded). The colophon rides only the cover/title slide."""
    if not theme.branded:
        return
    _draw_wordmark(prs_slide, theme)
    # Gate the colophon on the RESOLVED layout (not the authored type) so only a
    # true rendered title cover gets it — e.g. type="title"+layout_hint="closing"
    # must NOT, and the cover slide always does.
    if slide.layout == "title":
        _draw_cover_colophon(prs_slide, theme)


# ---------------------------------------------------------------------------
# Per-layout PPTX render functions (each ≤ 200 LOC — arch budget)
# ---------------------------------------------------------------------------

def _layout_title_slide(prs_slide, slide: DeckSlide, theme: Theme) -> None:  # type: ignore[type-arg]
    """Title-slide layout: large centered display title + optional subtitle.

    Visual structure:
      [vertical center]
        accent bar (2.5 in wide)
        Title      (Fraunces, 52 pt)
        Subtitle   (Newsreader italic, 24 pt, first bullet if present)

    All elements absolutely positioned in EMU on the 16:9 canvas.
    """
    from pptx.enum.text import PP_ALIGN

    _set_slide_bg(prs_slide, theme.bg)

    # Vertical origin (centered block starting at ~30% height)
    v_origin = int(_SLIDE_H * 0.28)

    # Accent bar
    bar_w = 2_286_000   # 2.5 in
    _add_accent_bar(prs_slide, _MARGIN, v_origin, bar_w, theme.accent)

    # Title textbox
    title_top = v_origin + 182_880   # 0.2 in below bar
    title_h = 1_828_800              # 2 in
    tf_title = _add_textbox(
        prs_slide, _MARGIN, title_top, _CW, title_h
    )
    p = tf_title.paragraphs[0]
    p.alignment = PP_ALIGN.LEFT
    run = p.add_run()
    _set_run_style(
        run,
        slide.title,
        _first_font(theme.font_display),
        52,
        theme.text,
        bold=False,
    )

    # Subtitle (first bullet, if any)
    if slide.bullets:
        sub_top = title_top + title_h
        sub_h = 914_400              # 1 in
        tf_sub = _add_textbox(prs_slide, _MARGIN, sub_top, _CW, sub_h)
        p_sub = tf_sub.paragraphs[0]
        p_sub.alignment = PP_ALIGN.LEFT
        run_sub = p_sub.add_run()
        _set_run_style(
            run_sub,
            slide.bullets[0],
            _first_font(theme.font_reading),
            24,
            theme.text_muted,
            italic=True,
        )

    # Speaker notes
    if slide.notes:
        prs_slide.notes_slide.notes_text_frame.text = slide.notes


def _layout_bullets_slide(prs_slide, slide: DeckSlide, theme: Theme) -> None:  # type: ignore[type-arg]
    """Standard title + bullets layout.

    Visual structure:
      Title (Schibsted Grotesk, 32 pt) — top 1 in, left-flush
      Accent underline bar (full content width, 2 pt thick)
      Bullets (Newsreader, 20 pt) — below bar, indented
    """
    from pptx.enum.text import PP_ALIGN

    _set_slide_bg(prs_slide, theme.bg)

    # Title
    tf_title = _add_textbox(
        prs_slide, _MARGIN, _MARGIN, _CW, _TITLE_H
    )
    p_title = tf_title.paragraphs[0]
    p_title.alignment = PP_ALIGN.LEFT
    run_title = p_title.add_run()
    _set_run_style(
        run_title,
        slide.title,
        _first_font(theme.font_ui),
        32,
        theme.text,
        bold=True,
    )

    # Accent bar under title (thin, full width)
    bar_top = _MARGIN + _TITLE_H + 45_720    # 0.05 in gap
    _add_accent_bar(prs_slide, _MARGIN, bar_top, _CW, theme.accent)

    # Bullets textbox — occupies remaining vertical space
    bullet_top = bar_top + 91_440 + 91_440   # 0.1 in bar + 0.1 in gap
    bullet_h = _SLIDE_H - bullet_top - _MARGIN
    tf_bullets = _add_textbox(
        prs_slide, _MARGIN + 228_600, bullet_top, _CW - 228_600, bullet_h
    )
    tf_bullets.word_wrap = True

    for i, bullet in enumerate(slide.bullets):
        if i == 0:
            p = tf_bullets.paragraphs[0]
        else:
            p = tf_bullets.add_paragraph()
        p.alignment = PP_ALIGN.LEFT
        # Bullet marker as a run prefix
        run = p.add_run()
        _set_run_style(
            run,
            f"• {bullet}",
            _first_font(theme.font_reading),
            20,
            theme.text,
        )

    if slide.notes:
        prs_slide.notes_slide.notes_text_frame.text = slide.notes


def _layout_section_slide(prs_slide, slide: DeckSlide, theme: Theme) -> None:  # type: ignore[type-arg]
    """Section-divider layout: vertically centered section title + optional subtitle.

    Renders with surface_1 background to visually separate sections.
    """
    from pptx.enum.text import PP_ALIGN

    _set_slide_bg(prs_slide, theme.surface_1)

    v_center = _SLIDE_H // 2

    # Accent bar (left-aligned, 1.5 in wide)
    bar_w = 1_371_600   # 1.5 in
    bar_top = v_center - 228_600    # 0.25 in above center
    _add_accent_bar(prs_slide, _MARGIN, bar_top, bar_w, theme.accent)

    # Section label (small allcaps kicker above the title)
    kicker_top = bar_top - 457_200   # 0.5 in above bar
    tf_kicker = _add_textbox(prs_slide, _MARGIN, kicker_top, _CW, 457_200)
    p_kicker = tf_kicker.paragraphs[0]
    p_kicker.alignment = PP_ALIGN.LEFT
    run_kicker = p_kicker.add_run()
    _set_run_style(
        run_kicker,
        "SECTION",
        _first_font(theme.font_ui),
        9,
        theme.accent,
        bold=True,
    )

    # Title (Fraunces, 40 pt)
    title_top = bar_top + 182_880   # 0.2 in below bar
    title_h = 1_371_600             # 1.5 in
    tf_title = _add_textbox(prs_slide, _MARGIN, title_top, _CW, title_h)
    p_title = tf_title.paragraphs[0]
    p_title.alignment = PP_ALIGN.LEFT
    run_title = p_title.add_run()
    _set_run_style(
        run_title,
        slide.title,
        _first_font(theme.font_display),
        40,
        theme.text,
    )

    # Optional subtitle (first bullet)
    if slide.bullets:
        sub_top = title_top + title_h
        tf_sub = _add_textbox(prs_slide, _MARGIN, sub_top, _CW, 914_400)
        p_sub = tf_sub.paragraphs[0]
        p_sub.alignment = PP_ALIGN.LEFT
        run_sub = p_sub.add_run()
        _set_run_style(
            run_sub,
            slide.bullets[0],
            _first_font(theme.font_reading),
            18,
            theme.text_muted,
            italic=True,
        )

    if slide.notes:
        prs_slide.notes_slide.notes_text_frame.text = slide.notes


def _layout_image_right_slide(prs_slide, slide: DeckSlide, theme: Theme) -> None:  # type: ignore[type-arg]
    """Title + bullets on left half, image placeholder on right half.

    When image_url is None (C7 image-gen not yet wired), renders a styled
    placeholder box so the layout is correct and C7 can fill it in later.
    """
    from pptx.enum.text import PP_ALIGN
    from pptx.util import Emu

    _set_slide_bg(prs_slide, theme.bg)

    half_w = _CW // 2 - 91_440     # slight gap between halves

    # Left half: title + bullets
    tf_title = _add_textbox(prs_slide, _MARGIN, _MARGIN, half_w, _TITLE_H)
    p_title = tf_title.paragraphs[0]
    p_title.alignment = PP_ALIGN.LEFT
    run_title = p_title.add_run()
    _set_run_style(
        run_title,
        slide.title,
        _first_font(theme.font_ui),
        28,
        theme.text,
        bold=True,
    )

    bar_top = _MARGIN + _TITLE_H + 45_720
    _add_accent_bar(prs_slide, _MARGIN, bar_top, half_w, theme.accent)

    bullet_top = bar_top + 182_880
    bullet_h = _SLIDE_H - bullet_top - _MARGIN
    tf_b = _add_textbox(prs_slide, _MARGIN + 228_600, bullet_top,
                        half_w - 228_600, bullet_h)
    tf_b.word_wrap = True
    for i, bullet in enumerate(slide.bullets):
        p = tf_b.paragraphs[0] if i == 0 else tf_b.add_paragraph()
        p.alignment = PP_ALIGN.LEFT
        run = p.add_run()
        _set_run_style(run, f"• {bullet}", _first_font(theme.font_reading),
                       18, theme.text)

    # Right half: image or placeholder
    img_left = _MARGIN + half_w + 182_880
    img_w = _CW - half_w - 182_880
    img_h = _CH

    if slide.image_url:
        try:
            prs_slide.shapes.add_picture(
                slide.image_url,
                Emu(img_left), Emu(_MARGIN), Emu(img_w), Emu(img_h),
            )
        except Exception:
            # Fall back to placeholder if image can't be loaded
            _image_placeholder(prs_slide, img_left, _MARGIN, img_w, img_h, theme)
    else:
        _image_placeholder(prs_slide, img_left, _MARGIN, img_w, img_h, theme)

    if slide.notes:
        prs_slide.notes_slide.notes_text_frame.text = slide.notes


def _image_placeholder(prs_slide, left: int, top: int, w: int, h: int, theme: Theme) -> None:  # type: ignore[type-arg]
    """Themed generative-art fallback for an empty image slot (image gen failed
    or is unconfigured). Replaces the old flat gray "[image]" box (Dylan
    2026-07-07: 'if image gen doesn't work then generate SVGs') — an abstract,
    deterministic geometric composition in the deck's own palette, drawn with
    native vector shapes (the PPTX analogue of the HTML renderer's inline-SVG
    fallback). Variant is seeded from the slot geometry so repeated slots in one
    deck differ but re-renders are byte-stable."""
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Emu

    # Base field.
    base = prs_slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Emu(left), Emu(top), Emu(w), Emu(h))
    base.fill.solid()
    base.fill.fore_color.rgb = _rgb_from_hex(theme.surface_1)
    base.line.color.rgb = _rgb_from_hex(theme.hairline)

    # Deterministic per-slot variant; distinct primes mix the geometry so a
    # cover slot and a side slot land on different compositions.
    variant = (left * 7 + top * 13 + w * 3 + h * 5) // 9_525 % 3
    cx, cy = left + w // 2, top + h // 2
    r_big = min(w, h) * 2 // 3
    r_mid = r_big // 2
    r_dot = max(r_big // 7, 91_440)

    def _circle(x: int, y: int, r: int, hex_color: str, outline: bool = False) -> None:
        c = prs_slide.shapes.add_shape(MSO_SHAPE.OVAL, Emu(x - r // 2), Emu(y - r // 2), Emu(r), Emu(r))
        if outline:
            c.fill.background()
            c.line.color.rgb = _rgb_from_hex(hex_color)
            c.line.width = Emu(19_050)
        else:
            c.fill.solid()
            c.fill.fore_color.rgb = _rgb_from_hex(hex_color)
            c.line.fill.background()

    def _bar(x: int, y: int, bw: int, bh: int, hex_color: str) -> None:
        b = prs_slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Emu(x), Emu(y), Emu(bw), Emu(bh))
        b.fill.solid()
        b.fill.fore_color.rgb = _rgb_from_hex(hex_color)
        b.line.fill.background()

    if variant == 0:
        # Off-center large disc + outlined echo + accent dot.
        _circle(left + w * 2 // 3, top + h // 3, r_big, theme.surface_2)
        _circle(left + w * 2 // 3, top + h // 3, r_big + r_mid // 2, theme.hairline_strong, outline=True)
        _circle(left + w // 4, top + h * 3 // 4, r_dot, theme.accent)
        _bar(left + w // 8, top + h * 5 // 6, w // 3, 27_432, theme.hairline_strong)
    elif variant == 1:
        # Horizon band + rising disc (accent) + faint moon.
        _bar(left, top + h * 2 // 3, w, h // 24 + 18_288, theme.surface_2)
        _circle(left + w // 3, top + h * 2 // 3, r_mid, theme.accent)
        _circle(left + w * 4 // 5, top + h // 4, r_dot * 2, theme.surface_2)
    else:
        # Column rhythm + accent square.
        for i in range(4):
            _bar(left + w * (2 * i + 1) // 9, top + h // 5, w // 18 + 9_525, h * 3 // 5, theme.surface_2)
        sq = max(r_dot, 137_160)
        _bar(left + w * 3 // 4, top + h * 2 // 3, sq, sq, theme.accent)


# ---------------------------------------------------------------------------
# MinimalDeck layout dispatch (compat path)
# ---------------------------------------------------------------------------

_MINIMAL_LAYOUT_FNS = {
    "title": _layout_title_slide,
    "bullets": _layout_bullets_slide,
    "section": _layout_section_slide,
    "image_right": _layout_image_right_slide,
}


# ---------------------------------------------------------------------------
# C1 Element renderer — maps each Element to a python-pptx shape
# ---------------------------------------------------------------------------

def _is_bullet_el(el: Element) -> bool:
    """A bullet body Element: a text Element whose text carries the "• " prefix.

    The C1 lowerer (`_deck_schema._layout_bullets` / `_layout_image_right` /
    `_layout_two_column` / `_layout_comparison`) emits each bullet as an
    independent absolutely-positioned textbox with this prefix; W-23 regroups
    them at render time so a wrapped 2nd line cannot spill into the next box.
    """
    return el.kind == "text" and el.text.startswith("• ")


def _render_bullet_group(prs_slide, group: list[Element], theme: Theme) -> None:  # type: ignore[type-arg]
    """W-23: render consecutive bullet Elements as ONE auto-fit text frame —
    one PARAGRAPH per bullet — instead of one absolute textbox per bullet.

    The per-bullet model (`top = body_top + i*line_h`, `height = line_h*1.15`)
    reserves NO space for a wrapped 2nd line; under font substitution in
    LibreOffice / Google Slides that 2nd line spilled into the next bullet's
    absolute box → overlap. Grouping into one flowing frame (word_wrap + TOP
    anchor + zero margins + small `space_after`, autofit via `_add_textbox`)
    lets bullets flow and never overlap. Text keeps its "• " prefix so the
    deck stays editable as plain paragraphs.
    """
    from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
    from pptx.util import Emu, Pt

    _ALIGN_MAP = {"LEFT": PP_ALIGN.LEFT, "CENTER": PP_ALIGN.CENTER, "RIGHT": PP_ALIGN.RIGHT}

    first = group[0]
    left = int(first.left)
    width = int(first.width)
    top = int(first.top)
    # Span down to the bottom safe-area margin so wrapped bullets have room to
    # flow; normAutofit (set in _add_textbox) only shrinks if even that overflows.
    height = max(int(first.height), (_SLIDE_H - _MARGIN) - top)

    tf = _add_textbox(prs_slide, left, top, width, height)
    tf.word_wrap = True
    tf.vertical_anchor = MSO_ANCHOR.TOP
    tf.margin_left = Emu(0)
    tf.margin_right = Emu(0)
    tf.margin_top = Emu(0)
    tf.margin_bottom = Emu(0)

    for i, el in enumerate(group):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = _ALIGN_MAP.get(el.align, PP_ALIGN.LEFT)
        p.line_spacing = 1.05
        p.space_after = Pt(6)
        run = p.add_run()
        _set_run_style(
            run, el.text,
            el.font_name or "Helvetica",
            el.font_size_pt,
            el.hex_color or "#1a1813",
            bold=el.bold,
            italic=el.italic,
        )


def _render_slide_elements(prs_slide, slide: Slide, theme: Theme) -> None:  # type: ignore[type-arg]
    """Render a slide's Elements, grouping consecutive bullet text Elements that
    share the same left/width into ONE auto-fit frame (W-23).

    Same-left/width grouping keeps the left vs right columns of two_column /
    comparison layouts as separate frames (their bullets differ by `left`), and
    leaves non-bullet elements (title, accent bar, image, labels) untouched.
    """
    group: list[Element] = []

    def _flush() -> None:
        if group:
            _render_bullet_group(prs_slide, list(group), theme)
            group.clear()

    for el in slide.elements:
        if _is_bullet_el(el) and (
            not group
            or (int(el.left) == int(group[0].left) and int(el.width) == int(group[0].width))
        ):
            group.append(el)
            continue
        _flush()
        if _is_bullet_el(el):
            group.append(el)
        else:
            _render_element(prs_slide, el, theme)
    _flush()


def _render_element(prs_slide, el: Element, theme: Theme) -> None:  # type: ignore[type-arg]
    """Render one C1 Element to a python-pptx slide shape."""

    if el.kind == "text":
        _render_text_element(prs_slide, el)
    elif el.kind == "accent_bar":
        _render_accent_bar_element(prs_slide, el)
    elif el.kind == "image":
        _render_image_element(prs_slide, el, theme)
    elif el.kind == "rect":
        _render_rect_element(prs_slide, el)
    # unknown kinds are skipped cleanly (no crash)


def _render_text_element(prs_slide, el: Element) -> None:  # type: ignore[type-arg]
    """Render a text Element as an absolutely-positioned textbox."""
    from pptx.enum.text import PP_ALIGN

    _ALIGN_MAP = {"LEFT": PP_ALIGN.LEFT, "CENTER": PP_ALIGN.CENTER, "RIGHT": PP_ALIGN.RIGHT}
    tf = _add_textbox(
        prs_slide,
        int(el.left), int(el.top), int(el.width), int(el.height),
    )
    tf.word_wrap = el.word_wrap
    p = tf.paragraphs[0]
    p.alignment = _ALIGN_MAP.get(el.align, PP_ALIGN.LEFT)
    run = p.add_run()
    # NOTE: python-pptx only stores the font *name* in the slide XML (e.g.
    # "Fraunces"). PPTX viewers (PowerPoint, LibreOffice) render it only if the
    # font is installed on the host OS. The base64 data-URI embedding in
    # font_face_css() fixes HTML/PDF output; PPTX fonts are NOT fixed here.
    _set_run_style(
        run, el.text,
        el.font_name or "Helvetica",
        el.font_size_pt,
        el.hex_color or "#1a1813",
        bold=el.bold,
        italic=el.italic,
    )


def _render_accent_bar_element(prs_slide, el: Element) -> None:  # type: ignore[type-arg]
    """Render an accent_bar Element as a thin filled rectangle."""
    _add_accent_bar(
        prs_slide,
        int(el.left), int(el.top), int(el.width),
        el.fill_hex,
    )


def _render_image_element(prs_slide, el: Element, theme: Theme) -> None:  # type: ignore[type-arg]
    """Render an image Element; falls back to styled placeholder when there is
    neither generated bytes nor a readable path."""
    import io

    from pptx.util import Emu

    # C7 wire: prefer the generated bytes (sandbox-agnostic) over a path on disk.
    source: object | None = io.BytesIO(el.image_bytes) if el.image_bytes else el.image_path
    if source is not None:
        try:
            prs_slide.shapes.add_picture(
                source,
                Emu(int(el.left)), Emu(int(el.top)),
                Emu(int(el.width)), Emu(int(el.height)),
            )
            return
        except Exception:
            pass  # fall through to placeholder

    # Styled placeholder box (visible "image" label)
    _image_placeholder(
        prs_slide,
        int(el.left), int(el.top),
        int(el.width), int(el.height),
        theme,
    )


def _render_rect_element(prs_slide, el: Element) -> None:  # type: ignore[type-arg]
    """Render a rect Element as a filled rectangle shape."""
    from pptx.util import Emu

    box = prs_slide.shapes.add_shape(
        1,
        Emu(int(el.left)), Emu(int(el.top)),
        Emu(int(el.width)), Emu(int(el.height)),
    )
    box.fill.solid()
    box.fill.fore_color.rgb = _rgb_from_hex(el.fill_hex)
    if el.border_hex:
        box.line.color.rgb = _rgb_from_hex(el.border_hex)
    else:
        box.line.fill.background()


def _slide_archetype(slide: Slide) -> str:
    return str(getattr(slide, "archetype", None) or "").strip().lower()


def _plain_element_text(el: Element) -> str:
    return el.text[2:] if el.text.startswith("• ") else el.text


def _title_element(slide: Slide) -> Element | None:
    texts = [el for el in slide.elements if el.kind == "text" and el.text.strip()]
    if not texts:
        return None
    bold_heads = [el for el in texts if el.bold and el.font_size_pt >= 20]
    return max(bold_heads or texts, key=lambda el: el.font_size_pt)


def _body_strings(slide: Slide) -> list[str]:
    title_el = _title_element(slide)
    out: list[str] = []
    for el in slide.elements:
        if el.kind != "text" or el is title_el:
            continue
        text = _plain_element_text(el).strip()
        if text:
            out.append(text)
    return out


def _split_timeline_label(raw: str) -> tuple[str, str]:
    for sep in (" — ", " - ", "|", ":"):
        if sep in raw:
            left, right = raw.split(sep, 1)
            return left.strip(), right.strip()
    return raw.strip(), ""


def _add_pptx_text(
    prs_slide,
    text: str,
    *,
    left: int,
    top: int,
    width: int,
    height: int,
    theme: Theme,
    font_name: str,
    size_pt: float,
    color: str,
    bold: bool = False,
    italic: bool = False,
    align: str = "LEFT",
) -> None:
    from pptx.enum.text import PP_ALIGN

    align_map = {"LEFT": PP_ALIGN.LEFT, "CENTER": PP_ALIGN.CENTER, "RIGHT": PP_ALIGN.RIGHT}
    tf = _add_textbox(prs_slide, left, top, width, height)
    p = tf.paragraphs[0]
    p.alignment = align_map.get(align, PP_ALIGN.LEFT)
    run = p.add_run()
    _set_run_style(run, text, font_name, size_pt, color, bold=bold, italic=italic)


def _render_big_number_pptx(prs_slide, slide: Slide, theme: Theme) -> None:  # type: ignore[type-arg]
    body = _body_strings(slide)
    figure = body[0] if body else slide.title
    support = body[1] if len(body) > 1 else (slide.title if body else "")
    if slide.title and slide.title != support:
        _add_pptx_text(
            prs_slide,
            slide.title,
            left=_MARGIN,
            top=_MARGIN,
            width=_CW,
            height=365_760,
            theme=theme,
            font_name=_first_font(theme.font_ui),
            size_pt=18.0,
            color=theme.text_muted,
            bold=True,
        )
    _add_pptx_text(
        prs_slide,
        figure,
        left=_MARGIN,
        top=1_371_600,
        width=_CW,
        height=2_286_000,
        theme=theme,
        font_name=_first_font(theme.font_display),
        size_pt=150.0,
        color=theme.accent,
        bold=True,
        align="CENTER",
    )
    if support:
        _add_pptx_text(
            prs_slide,
            support,
            left=int(_MARGIN * 1.5),
            top=3_657_600,
            width=_SLIDE_W - int(_MARGIN * 3),
            height=731_520,
            theme=theme,
            font_name=_first_font(theme.font_reading),
            size_pt=24.0,
            color=theme.text,
            align="CENTER",
        )


def _render_quote_pptx(prs_slide, slide: Slide, theme: Theme) -> None:  # type: ignore[type-arg]
    body = _body_strings(slide)
    quote = body[0] if body else slide.title
    attribution = body[1] if len(body) > 1 else ""
    _add_pptx_text(
        prs_slide,
        "“",
        left=_MARGIN,
        top=914_400,
        width=914_400,
        height=1_371_600,
        theme=theme,
        font_name=_first_font(theme.font_display),
        size_pt=120.0,
        color=theme.accent,
        bold=True,
    )
    _add_pptx_text(
        prs_slide,
        quote,
        left=1_371_600,
        top=1_280_160,
        width=_SLIDE_W - 2_286_000,
        height=2_743_200,
        theme=theme,
        font_name=_first_font(theme.font_reading),
        size_pt=34.0,
        color=theme.text,
        italic=True,
    )
    if attribution:
        _add_pptx_text(
            prs_slide,
            attribution,
            left=1_371_600,
            top=4_206_240,
            width=_SLIDE_W - 2_286_000,
            height=457_200,
            theme=theme,
            font_name=_first_font(theme.font_ui),
            size_pt=16.0,
            color=theme.text_muted,
            bold=True,
        )


def _render_timeline_pptx(prs_slide, slide: Slide, theme: Theme) -> None:  # type: ignore[type-arg]
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Emu

    items = _body_strings(slide) or [slide.title]
    title = slide.title
    if title:
        _add_pptx_text(
            prs_slide,
            title,
            left=_MARGIN,
            top=_MARGIN,
            width=_CW,
            height=548_640,
            theme=theme,
            font_name=_first_font(theme.font_ui),
            size_pt=24.0,
            color=theme.text,
            bold=True,
        )
    margin = 914_400
    y = 3_337_560
    spine = prs_slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE, Emu(margin), Emu(y), Emu(_SLIDE_W - 2 * margin), Emu(18_288)
    )
    spine.fill.solid()
    spine.fill.fore_color.rgb = _rgb_from_hex(theme.accent)
    spine.line.fill.background()
    denom = max(1, len(items) - 1)
    for i, item in enumerate(items):
        x = margin + int(i * ((_SLIDE_W - 2 * margin) / denom))
        node = prs_slide.shapes.add_shape(
            MSO_SHAPE.OVAL, Emu(x - 54_864), Emu(y - 54_864), Emu(109_728), Emu(109_728)
        )
        node.fill.solid()
        node.fill.fore_color.rgb = _rgb_from_hex(theme.accent)
        node.line.fill.background()
        head, detail = _split_timeline_label(item)
        label = head if not detail else f"{head}\n{detail}"
        label_top = y - 914_400 if i % 2 == 0 else y + 228_600
        _add_pptx_text(
            prs_slide,
            label,
            left=x - 914_400,
            top=label_top,
            width=1_828_800,
            height=731_520,
            theme=theme,
            font_name=_first_font(theme.font_reading),
            size_pt=14.0,
            color=theme.text,
            align="CENTER",
        )


def _render_two_by_two_pptx(prs_slide, slide: Slide, theme: Theme) -> None:  # type: ignore[type-arg]
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Emu

    body = _body_strings(slide)
    # Axis labels ONLY when the model authored them (6+ body lines: x, y, 4 cells).
    # The old fallback INVENTED "Higher impact/certainty" on 4-cell content — a
    # fabricated analytical framing the quadrants never plotted (no false
    # affordances; gauntlet e-news visual review 2026-07-07).
    axis_x = body[0] if len(body) >= 6 else None
    axis_y = body[1] if len(body) >= 6 else None
    cells = body[2:6] if len(body) >= 6 else body[:4]
    while len(cells) < 4:
        cells.append("")
    _add_pptx_text(
        prs_slide,
        slide.title,
        left=_MARGIN,
        top=_MARGIN,
        width=_CW,
        height=548_640,
        theme=theme,
        font_name=_first_font(theme.font_ui),
        size_pt=24.0,
        color=theme.text,
        bold=True,
    )
    left = 1_371_600
    top = 1_371_600
    grid_w = _SLIDE_W - 2_743_200
    grid_h = 4_389_120
    cell_w = grid_w // 2
    cell_h = grid_h // 2
    for idx, label in enumerate(cells):
        col = idx % 2
        row = idx // 2
        box = prs_slide.shapes.add_shape(
            MSO_SHAPE.RECTANGLE,
            Emu(left + col * cell_w),
            Emu(top + row * cell_h),
            Emu(cell_w),
            Emu(cell_h),
        )
        box.fill.solid()
        box.fill.fore_color.rgb = _rgb_from_hex(theme.surface_1)
        box.line.color.rgb = _rgb_from_hex(theme.hairline)
        _add_pptx_text(
            prs_slide,
            label,
            left=left + col * cell_w + 137_160,
            top=top + row * cell_h + 137_160,
            width=cell_w - 274_320,
            height=cell_h - 274_320,
            theme=theme,
            font_name=_first_font(theme.font_reading),
            size_pt=18.0,
            color=theme.text,
            bold=True,
        )
    if axis_x:
        _add_pptx_text(
            prs_slide,
            axis_x,
            left=left + grid_w - 1_828_800,
            top=top + grid_h + 91_440,
            width=1_828_800,
            height=274_320,
            theme=theme,
            font_name=_first_font(theme.font_ui),
            size_pt=10.0,
            color=theme.accent,
            bold=True,
            align="RIGHT",
        )
    if axis_y:
        _add_pptx_text(
            prs_slide,
            axis_y,
            left=left - 914_400,
            top=top,
            width=822_960,
            height=274_320,
            theme=theme,
            font_name=_first_font(theme.font_ui),
            size_pt=10.0,
            color=theme.accent,
            bold=True,
            align="RIGHT",
        )


def _render_archetype_pptx(prs_slide, slide: Slide, theme: Theme) -> bool:  # type: ignore[type-arg]
    archetype = _slide_archetype(slide)
    if archetype == "big_number":
        _render_big_number_pptx(prs_slide, slide, theme)
        return True
    if archetype == "quote":
        _render_quote_pptx(prs_slide, slide, theme)
        return True
    if archetype == "timeline":
        _render_timeline_pptx(prs_slide, slide, theme)
        return True
    if archetype == "two_by_two":
        _render_two_by_two_pptx(prs_slide, slide, theme)
        return True
    return False


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


def _render_pptx_c1(deck: Deck) -> bytes:
    """Render a C1 Deck to .pptx bytes."""
    from pptx import Presentation
    from pptx.util import Emu

    prs = Presentation()
    prs.slide_width = Emu(_SLIDE_W)
    prs.slide_height = Emu(_SLIDE_H)
    blank_layout = prs.slide_layouts[6]

    for slide in deck.slides:
        prs_slide = prs.slides.add_slide(blank_layout)
        _set_slide_bg(prs_slide, deck.theme.bg)

        # C8: chart/table slides delegate to native chart/table shapes
        if slide.chart is not None:
            from disco.tools.builtin._c8_chart_layouts import layout_chart_slide_pptx
            layout_chart_slide_pptx(prs_slide, slide, deck.theme)
        elif slide.table is not None:
            from disco.tools.builtin._c8_chart_layouts import layout_table_slide_pptx
            layout_table_slide_pptx(prs_slide, slide, deck.theme)
        else:
            if not _render_archetype_pptx(prs_slide, slide, deck.theme):
                _render_slide_elements(prs_slide, slide, deck.theme)

            if slide.notes:
                _ntf = prs_slide.notes_slide.notes_text_frame
                if _ntf is not None:
                    _ntf.text = slide.notes

        # Default-template brand chrome (wordmark on every slide; colophon on the
        # cover) — applied after content so it overlays cleanly. Chart/table slides
        # get the wordmark too (theme-gated inside).
        _draw_brand_marks(prs_slide, slide, deck.theme)

    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# render_html — 16:9 brand HTML (accepts Deck or MinimalDeck)
# ---------------------------------------------------------------------------

def _img_src(el: Element) -> str | None:
    """C7 wire: the HTML <img src> for an image Element — an inline data-URI from
    the generated bytes (sandbox-agnostic, no extra HTTP fetch), else the stored
    path, else None (→ the caller renders the [image] placeholder)."""
    if el.image_bytes:
        import base64

        sig = el.image_bytes[:3]
        mime = "image/jpeg" if sig == b"\xff\xd8\xff" else "image/png"
        b64 = base64.b64encode(el.image_bytes).decode("ascii")
        return f"data:{mime};base64,{b64}"
    if el.image_path:
        return html.escape(el.image_path)
    return None


def _art_fallback_svg(variant: int = 0, *, style: str = "") -> str:
    """Inline-SVG generative-art fallback for an empty image slot (the HTML
    analogue of the PPTX `_image_placeholder` shape composition). Uses the
    deck's CSS custom properties so it re-themes with the template. Dylan
    2026-07-07: 'if image gen doesn't work then generate SVGs'."""
    v = variant % 3
    if v == 0:
        art = (
            '<circle cx="66" cy="30" r="24" fill="var(--surface-2)"/>'
            '<circle cx="66" cy="30" r="30" fill="none" stroke="var(--hairline-strong)" stroke-width="0.6"/>'
            '<circle cx="25" cy="68" r="4" fill="var(--accent)"/>'
            '<rect x="12" y="82" width="34" height="1.2" fill="var(--hairline-strong)"/>'
        )
    elif v == 1:
        art = (
            '<rect x="0" y="62" width="100" height="5" fill="var(--surface-2)"/>'
            '<circle cx="33" cy="62" r="12" fill="var(--accent)"/>'
            '<circle cx="80" cy="24" r="7" fill="var(--surface-2)"/>'
        )
    else:
        cols = "".join(
            f'<rect x="{22 * i + 11}" y="20" width="6" height="56" fill="var(--surface-2)"/>'
            for i in range(4)
        )
        art = cols + '<rect x="74" y="64" width="9" height="9" fill="var(--accent)"/>'
    return (
        f'<svg viewBox="0 0 100 100" preserveAspectRatio="xMidYMid slice" '
        f'aria-hidden="true" style="background:var(--surface-1);{style}">{art}</svg>'
    )


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


def _brand_marks_html(slide: Slide, theme: Theme) -> str:
    """The default-template brand chrome for one HTML slide — the 'Disco.' wordmark
    (every slide) + the 'disco — Latin · verb' colophon on the cover/title slide.
    Mirrors _draw_brand_marks for PPTX. Empty when the theme is unbranded."""
    if not theme.branded:
        return ""
    wordmark = '<div class="brand-wordmark">Disco<span class="dot">.</span></div>'
    if slide.layout != "title":
        return wordmark
    colophon = (
        '<div class="brand-colophon">'
        '<div class="bc-l1"><span class="bc-hw">disco</span>'
        '<span class="bc-pos">Latin&nbsp;·&nbsp;verb</span>'
        '<span class="bc-ipa">/ˈdɪs.koː/</span></div>'
        '<div class="bc-rule"></div>'
        '<div class="bc-gloss">“I learn; I become acquainted with.”</div>'
        '<div class="bc-root">from <em>discere</em> — to learn</div>'
        '</div>'
    )
    return wordmark + colophon


def _render_html_c1(deck: Deck) -> str:  # noqa: C901
    """Render a C1 Deck to a self-contained 16:9 brand HTML string."""
    from disco.core.brand.css import font_face_css, theme_css_vars

    theme = deck.theme
    font_css = font_face_css()
    vars_css = theme_css_vars(theme)

    sections_html: list[str] = []
    for i, slide in enumerate(deck.slides):
        active = ' active' if i == 0 else ''
        bg = theme.surface_1 if slide.layout == "section_header" else theme.bg
        archetype = _slide_archetype(slide)
        archetype_attr = (
            f'data-archetype="{html.escape(archetype)}" ' if archetype else ""
        )

        inner = _html_for_c1_slide(slide, theme, slide_idx=i)
        brand = _brand_marks_html(slide, theme)
        sections_html.append(
            f'<section class="slide{active}" id="slide-{i}" '
            f'data-slide-id="slide-{i}" '
            f'data-layout="{html.escape(slide.layout)}" '
            f'{archetype_attr}'
            f'style="background:{html.escape(bg)}">\n{inner}\n{brand}\n</section>'
        )

    slides_joined = "\n".join(sections_html)

    return f"""\
<!DOCTYPE html>
<!-- {_PIPELINE_GENERATOR_MARKER} v1 -->
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(deck.title)}</title>
<style>
{font_css}
{vars_css}
/* 16:9 slide deck shell */
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{background:#000;display:flex;align-items:center;justify-content:center;
  min-height:100vh;overflow:hidden;}}
.deck{{position:relative;width:100vw;height:56.25vw;max-height:100vh;
  max-width:177.78vh;overflow:hidden;}}
.slide{{display:none;position:absolute;inset:0;width:100%;height:100%;
  padding:3% 4%;font-family:var(--ui);color:var(--text);}}
.slide.active{{display:flex;flex-direction:column;justify-content:center;}}
/* layout-specific */
.slide-title-bar{{width:20%;height:3px;background:var(--accent);margin-bottom:2%;}}
.slide-title{{font-family:var(--display);font-size:4vw;line-height:1.15;
  color:var(--text);margin-bottom:1%;}}
.slide-subtitle{{font-family:var(--reading);font-style:italic;font-size:2vw;
  color:var(--text-muted);}}
.slide-heading{{font-family:var(--ui);font-size:2.5vw;font-weight:700;
  color:var(--text);margin-bottom:1%;}}
.slide-rule{{width:100%;height:2px;background:var(--accent);margin-bottom:2%;}}
.slide-bullets{{list-style:none;padding-left:2%;}}
.slide-bullets li{{font-family:var(--reading);font-size:1.8vw;line-height:1.5;
  color:var(--text);margin-bottom:0.8%;padding-left:1.5%;
  border-left:3px solid var(--accent);}}
.slide-section-kicker{{font-family:var(--ui);font-size:0.9vw;font-weight:700;
  letter-spacing:.15em;text-transform:uppercase;color:var(--accent);
  margin-bottom:1%;}}
.slide-section-title{{font-family:var(--display);font-size:4.5vw;
  color:var(--text);line-height:1.1;}}
.slide-kicker{{font-family:var(--ui);font-size:0.95vw;font-weight:700;
  color:var(--text-muted);text-transform:uppercase;letter-spacing:0;}}
.slide-big-number-layout{{height:100%;display:flex;flex-direction:column;
  align-items:center;justify-content:center;text-align:center;gap:2%;}}
.slide-big-number-figure{{font-family:var(--display);font-size:200px;line-height:.85;
  color:var(--accent);font-weight:800;}}
.slide-big-number-support{{font-family:var(--reading);font-size:2.1vw;line-height:1.25;
  color:var(--text);max-width:70%;}}
.slide-quote-layout{{position:relative;height:100%;display:flex;flex-direction:column;
  justify-content:center;padding:0 8% 0 12%;}}
.slide-quote-mark{{position:absolute;left:4%;top:14%;font-family:var(--display);
  font-size:12vw;line-height:1;color:var(--accent);font-weight:800;opacity:.9;}}
.slide-quote-text{{font-family:var(--reading);font-size:3.2vw;line-height:1.18;
  color:var(--text);font-style:italic;max-width:86%;}}
.slide-quote-attribution{{font-family:var(--ui);font-size:1.15vw;color:var(--text-muted);
  font-weight:700;margin-top:2%;}}
.slide-timeline-layout{{height:100%;display:flex;flex-direction:column;justify-content:center;}}
.slide-timeline{{position:relative;height:56%;margin:3% 5% 0;}}
.slide-timeline-spine{{position:absolute;left:8%;right:8%;top:50%;height:3px;
  background:var(--accent);}}
.slide-timeline-node{{position:absolute;top:50%;width:14px;height:14px;border-radius:50%;
  background:var(--accent);transform:translate(-50%,-50%);box-shadow:0 0 0 6px var(--bg);}}
.slide-timeline-label{{position:absolute;width:22%;transform:translateX(-50%);
  font-family:var(--reading);font-size:1.1vw;line-height:1.25;color:var(--text);text-align:center;}}
.slide-timeline-label.above{{bottom:55%;}}
.slide-timeline-label.below{{top:58%;}}
.slide-timeline-label strong{{display:block;font-family:var(--ui);font-size:.9vw;
  color:var(--accent);margin-bottom:.25em;}}
.slide-two-by-two-layout{{height:100%;display:flex;flex-direction:column;justify-content:center;}}
.slide-two-by-two-grid{{position:relative;display:grid;grid-template-columns:1fr 1fr;
  grid-template-rows:1fr 1fr;gap:0;border:1px solid var(--hairline);height:66%;
  margin:2% 8% 0;background:var(--surface-1);}}
.slide-two-by-two-cell{{display:flex;align-items:center;justify-content:center;
  padding:6%;border:1px solid var(--hairline);font-family:var(--reading);
  font-size:1.45vw;line-height:1.25;color:var(--text);text-align:center;font-weight:700;}}
.slide-two-by-two-axis{{position:absolute;font-family:var(--ui);font-size:.85vw;
  color:var(--accent);font-weight:800;text-transform:uppercase;letter-spacing:0;}}
.slide-two-by-two-axis.x-end{{right:0;bottom:-7%;}}
.slide-two-by-two-axis.y-end{{left:-8%;top:0;transform:rotate(-90deg);transform-origin:left top;}}
.slide-full-image-wrap{{position:relative;width:100%;height:100%;overflow:hidden;}}
.slide-full-image-bg{{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;}}
.slide-full-image-placeholder{{position:absolute;inset:0;background:var(--surface-2);
  border:1px solid var(--hairline);display:flex;align-items:center;justify-content:center;
  color:var(--text-faint);font-family:var(--ui);font-size:1.2vw;}}
.slide-image-scrim{{position:absolute;inset:0;background:
  linear-gradient(90deg,rgba(0,0,0,.72) 0%,rgba(0,0,0,.52) 38%,rgba(0,0,0,.12) 72%,rgba(0,0,0,0) 100%);}}
.slide-full-image-copy{{position:absolute;left:5%;bottom:8%;max-width:68%;z-index:1;}}
.slide-full-image-copy .slide-heading,
.slide-full-image-copy .slide-title{{color:#fff;text-shadow:0 1px 2px rgba(0,0,0,.35);}}
.slide-full-image-copy .slide-subtitle{{color:rgba(255,255,255,.86);}}
.slide-full-image-copy .slide-bullets li{{color:#fff;border-left-color:rgba(255,255,255,.72);
  text-shadow:0 1px 2px rgba(0,0,0,.35);}}
/* nav controls */
.nav{{position:fixed;bottom:1%;right:1%;display:flex;gap:.5em;z-index:10;}}
.nav button{{background:var(--surface-2);border:1px solid var(--hairline);
  color:var(--text-muted);font-family:var(--ui);font-size:.9em;
  padding:.3em .8em;border-radius:4px;cursor:pointer;}}
.nav button:hover{{background:var(--surface-1);}}
.slide-counter{{position:fixed;bottom:1%;left:1%;font-family:var(--ui);
  font-size:.8em;color:var(--text-faint);}}
/* chart / table layouts (C8) */
.slide-chart{{width:100%;margin-top:1%;overflow:hidden;}}
.slide-chart svg{{max-width:100%;height:auto;}}
.slide-table-wrap{{width:100%;overflow-x:auto;margin-top:1%;}}
.slide-table{{border-collapse:collapse;width:100%;font-family:var(--reading);font-size:1.4vw;}}
.slide-table thead th{{background:var(--accent);color:#fff;font-weight:bold;
  padding:.4em .6em;text-align:left;border:1px solid rgba(255,255,255,0.2);}}
.slide-table tbody td{{padding:.35em .6em;border:1px solid var(--hairline);
  color:var(--text);}}
.slide-table tbody tr:nth-child(even){{background:var(--surface-1);}}
.slide-table-empty{{font-family:var(--ui);color:var(--text-faint);font-size:1.2vw;margin-top:2%;}}
/* default-template brand chrome (mirrors the PDF report) */
.brand-wordmark{{position:absolute;top:3.5%;right:4%;font-family:var(--display);
  font-weight:600;font-size:1.15vw;color:var(--text-faint);letter-spacing:-.01em;
  pointer-events:none;}}
.brand-wordmark .dot{{color:var(--accent);}}
.brand-colophon{{position:absolute;left:4%;bottom:7%;max-width:60%;pointer-events:none;}}
.brand-colophon .bc-rule{{width:7%;height:2px;background:var(--accent);margin:.7vw 0;}}
.bc-l1{{display:flex;align-items:baseline;gap:.7vw;}}
.bc-hw{{font-family:var(--display);font-size:1.7vw;color:var(--text);}}
.bc-pos{{font-family:var(--ui);font-size:.78vw;letter-spacing:.14em;
  text-transform:uppercase;color:var(--text-faint);}}
.bc-ipa{{font-family:var(--mono);font-size:.78vw;color:var(--text-faint);}}
.bc-gloss{{font-family:var(--reading);font-style:italic;font-size:1.2vw;
  color:var(--text-muted);}}
.bc-root{{font-family:var(--ui);font-size:.8vw;color:var(--text-faint);margin-top:.3vw;}}
</style>
</head>
<body>
<div class="deck">
{slides_joined}
</div>
<div class="nav">
  <button id="btn-prev" aria-label="Previous slide">←</button>
  <button id="btn-next" aria-label="Next slide">→</button>
</div>
<div class="slide-counter" id="counter"></div>
<script>
(function(){{
  var slides = document.querySelectorAll('.slide');
  var cur = 0;
  function go(n) {{
    slides[cur].classList.remove('active');
    cur = Math.max(0, Math.min(slides.length - 1, n));
    slides[cur].classList.add('active');
    document.getElementById('counter').textContent =
      (cur + 1) + ' / ' + slides.length;
  }}
  document.getElementById('btn-prev').addEventListener('click', function(){{ go(cur - 1); }});
  document.getElementById('btn-next').addEventListener('click', function(){{ go(cur + 1); }});
  document.addEventListener('keydown', function(e) {{
    if (e.key === 'ArrowRight' || e.key === 'ArrowDown') go(cur + 1);
    else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') go(cur - 1);
  }});
  go(0);
}})();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# strip_element_ids — pure helper: remove editor-only data-* attributes
# ---------------------------------------------------------------------------
# Compiled once at import time.  Each pattern matches the attribute AND the
# single space that separates it from the previous attribute in a tag — the
# leading ``\s*`` consumes that one space so no double-space gap is left
# behind.  All other whitespace, text, CSS, JS, and preformatted content is
# preserved byte-for-byte.
# Match the two attributes (with their separating leading whitespace) ONLY as
# start-tag attributes — applied per-tag, never to free text / CSS / JS / <pre>.
_TAG_RE = re.compile(r"<[^>]+>")
_STRIP_ELEMENT_ID_RE = re.compile(r'\s+data-element-id="[^"]*"')
_STRIP_SLIDE_ID_RE = re.compile(r'\s+data-slide-id="[^"]*"')


def strip_element_ids(html_str: str) -> str:
    """Strip ``data-element-id="..."`` and ``data-slide-id="..."`` from *html_str*.

    Pure helper — removes those two attributes ONLY where they appear as start-tag
    attributes (the leading whitespace separating them from the previous attribute
    is consumed so no double space is left).  Text, CSS, ``<style>``/``<script>``
    content, and ``<pre>`` blocks are preserved byte-for-byte even if they happen
    to contain the literal attribute strings — removal is scoped to ``<...>`` tags
    only.  Idempotent: a second call is a no-op.

    Note: RESTORED here as a tested utility but NOT yet wired to any export path.
    ``render_html`` still stamps the attributes so the §4.1 in-preview
    SelectionOverlay can read them; callers needing a clean downloadable variant
    must call this explicitly.
    """
    def _clean_tag(m: re.Match[str]) -> str:
        tag = _STRIP_ELEMENT_ID_RE.sub("", m.group(0))
        return _STRIP_SLIDE_ID_RE.sub("", tag)

    return _TAG_RE.sub(_clean_tag, html_str)


BodyEidFn = Callable[[int], str]


def _html_body_text(el: Element) -> str:
    return html.escape(_plain_element_text(el))


def _html_big_number(
    sid: str,
    title_html: str,
    title_el: Element | None,
    body: list[Element],
    body_eid: BodyEidFn,
) -> str:
    figure_el = body[0] if body else title_el
    support_el = body[1] if len(body) > 1 else None
    figure = (
        f'<div class="slide-big-number-figure" data-element-id="{body_eid(0)}" '
        f'data-slide-id="{sid}">{_html_body_text(figure_el)}</div>'
        if figure_el is not None else ""
    )
    support = (
        f'<p class="slide-big-number-support" data-element-id="{body_eid(1)}" '
        f'data-slide-id="{sid}">{_html_body_text(support_el)}</p>'
        if support_el is not None else ""
    )
    return (
        '<div class="slide-big-number-layout">'
        f'{title_html}{figure}{support}'
        '</div>'
    )


def _html_quote(
    sid: str,
    title_el: Element | None,
    body: list[Element],
    body_eid: BodyEidFn,
) -> str:
    quote_el = body[0] if body else title_el
    attr_el = body[1] if len(body) > 1 else None
    quote = (
        f'<blockquote class="slide-quote-text" data-element-id="{body_eid(0)}" '
        f'data-slide-id="{sid}">{_html_body_text(quote_el)}</blockquote>'
        if quote_el is not None else ""
    )
    attribution = (
        f'<figcaption class="slide-quote-attribution" data-element-id="{body_eid(1)}" '
        f'data-slide-id="{sid}">{_html_body_text(attr_el)}</figcaption>'
        if attr_el is not None else ""
    )
    return (
        '<figure class="slide-quote-layout">'
        '<div class="slide-quote-mark" aria-hidden="true">“</div>'
        f'{quote}{attribution}'
        '</figure>'
    )


def _html_timeline(
    sid: str,
    title_html: str,
    title_el: Element | None,
    body: list[Element],
    body_eid: BodyEidFn,
) -> str:
    nodes: list[str] = []
    labels: list[str] = []
    items = body or ([title_el] if title_el is not None else [])
    denom = max(1, len(items) - 1)
    for j, el in enumerate(items):
        x = 8.0 + (j * (100.0 - 16.0) / denom if len(items) > 1 else 42.0)
        head, detail = _split_timeline_label(_plain_element_text(el))
        label_cls = "above" if j % 2 == 0 else "below"
        nodes.append(
            f'<div class="slide-timeline-node" style="left:{x:.2f}%"></div>'
        )
        labels.append(
            f'<div class="slide-timeline-label {label_cls}" style="left:{x:.2f}%" '
            f'data-element-id="{body_eid(j)}" data-slide-id="{sid}">'
            f'<strong>{html.escape(head)}</strong>{html.escape(detail)}</div>'
        )
    return (
        '<div class="slide-timeline-layout">'
        f'{title_html}<div class="slide-rule"></div>'
        '<div class="slide-timeline">'
        '<div class="slide-timeline-spine"></div>'
        f'{"".join(nodes)}{"".join(labels)}'
        '</div></div>'
    )


def _html_two_by_two(
    sid: str,
    title_html: str,
    body: list[Element],
    body_eid: BodyEidFn,
) -> str:
    # Axis labels ONLY when the model authored them (6+ body lines: x, y, 4 cells).
    # Same no-false-affordances rule as the PPTX path: never invent an analytical
    # framing ("Higher impact/certainty") the quadrants don't actually plot.
    axis_x: str | None = None
    axis_y: str | None = None
    cells = body[:4]
    if len(body) >= 6:
        axis_x = _plain_element_text(body[0])
        axis_y = _plain_element_text(body[1])
        cells = body[2:6]
    cell_html: list[str] = []
    for j in range(4):
        text = _html_body_text(cells[j]) if j < len(cells) else ""
        body_pos = j + (2 if len(body) >= 6 else 0)
        cell_html.append(
            f'<div class="slide-two-by-two-cell" data-quadrant="{j + 1}" '
            f'data-element-id="{body_eid(body_pos)}" data-slide-id="{sid}">{text}</div>'
        )
    axis_html = ""
    if axis_x:
        axis_html += f'<div class="slide-two-by-two-axis x-end">{html.escape(axis_x)}</div>'
    if axis_y:
        axis_html += f'<div class="slide-two-by-two-axis y-end">{html.escape(axis_y)}</div>'
    return (
        '<div class="slide-two-by-two-layout">'
        f'{title_html}<div class="slide-rule"></div>'
        '<div class="slide-two-by-two-grid">'
        f'{axis_html}'
        f'{"".join(cell_html)}'
        '</div></div>'
    )


def _html_for_c1_slide(slide: Slide, theme: Theme, *, slide_idx: int = 0) -> str:
    """Return inner HTML for a C1 Slide by extracting its text Elements.

    ``slide_idx`` is the 0-based position in the deck; used to stamp
    ``data-element-id`` attributes so the SelectionOverlay + deckResolver can
    identify elements in the rendered HTML.
    """
    sid = f"slide-{slide_idx}"

    # C8: chart/table slides delegate to specialised HTML generators. The wrapper
    # carries ONLY data-slide-id (slide nav) — NOT a data-element-id. The whole
    # chart/table is not an editable TEXT element; stamping it `{sid}:title` made the
    # in-app editor overlay a title-edit box over the entire chart (a false affordance
    # writing /slides/N/title). Charts/tables are edited via their own tools.
    if slide.chart is not None:
        from disco.tools.builtin._c8_chart_layouts import html_chart_content
        inner = html_chart_content(slide.title, slide.chart, theme)
        return f'<div data-slide-id="{sid}">{inner}</div>'
    if slide.table is not None:
        from disco.tools.builtin._c8_chart_layouts import html_table_content
        inner = html_table_content(slide.title, slide.table, theme)
        return f'<div data-slide-id="{sid}">{inner}</div>'

    # Separate text and image elements
    texts = [el for el in slide.elements if el.kind == "text"]
    images = [el for el in slide.elements if el.kind == "image"]

    if not texts and not images:
        return ""

    # Sort text by font size desc (largest = most prominent = title)
    texts_sorted = sorted(texts, key=lambda e: e.font_size_pt, reverse=True)

    layout = slide.layout

    def _title_tag(el: Element, cls: str, tag: str = "h2") -> str:
        eid = f'{sid}:title'
        return (
            f'<{tag} class="{cls}" '
            f'data-element-id="{eid}" data-slide-id="{sid}">'
            f'{html.escape(el.text)}</{tag}>'
        )

    def _sub_tag(el: Element, cls: str) -> str:
        eid = f'{sid}:subtitle'
        return (
            f'<p class="{cls}" '
            f'data-element-id="{eid}" data-slide-id="{sid}">'
            f'{html.escape(el.text)}</p>'
        )

    # BW-13: map a rendered bullet's POSITION (its index among this slide's body
    # lines, in render order) back to its ORIGINAL authored-body index via the
    # slide's body_index_map — the SAME mapping lower_deck_for_editor uses. This
    # keeps the rendered data-element-id ("{sid}:body:{orig}") aligned 1:1 with the
    # editor pointer model for overflow / continuation / non-contiguous column spill
    # (where render position != authored index). Empty map (hand-built / MinimalDeck
    # Slide) → identity, so simple decks render byte-identically.
    body_index_map: list[int] = getattr(slide, "body_index_map", None) or []

    def _orig_bidx(render_pos: int) -> int:
        return body_index_map[render_pos] if render_pos < len(body_index_map) else render_pos

    def _body_eid(render_pos: int) -> str:
        return f'{sid}:body:{_orig_bidx(render_pos)}'

    def _bullet_li(el: Element, render_pos: int) -> str:
        return (
            f'<li data-element-id="{_body_eid(render_pos)}" data-slide-id="{sid}">'
            f'{_html_body_text(el)}</li>'
        )

    def _archetype_body_els(title_el: Element | None) -> list[Element]:
        return [
            el for el in texts
            if el is not title_el and el.text.strip()
        ]

    archetype = _slide_archetype(slide)
    archetype_title = _title_element(slide)
    archetype_body = _archetype_body_els(archetype_title)

    if archetype == "big_number":
        title_html = _title_tag(archetype_title, "slide-kicker", "p") if archetype_title else ""
        return _html_big_number(sid, title_html, archetype_title, archetype_body, _body_eid)

    if archetype == "quote":
        return _html_quote(sid, archetype_title, archetype_body, _body_eid)

    if archetype == "timeline":
        title_html = _title_tag(archetype_title, "slide-heading") if archetype_title else ""
        return _html_timeline(sid, title_html, archetype_title, archetype_body, _body_eid)

    if archetype == "two_by_two":
        title_html = _title_tag(archetype_title, "slide-heading") if archetype_title else ""
        return _html_two_by_two(sid, title_html, archetype_body, _body_eid)

    if layout == "title":
        primary = texts_sorted[0] if texts_sorted else None
        rest = texts_sorted[1:]
        title_html = _title_tag(primary, "slide-title", "h1") if primary else ""
        sub_html = "\n".join(_sub_tag(el, "slide-subtitle") for el in rest)
        return f'<div class="slide-title-bar"></div>\n{title_html}\n{sub_html}'

    if layout == "section_header":
        kicker_texts = [t for t in texts if t.font_size_pt <= 12]
        head_texts = [t for t in texts if t.font_size_pt > 12]
        kicker_sorted = sorted(kicker_texts, key=lambda e: e.font_size_pt)
        head_sorted = sorted(head_texts, key=lambda e: e.font_size_pt, reverse=True)
        kicker = f'<p class="slide-section-kicker">{html.escape(kicker_sorted[0].text)}</p>' if kicker_sorted else ""
        title = _title_tag(head_sorted[0], "slide-section-title", "h2") if head_sorted else ""
        sub = "\n".join(_sub_tag(el, "slide-subtitle") for el in head_sorted[1:])
        return f'{kicker}<div class="slide-title-bar"></div>\n{title}\n{sub}'

    if layout in ("image_right", "image_left", "full_image"):
        title_el = texts_sorted[0] if texts_sorted else None
        body_els = texts_sorted[1:]

        title_html = _title_tag(title_el, "slide-heading") if title_el else ""
        bullet_items = "".join(_bullet_li(el, j) for j, el in enumerate(body_els))
        bullets_html = f'<ul class="slide-bullets">{bullet_items}</ul>' if bullet_items else ""

        if layout == "full_image":
            # Themed art fallback (not a dead '[image]' box) — full-bleed slot.
            img_html = _art_fallback_svg(0, style="position:absolute;inset:0;width:100%;height:100%;")
            if images:
                img_src = _img_src(images[0])
                if img_src:
                    img_html = f'<img class="slide-full-image-bg" src="{img_src}" alt="">'
            return (
                '<div class="slide-full-image-wrap">'
                f'{img_html}'
                '<div class="slide-image-scrim" aria-hidden="true"></div>'
                f'<div class="slide-full-image-copy">{title_html}{bullets_html}</div>'
                '</div>'
            )

        img_html = ""
        if images:
            img_el = images[0]
            img_src = _img_src(img_el)  # C7: data-URI from bytes, else the path
            if img_src:
                img_html = f'<img src="{img_src}" alt="" style="max-width:48%;max-height:90%;object-fit:contain;">'
            else:
                # Themed art fallback (not a dead '[image]' box) — side slot.
                img_html = _art_fallback_svg(
                    2, style="width:46%;height:80%;border:1px solid var(--hairline);"
                )

        text_div = (
            f'<div style="flex:1;display:flex;flex-direction:column;">'
            f'{title_html}<div class="slide-rule"></div>{bullets_html}'
            f'</div>'
        )
        if layout == "image_left":
            return f'<div style="display:flex;gap:4%;width:100%;height:100%;align-items:center;">{img_html}{text_div}</div>'
        return f'<div style="display:flex;gap:4%;width:100%;height:100%;align-items:center;">{text_div}{img_html}</div>'

    if layout == "closing":
        primary = texts_sorted[0] if texts_sorted else None
        rest = texts_sorted[1:]
        title_html = _title_tag(primary, "slide-title", "h1") if primary else ""
        sub_html = "\n".join(_sub_tag(el, "slide-subtitle") for el in rest)
        return f'<div class="slide-title-bar"></div>\n{title_html}\n{sub_html}'

    if layout in ("two_column", "comparison"):
        title_el = next((t for t in texts_sorted if t.bold and t.font_size_pt > 20), None)
        body_els = [t for t in texts if t is not title_el]
        mid = max(1, len(body_els) // 2)
        left_els = body_els[:mid]
        right_els = body_els[mid:]
        title_html = _title_tag(title_el, "slide-heading") if title_el else ""
        left_items = "".join(_bullet_li(e, j) for j, e in enumerate(left_els))
        right_items = "".join(_bullet_li(e, j + len(left_els)) for j, e in enumerate(right_els))
        left_ul = f'<ul class="slide-bullets">{left_items}</ul>'
        right_ul = f'<ul class="slide-bullets">{right_items}</ul>'
        return (
            f'{title_html}<div class="slide-rule"></div>'
            f'<div style="display:flex;gap:4%;width:100%;">'
            f'<div style="flex:1">{left_ul}</div>'
            f'<div style="flex:1">{right_ul}</div>'
            f'</div>'
        )

    # Default: bullets
    title_el = texts_sorted[0] if texts_sorted else None
    body_els = texts_sorted[1:] if len(texts_sorted) > 1 else []
    title_html = _title_tag(title_el, "slide-heading") if title_el else ""
    bullet_items = "".join(_bullet_li(el, j) for j, el in enumerate(body_els))
    bullets_html = f'<ul class="slide-bullets">{bullet_items}</ul>' if bullet_items else ""
    return f'{title_html}\n<div class="slide-rule"></div>\n{bullets_html}'


def _html_for_slide(slide: DeckSlide, theme: Theme) -> str:
    """Return the inner HTML markup for one slide, keyed on layout."""
    title_esc = html.escape(slide.title)

    if slide.layout == "title":
        subtitle = ""
        if slide.bullets:
            subtitle = (
                f'<p class="slide-subtitle">{html.escape(slide.bullets[0])}</p>'
            )
        return (
            f'<div class="slide-title-bar"></div>\n'
            f'<h1 class="slide-title">{title_esc}</h1>\n'
            f'{subtitle}'
        )

    if slide.layout == "section":
        subtitle = ""
        if slide.bullets:
            subtitle = (
                f'<p class="slide-subtitle" style="margin-top:1.5%">'
                f'{html.escape(slide.bullets[0])}</p>'
            )
        return (
            f'<p class="slide-section-kicker">Section</p>\n'
            f'<div class="slide-title-bar"></div>\n'
            f'<h2 class="slide-section-title">{title_esc}</h2>\n'
            f'{subtitle}'
        )

    if slide.layout == "image_right":
        bullets_html = _bullets_html(slide.bullets)
        img_html = ""
        if slide.image_url:
            img_html = (
                f'<img src="{html.escape(slide.image_url)}" '
                f'alt="" style="max-width:48%;max-height:90%;object-fit:contain;">'
            )
        else:
            # Themed art fallback (not a dead '[image]' box) — side slot.
            img_html = _art_fallback_svg(1, style="width:46%;height:80%;border:1px solid var(--hairline);")
        return (
            f'<div style="display:flex;gap:4%;width:100%;height:100%;'
            f'align-items:center;">'
            f'<div style="flex:1;display:flex;flex-direction:column;">'
            f'<h2 class="slide-heading">{title_esc}</h2>'
            f'<div class="slide-rule"></div>'
            f'{bullets_html}</div>'
            f'{img_html}</div>'
        )

    # Default: bullets layout
    bullets_html = _bullets_html(slide.bullets)
    return (
        f'<h2 class="slide-heading">{title_esc}</h2>\n'
        f'<div class="slide-rule"></div>\n'
        f'{bullets_html}'
    )


def _bullets_html(bullets: list[str]) -> str:
    """Render a bullet list as HTML; returns empty string when no bullets."""
    if not bullets:
        return ""
    items = "".join(f"<li>{html.escape(b)}</li>" for b in bullets)
    return f'<ul class="slide-bullets">{items}</ul>'


# ---------------------------------------------------------------------------
# convert_to_pdf — LibreOffice headless (sandbox-jailed)
# ---------------------------------------------------------------------------

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
            "deploy/sandbox/Dockerfile to enable it."
        )

    # 2. Convert (--outdir . → PDF lands in cwd = workspace)
    cmd = (
        "soffice --headless --convert-to pdf --outdir . "
        + shlex.quote(pptx_name)
    )
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
