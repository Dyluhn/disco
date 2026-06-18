"""C3 — Native editable PPTX renderer.

Renders a MinimalDeck to:
  - .pptx  via python-pptx (real text boxes, brand fonts/colors — NOT image-per-slide)
  - 16:9 brand HTML (self-contained, OFL font-face via file:// abs URLs)
  - PDF   via LibreOffice headless inside the sandbox (gated on soffice present;
          graceful failure when absent — no false affordance)

MinimalDeck is a minimal typed deck representation used until the full C1
AuthoredDeck/Deck schema is frozen by the C4 experiment verdict.  When C1
ships, the render_pptx/render_html entry-points will accept the full Deck
instead; the per-layout helpers are stable and do not need to change.

# NOTE: MinimalDeck will align to the C1 Layer-2 Deck after the C4 verdict.
# The layout names and element positions here are intentionally conservative
# (centered/padded, 0.5in margins) and will be refined once the C4 overflow
# scorer + C1 _fit_text measurements are available.

Layering: disco.tools → disco.core (legal downward import).
          disco.tools ≠→ disco.agent_server (upward import — illegal, not done here).
"""

from __future__ import annotations

import html
import importlib.resources
import io
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from disco.core.brand import resolve_theme
from disco.core.brand.tokens import Theme

# C8 chart/table layout helpers (must come after core imports; no cycle — these
# modules don't import _pptx_render).
from disco.tools.builtin._c8_chart_layouts import (  # noqa: E402
    ChartSpec,
    TableSpec,
    html_chart_content,
    html_table_content,
    layout_chart_slide_pptx,
    layout_table_slide_pptx,
)

if TYPE_CHECKING:
    from disco.tools.anatomy import ToolContext

# ---------------------------------------------------------------------------
# Minimal deck representation
# (pending C1 AuthoredDeck/Deck — will align after C4 experiment verdict)
# ---------------------------------------------------------------------------

LayoutHint = Literal["title", "bullets", "section", "image_right", "chart", "table"]


@dataclass
class DeckSlide:
    """One slide in the minimal deck.  Maps to a single slide in the PPTX/HTML."""

    title: str
    bullets: list[str] = field(default_factory=list)
    layout: LayoutHint = "bullets"
    # image_url: only used in image_right layout; stub for C7 image-gen wiring
    image_url: str | None = None
    notes: str | None = None
    # C8: structured chart/table data — set layout="chart" or layout="table"
    chart: ChartSpec | None = None
    table: TableSpec | None = None


@dataclass
class MinimalDeck:
    """Minimal typed deck, pending C1 full schema.

    theme_name + theme_mode resolve via core.brand.resolve_theme(name, mode).
    Splitting "disco-light" → ("disco", "light") is the caller's job (two-arg
    signature, per C1 spec note).
    """

    title: str
    slides: list[DeckSlide] = field(default_factory=list)
    theme_name: str = "disco"
    theme_mode: str = "light"


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
# Layout dispatch
# ---------------------------------------------------------------------------

_LAYOUT_FNS = {
    "title": _layout_title_slide,
    "bullets": _layout_bullets_slide,
    "section": _layout_section_slide,
    "image_right": _layout_image_right_slide,
    # C8 chart/table layouts
    "chart": layout_chart_slide_pptx,
    "table": layout_table_slide_pptx,
}


# ---------------------------------------------------------------------------
# render_pptx — main PPTX entry point
# ---------------------------------------------------------------------------

def render_pptx(deck: MinimalDeck) -> bytes:
    """Render *deck* to native editable .pptx bytes (python-pptx, real text boxes).

    Uses the blank slide layout (index 6) so no placeholder frames conflict with
    the absolutely-positioned textboxes.  Returns raw bytes suitable for
    sandbox.write_file(name, bytes).  NEVER call .encode() on the result —
    it's already binary.
    """
    from pptx import Presentation
    from pptx.util import Emu

    theme = resolve_theme(deck.theme_name, deck.theme_mode)

    prs = Presentation()
    prs.slide_width = Emu(_SLIDE_W)
    prs.slide_height = Emu(_SLIDE_H)

    # Blank layout (index 6) — no competing placeholder frames
    blank_layout = prs.slide_layouts[6]

    for slide_data in deck.slides:
        prs_slide = prs.slides.add_slide(blank_layout)
        layout_fn = _LAYOUT_FNS.get(slide_data.layout, _layout_bullets_slide)
        layout_fn(prs_slide, slide_data, theme)

    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# render_html — 16:9 brand HTML
