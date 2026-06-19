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
from typing import TYPE_CHECKING, Literal

from disco.core.brand import resolve_theme
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
    chart: "ChartSpec | None" = None   # C8: chart data → routes to c8 chart layout
    table: "TableSpec | None" = None   # C8: table data → routes to c8 table layout


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
    """Add a textbox at absolute EMU coordinates and return its text_frame."""
    from pptx.util import Emu
    shape = slide.shapes.add_textbox(Emu(left), Emu(top), Emu(w), Emu(h))
    tf = shape.text_frame
    tf.word_wrap = True
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
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    # Use add_shape with RECTANGLE (auto-shape type 1)
    bar = slide.shapes.add_shape(1, Emu(left), Emu(top), Emu(w), Emu(91_440))
    bar.fill.solid()
    bar.fill.fore_color.rgb = _rgb_from_hex(hex_color)
    bar.line.fill.background()   # no border line


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
    from pptx.util import Emu, Pt

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
    from pptx.util import Emu, Pt

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
    from pptx.util import Emu, Pt

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
    from pptx.util import Emu, Pt

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
    """Render a styled image-placeholder box (used when image_url is None)."""
    from pptx.util import Emu

    box = prs_slide.shapes.add_shape(1, Emu(left), Emu(top), Emu(w), Emu(h))
    box.fill.solid()
    box.fill.fore_color.rgb = _rgb_from_hex(theme.surface_2)
    box.line.color.rgb = _rgb_from_hex(theme.hairline)
    tf = box.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    from pptx.enum.text import PP_ALIGN
    p.alignment = PP_ALIGN.CENTER
    run = p.add_run()
    _set_run_style(run, "[image]", _first_font(theme.font_ui), 14, theme.text_faint)


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

def _render_element(prs_slide, el: Element, theme: Theme) -> None:  # type: ignore[type-arg]
    """Render one C1 Element to a python-pptx slide shape."""
    from pptx.util import Emu

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
    from pptx.util import Pt

    _ALIGN_MAP = {"LEFT": PP_ALIGN.LEFT, "CENTER": PP_ALIGN.CENTER, "RIGHT": PP_ALIGN.RIGHT}
    tf = _add_textbox(
        prs_slide,
        int(el.left), int(el.top), int(el.width), int(el.height),
    )
    tf.word_wrap = el.word_wrap
    p = tf.paragraphs[0]
    p.alignment = _ALIGN_MAP.get(el.align, PP_ALIGN.LEFT)
    run = p.add_run()
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
    """Render an image Element; falls back to styled placeholder when no image_path."""
    from pptx.util import Emu

    if el.image_path:
        try:
            prs_slide.shapes.add_picture(
                el.image_path,
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


# ---------------------------------------------------------------------------
# render_pptx — accepts Deck (C1) or MinimalDeck (compat)
# ---------------------------------------------------------------------------

def render_pptx(deck: "Deck | MinimalDeck") -> bytes:
    """Render *deck* to native editable .pptx bytes (python-pptx, real text boxes).

    Accepts either a C1 ``Deck`` (from ``_deck_schema.lower_deck``) or a
    ``MinimalDeck`` (backward-compat shim).  Returns raw bytes suitable for
    ``sandbox.write_file(name, bytes)``.  NEVER call ``.encode()`` on the
    result — it is already binary.
    """
    from pptx import Presentation
    from pptx.util import Emu

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
            for el in slide.elements:
                _render_element(prs_slide, el, deck.theme)

            if slide.notes:
                _ntf = prs_slide.notes_slide.notes_text_frame
                if _ntf is not None:
                    _ntf.text = slide.notes

    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# render_html — 16:9 brand HTML (accepts Deck or MinimalDeck)
# ---------------------------------------------------------------------------

def render_html(deck: "Deck | MinimalDeck") -> str:  # noqa: C901
    """Render *deck* to a self-contained 16:9 brand HTML string.

    Accepts a C1 ``Deck`` or a ``MinimalDeck`` (backward-compat shim).
    One ``<section class="slide">`` per slide.  Keyboard navigation (←/→).
    """
    if isinstance(deck, MinimalDeck):
        authored = _minimal_to_authored(deck)
        c1_deck = lower_deck(authored)
        return _render_html_c1(c1_deck)
    return _render_html_c1(deck)


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

        inner = _html_for_c1_slide(slide, theme, slide_idx=i)
        sections_html.append(
            f'<section class="slide{active}" id="slide-{i}" '
            f'data-slide-id="slide-{i}" '
            f'data-layout="{html.escape(slide.layout)}" '
            f'style="background:{html.escape(bg)}">\n{inner}\n</section>'
        )

    slides_joined = "\n".join(sections_html)

    return f"""\
<!DOCTYPE html>
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
    def _clean_tag(m: "re.Match[str]") -> str:
        tag = _STRIP_ELEMENT_ID_RE.sub("", m.group(0))
        return _STRIP_SLIDE_ID_RE.sub("", tag)

    return _TAG_RE.sub(_clean_tag, html_str)


def _html_for_c1_slide(slide: Slide, theme: Theme, *, slide_idx: int = 0) -> str:
    """Return inner HTML for a C1 Slide by extracting its text Elements.

    ``slide_idx`` is the 0-based position in the deck; used to stamp
    ``data-element-id`` attributes so the SelectionOverlay + deckResolver can
    identify elements in the rendered HTML.
    """
    sid = f"slide-{slide_idx}"

    # C8: chart/table slides delegate to specialised HTML generators
    if slide.chart is not None:
        from disco.tools.builtin._c8_chart_layouts import html_chart_content
        inner = html_chart_content(slide.title, slide.chart, theme)
        return f'<div data-element-id="{sid}:title" data-slide-id="{sid}">{inner}</div>'
    if slide.table is not None:
        from disco.tools.builtin._c8_chart_layouts import html_table_content
        inner = html_table_content(slide.title, slide.table, theme)
        return f'<div data-element-id="{sid}:title" data-slide-id="{sid}">{inner}</div>'

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

    def _bullet_li(el: Element, bidx: int) -> str:
        eid = f'{sid}:body:{bidx}'
        return (
            f'<li data-element-id="{eid}" data-slide-id="{sid}">'
            f'{html.escape(el.text.lstrip("• "))}</li>'
        )

    if layout == "title":
        primary = texts_sorted[0] if texts_sorted else None
        rest = texts_sorted[1:]
        title_html = _title_tag(primary, "slide-title", "h1") if primary else ""
        sub_html = _sub_tag(rest[0], "slide-subtitle") if rest else ""
        return f'<div class="slide-title-bar"></div>\n{title_html}\n{sub_html}'

    if layout == "section_header":
        kicker_texts = [t for t in texts if t.font_size_pt <= 12]
        head_texts = [t for t in texts if t.font_size_pt > 12]
        kicker_sorted = sorted(kicker_texts, key=lambda e: e.font_size_pt)
        head_sorted = sorted(head_texts, key=lambda e: e.font_size_pt, reverse=True)
        kicker = f'<p class="slide-section-kicker">{html.escape(kicker_sorted[0].text)}</p>' if kicker_sorted else ""
        title = _title_tag(head_sorted[0], "slide-section-title", "h2") if head_sorted else ""
        sub = _sub_tag(head_sorted[1], "slide-subtitle") if len(head_sorted) > 1 else ""
        return f'{kicker}<div class="slide-title-bar"></div>\n{title}\n{sub}'

    if layout in ("image_right", "image_left", "full_image"):
        title_el = texts_sorted[0] if texts_sorted else None
        body_els = texts_sorted[1:]

        title_html = _title_tag(title_el, "slide-heading") if title_el else ""
        bullet_items = "".join(_bullet_li(el, j) for j, el in enumerate(body_els))
        bullets_html = f'<ul class="slide-bullets">{bullet_items}</ul>' if bullet_items else ""

        img_html = ""
        if images:
            img_el = images[0]
            if img_el.image_path:
                img_html = f'<img src="{html.escape(img_el.image_path)}" alt="" style="max-width:48%;max-height:90%;object-fit:contain;">'
            else:
                img_html = (
                    '<div style="width:46%;height:80%;background:var(--surface-2);'
                    'border:1px solid var(--hairline);display:flex;align-items:center;'
                    'justify-content:center;color:var(--text-faint);'
                    'font-family:var(--ui);font-size:1.2vw;">[image]</div>'
                )

        if layout == "full_image":
            return (
                f'<div style="position:relative;width:100%;height:100%;">'
                f'{img_html}'
                f'<div style="position:absolute;bottom:5%;left:5%;">{title_html}</div>'
                f'</div>'
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
        sub_html = _sub_tag(rest[0], "slide-subtitle") if rest else ""
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
            img_html = (
                f'<div style="width:46%;height:80%;background:var(--surface-2);'
                f'border:1px solid var(--hairline);display:flex;align-items:center;'
                f'justify-content:center;color:var(--text-faint);'
                f'font-family:var(--ui);font-size:1.2vw;">[image]</div>'
            )
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

async def convert_to_pdf(ctx: "ToolContext", pptx_name: str) -> tuple[bool, str]:
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

def render_deck(deck: "Deck | MinimalDeck") -> dict[str, bytes | str]:
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