# ---------------------------------------------------------------------------

def render_html(deck: MinimalDeck) -> str:  # noqa: C901
    """Render *deck* to a self-contained 16:9 brand HTML string.

    One ``<section class="slide">`` per slide.  Keyboard navigation (←/→).
    OFL @font-face declarations via file:// abs URLs (same as font_face_css()
    in core.brand.css).  Brand CSS vars from theme_css_vars().
    """
    from disco.core.brand.css import font_face_css, theme_css_vars

    theme = resolve_theme(deck.theme_name, deck.theme_mode)
    font_css = font_face_css()
    vars_css = theme_css_vars(theme)

    # Determine accent-contrast text for section slides
    # (surface_1 background → still use theme.text)

    sections_html: list[str] = []
    for i, slide in enumerate(deck.slides):
        active = ' active' if i == 0 else ''
        bg = theme.surface_1 if slide.layout == "section" else theme.bg

        # Build inner content based on layout
        inner = _html_for_slide(slide, theme)
        sections_html.append(
            f'<section class="slide{active}" id="slide-{i}" '
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
/* C8 chart/table slide styles */
.slide-chart{{flex:1;display:flex;align-items:center;justify-content:center;
  overflow:hidden;margin-top:1%;}}
.slide-chart svg{{max-width:100%;max-height:75%;}}
.chart-table{{font-family:var(--ui);font-size:1.1vw;border-collapse:collapse;
  width:100%;margin-top:1%;}}
.chart-table caption{{font-weight:700;margin-bottom:0.4%;color:var(--text);}}
.chart-table th{{background:var(--accent);color:#fff;padding:0.3em 0.7em;
  text-align:left;}}
.chart-table td{{padding:0.25em 0.7em;border-bottom:1px solid var(--hairline);
  color:var(--text);}}
.chart-fallback{{font-family:var(--ui);font-size:1.1vw;color:var(--text-muted);
  margin-top:2%;}}
.slide-table-wrap{{flex:1;overflow:auto;margin-top:1%;}}
.slide-table{{width:100%;border-collapse:collapse;font-family:var(--ui);
  font-size:1.2vw;}}
.slide-table th{{background:var(--accent);color:#fff;padding:0.4em 0.8em;
  text-align:left;font-weight:700;}}
.slide-table td{{padding:0.3em 0.8em;border-bottom:1px solid var(--hairline);
  color:var(--text);}}
.slide-table tr:nth-child(even) td{{background:var(--surface-1);}}
.slide-table-empty{{font-family:var(--ui);font-size:1.2vw;
  color:var(--text-muted);margin-top:2%;}}
/* nav controls */
.nav{{position:fixed;bottom:1%;right:1%;display:flex;gap:.5em;z-index:10;}}
.nav button{{background:var(--surface-2);border:1px solid var(--hairline);
  color:var(--text-muted);font-family:var(--ui);font-size:.9em;
  padding:.3em .8em;border-radius:4px;cursor:pointer;}}
.nav button:hover{{background:var(--surface-1);}}
.slide-counter{{position:fixed;bottom:1%;left:1%;font-family:var(--ui);
  font-size:.8em;color:var(--text-faint);}}
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

    # C8 — chart layout: embed SVG from render_chart_svg (fallback: table)
    if slide.layout == "chart":
        if slide.chart is not None:
            return html_chart_content(slide.title, slide.chart, theme)
        # chart field missing but layout="chart" — degrade to bullets
        return (
            f'<h2 class="slide-heading">{title_esc}</h2>\n'
            f'<div class="slide-rule"></div>\n'
            f'<p class="chart-fallback">[chart data unavailable]</p>'
        )

    # C8 — table layout: native HTML table from TableSpec
    if slide.layout == "table":
        if slide.table is not None:
            return html_table_content(slide.title, slide.table, theme)
        return (
            f'<h2 class="slide-heading">{title_esc}</h2>\n'
            f'<div class="slide-rule"></div>\n'
            f'<p class="slide-table-empty">[table data unavailable]</p>'
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

def render_deck(deck: MinimalDeck) -> dict[str, bytes | str]:
    """Render *deck* to pptx_bytes + html_str.

    PDF requires a sandbox context; call ``convert_to_pdf(ctx, pptx_name)``
    separately after writing the .pptx to the workspace.

    Returns:
        ``{"pptx_bytes": bytes, "html_str": str}``
    """
    return {
        "pptx_bytes": render_pptx(deck),
        "html_str": render_html(deck),
    }
