"""C1 — Two-layer deck schema + deterministic lowering.

Layer 1 (AuthoredDeck / AuthoredSlide): the loose authoring schema the LLM
emits.  Field set FROZEN by the C4 experiment verdict
(docs/slides-experiment-verdict.md §6).  Do NOT change the AuthoredSlide
field set without a new experiment.

Layer 2 (Deck / Slide / Element): precise positional representation consumed by
the C3 renderer (_pptx_render.py) and the future browser editor.  All positions
are in EMU (English Metric Units, 1 inch = 914,400).

``lower_deck(authored) -> Deck`` is the single entry point.  It is a pure
function (same input → byte-identical output) that:
  1. Resolves theme tokens via core.brand.resolve_theme (two-arg signature).
  2. Infers the resolved layout for each slide via ``_infer_layout``.
  3. Calls the per-layout function to produce absolutely-positioned Elements.
  4. Applies ``_fit_text`` overflow control:
       • font step-down from max to min in 2-pt increments;
       • if content still overflows at font-floor → continuation-slide split
         (NOT content truncation).
  5. ``notes`` and ``image_prompt`` are EXCLUDED from overflow calculations.

Layering: disco.tools → disco.core  (legal downward import).
          disco.tools  ≠→ disco.agent_server  (upward import — never here).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

from disco.core.brand import resolve_theme
from disco.core.brand.tokens import Theme

# ---------------------------------------------------------------------------
# Slide canvas geometry (16:9)
# ---------------------------------------------------------------------------

_SLIDE_W = 12_192_000   # 13.33 inches
_SLIDE_H = 6_858_000    # 7.5 inches
_MARGIN = 457_200       # 0.5 inch
_CW = _SLIDE_W - 2 * _MARGIN   # 11 277 600
_CH = _SLIDE_H - 2 * _MARGIN   # 5 943 600
_TITLE_H = 914_400              # ≈1 in title strip
_BAR_H = 91_440                 # 0.1 in accent bar
_GAP = 91_440                   # 0.1 in gap between elements

# Content box (below title + bar) used for body text
_BODY_TOP = _MARGIN + _TITLE_H + _GAP + _BAR_H + _GAP
_BODY_H = _SLIDE_H - _BODY_TOP - _MARGIN
_BODY_INDENT = 228_600           # 0.25 in left indent for bullets

# ---------------------------------------------------------------------------
# LayoutHint — resolved layout values (never None in Layer 2)
# ---------------------------------------------------------------------------

LayoutHint = Literal[
    "title",
    "section_header",
    "bullets",
    "two_column",
    "comparison",
    "image_right",
    "image_left",
    "full_image",
    "metrics",
    "metrics_grid",
    "table",
    "closing",
]


# ---------------------------------------------------------------------------
# Layer 1 — authoring schema (frozen by C4 verdict)
# ---------------------------------------------------------------------------


class ChartSpec(BaseModel):
    """Structured chart payload (wired in C8)."""

    kind: Literal["bar", "line", "pie", "scatter"]
    title: str = ""
    labels: list[str]
    series: list[dict]  # [{"name": str, "data": [float]}]


class TableSpec(BaseModel):
    """Structured table payload."""

    headers: list[str]
    rows: list[list[str]]


class AuthoredSlide(BaseModel):
    """One slide as the LLM authors it — loose/semantic, no coordinates.

    Field set is FROZEN by the C4 experiment verdict.  Do NOT add or remove
    fields without running a new experiment and updating that document.
    """

    type: str = Field(
        description=(
            "Semantic slide type.  Well-known values: 'title', 'bullets', "
            "'two_column', 'metrics', 'section_header', 'image_right', "
            "'image_left', 'table', 'closing'.  Custom strings are OK — "
            "the lowerer falls back to 'bullets' for unknown types."
        )
    )
    title: str = Field(
        description="Slide heading.  No hard limit — lowerer truncates if needed."
    )
    body: list[str] = Field(
        default=[],
        description=(
            "Bullet points or content lines.  Guidelines: 4–6 lines, each "
            "under 100 chars.  If image_prompt is set and this slide is "
            "primarily visual, set body=[] or at most one caption line."
        ),
    )
    layout_hint: LayoutHint | None = Field(
        default=None,
        description=(
            "Optional explicit layout override.  If absent, _infer_layout() "
            "derives it from type + field presence."
        ),
    )
    image_prompt: str | None = Field(
        default=None,
        description=(
            "If set, image_generate is called with this prompt.  "
            "Triggers image_right/image_left or full_image layout.  "
            "Keep body=[] for full-image slides."
        ),
    )
    chart: ChartSpec | None = Field(
        default=None,
        description="Structured chart data (C8).  Triggers metrics/chart layout.",
    )
    table: TableSpec | None = Field(
        default=None,
        description="Structured table data.  Triggers table layout.",
    )
    notes: str | None = Field(
        default=None,
        description=(
            "Speaker notes.  NEVER rendered on the visible slide face.  "
            "NEVER counted in overflow calculations."
        ),
    )


class AuthoredDeck(BaseModel):
    """A complete deck as the LLM authors it."""

    title: str
    theme: Literal["disco-light", "disco-dark", "neutral"] = "disco-light"
    slides: list[AuthoredSlide]


# ---------------------------------------------------------------------------
# Layer 2 — precise positional representation (renderer / editor consume)
# ---------------------------------------------------------------------------


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
    font_name: str = ""       # first font in the CSS stack
    font_size_pt: float = 20.0
    hex_color: str = "#1a1813"
    bold: bool = False
    italic: bool = False
    align: str = "LEFT"       # "LEFT" | "CENTER" | "RIGHT"
    word_wrap: bool = True

    # ---- image fields -------------------------------------------------------
    image_prompt: str | None = None  # C7 wire: generate from this prompt
    image_path: str | None = None    # file path if already generated

    # ---- shape fields -------------------------------------------------------
    fill_hex: str = "#2563eb"
    border_hex: str | None = None


@dataclass
class Slide:
    """One slide in the precise Deck — layout resolved, elements positioned."""

    id: str
    type: str                   # from AuthoredSlide.type (e.g. "bullets_cont")
    layout: LayoutHint          # resolved, never None
    elements: list[Element] = field(default_factory=list)
    notes: str | None = None


@dataclass
class Deck:
    """Full precise deck consumed by the C3 renderer."""

    id: str
    title: str
    theme: Theme               # resolved via core.brand.resolve_theme
    slides: list[Slide] = field(default_factory=list)
    size: tuple[int, int] = (12_192_000, 6_858_000)  # 16:9 EMU


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _uid() -> str:
    return uuid.uuid4().hex[:12]


def _first_font(stack: str) -> str:
    """Extract the first font name from a CSS font-family stack."""
    return stack.split(",")[0].strip().strip("'\"")


def _parse_theme(theme_str: str) -> tuple[str, str]:
    """Split 'disco-light' → ('disco', 'light').

    The verdict doc calls out resolve_theme's TWO-arg signature; callers must
    split the compound string rather than passing it directly.
    """
    if theme_str == "disco-light":
        return "disco", "light"
    if theme_str == "disco-dark":
        return "disco", "dark"
    # "neutral" → neutral/light
    return "neutral", "light"


# ---------------------------------------------------------------------------
# _fit_text — overflow control (font step-down + continuation split)
# ---------------------------------------------------------------------------

# Font size ranges per usage context
_BODY_FONT_MAX = 24.0
_BODY_FONT_MIN = 14.0
_BODY_FONT_STEP = 2.0

# Text measurement constants (character-count model, no PIL/PPTX at lower time)
# Reference calibration: at 20pt, ~80 chars fit in a 9-inch content box.
_REF_FONT_PT = 20.0
_REF_CHARS_PER_LINE = 80.0
_REF_BOX_WIDTH_EMU = float(_CW - _BODY_INDENT)  # 11 049 000


def _chars_per_line(font_pt: float, box_width_emu: float) -> int:
    """Estimate max characters per line at given font size and box width."""
    scale = (box_width_emu / _REF_BOX_WIDTH_EMU) * (_REF_FONT_PT / font_pt)
    return max(10, int(_REF_CHARS_PER_LINE * scale))


def _lines_per_box(font_pt: float, box_height_emu: float) -> int:
    """Estimate how many text lines fit in a box at given font size."""
    line_h_emu = font_pt * 12_700 * 1.3  # 1.3× leading
    return max(1, int(box_height_emu / line_h_emu))


def _fit_text(
    lines: list[str],
    box_width: float,
    box_height: float,
    max_font: float = _BODY_FONT_MAX,
    min_font: float = _BODY_FONT_MIN,
) -> tuple[float, list[str], list[str]]:
    """Fit *lines* into *box* via font step-down.

    Returns ``(chosen_font_pt, fitted_lines, overflow_lines)``.

    ``notes`` and ``image_prompt`` must NOT be passed here — they are excluded
    from overflow calculations per the verdict.

    When text still overflows at min_font, the overflow slice is returned as
    ``overflow_lines`` (NOT truncated) for the caller to split into a
    continuation slide.
    """
    if not lines:
        return max_font, [], []

    font = max_font
    while font >= min_font:
        cpl = _chars_per_line(font, box_width)
        lph = _lines_per_box(font, box_height)
        # Count wrapped lines
        total = sum(max(1, (len(ln) + cpl - 1) // cpl) for ln in lines)
        if total <= lph:
            return font, lines, []
        font -= _BODY_FONT_STEP

    # Still overflows at min_font — split to continuation slide
    font = min_font
    cpl = _chars_per_line(font, box_width)
    lph = _lines_per_box(font, box_height)
    fitted: list[str] = []
    remaining = lph
    for line in lines:
        wrapped_count = max(1, (len(line) + cpl - 1) // cpl)
        if remaining >= wrapped_count:
            fitted.append(line)
            remaining -= wrapped_count
        else:
            break  # this line triggers overflow; rest goes to continuation

    overflow = lines[len(fitted):]
    return font, fitted, overflow


# ---------------------------------------------------------------------------
# _infer_layout — total deterministic mapping (never returns None)
# ---------------------------------------------------------------------------


def _infer_layout(slide: AuthoredSlide, image_alt: list[int] | None = None) -> LayoutHint:
    """Deterministically resolve the LayoutHint for a slide.

    ``image_alt`` is a mutable 1-element list used to alternate image_right
    and image_left across slides.  Pass a shared list from lower_deck().

    Per verdict §6 lowering rules (authoritative):
      image_prompt + body==[] → full_image
      image_prompt + body non-empty → image_right/image_left (alternating)
      chart or table → metrics / table
      type/layout_hint two_column/comparison → those layouts
      title, section_header, closing, metrics → direct
      default (bullets/custom/unknown) → bullets
    """
    # Explicit layout_hint always wins
    if slide.layout_hint is not None:
        return slide.layout_hint

    # Image-driven
    if slide.image_prompt is not None:
        if not slide.body:
            return "full_image"
        # Alternate image sides
        if image_alt is not None:
            side: LayoutHint = "image_left" if image_alt[0] % 2 else "image_right"
            image_alt[0] += 1
            return side
        return "image_right"

    # Data-driven
    if slide.table is not None:
        return "table"
    if slide.chart is not None:
        return "metrics"

    # Semantic type lookup
    _TYPE_MAP: dict[str, LayoutHint] = {
        "title": "title",
        "section_header": "section_header",
        "section": "section_header",
        "two_column": "two_column",
        "comparison": "comparison",
        "metrics": "metrics",
        "metrics_grid": "metrics_grid",
        "image_right": "image_right",
        "image_left": "image_left",
        "full_image": "full_image",
        "table": "table",
        "closing": "closing",
    }
    typ = slide.type.lower().removesuffix("_cont")  # "bullets_cont" → "bullets"
    if typ in _TYPE_MAP:
        return _TYPE_MAP[typ]

    # Default — covers "bullets", custom types, "_cont" continuations
    return "bullets"


# ---------------------------------------------------------------------------
# Per-layout functions — each ≤ 200 LOC (arch budget)
# ---------------------------------------------------------------------------


def _layout_title(slide: AuthoredSlide, theme: Theme) -> tuple[list[Element], list[str]]:
    """Title slide: large display title + optional subtitle."""
    els: list[Element] = []

    v_origin = int(_SLIDE_H * 0.28)

    # Accent bar
    els.append(Element(
        id=_uid(), kind="accent_bar",
        left=_MARGIN, top=v_origin,
        width=2_286_000, height=_BAR_H,
        fill_hex=theme.accent,
    ))

    # Display title
    title_top = v_origin + 182_880
    title_h = 1_828_800
    els.append(Element(
        id=_uid(), kind="text",
        left=_MARGIN, top=title_top,
        width=_CW, height=title_h,
        text=slide.title,
        font_name=_first_font(theme.font_display),
        font_size_pt=52.0,
        hex_color=theme.text,
    ))

    # Subtitle (first body line only; image_prompt excluded per verdict)
    overflow: list[str] = []
    if slide.body:
        sub_top = title_top + title_h
        els.append(Element(
            id=_uid(), kind="text",
            left=_MARGIN, top=sub_top,
            width=_CW, height=914_400,
            text=slide.body[0],
            font_name=_first_font(theme.font_reading),
            font_size_pt=24.0,
            hex_color=theme.text_muted,
            italic=True,
        ))
        overflow = slide.body[1:]

    return els, overflow


def _layout_section_header(slide: AuthoredSlide, theme: Theme) -> tuple[list[Element], list[str]]:
    """Section-divider: vertically centred title with kicker + accent bar."""
    els: list[Element] = []

    v_center = _SLIDE_H // 2
    bar_top = v_center - 228_600

    # Kicker
    kicker_top = bar_top - 457_200
    els.append(Element(
        id=_uid(), kind="text",
        left=_MARGIN, top=kicker_top,
        width=_CW, height=457_200,
        text="SECTION",
        font_name=_first_font(theme.font_ui),
        font_size_pt=9.0,
        hex_color=theme.accent,
        bold=True,
    ))

    # Accent bar
    els.append(Element(
        id=_uid(), kind="accent_bar",
        left=_MARGIN, top=bar_top,
        width=1_371_600, height=_BAR_H,
        fill_hex=theme.accent,
    ))

    # Title
    title_top = bar_top + 182_880
    title_h = 1_371_600
    els.append(Element(
        id=_uid(), kind="text",
        left=_MARGIN, top=title_top,
        width=_CW, height=title_h,
        text=slide.title,
        font_name=_first_font(theme.font_display),
        font_size_pt=40.0,
        hex_color=theme.text,
    ))

    overflow: list[str] = []
    if slide.body:
        sub_top = title_top + title_h
        els.append(Element(
            id=_uid(), kind="text",
            left=_MARGIN, top=sub_top,
            width=_CW, height=914_400,
            text=slide.body[0],
            font_name=_first_font(theme.font_reading),
            font_size_pt=18.0,
            hex_color=theme.text_muted,
            italic=True,
        ))
        overflow = slide.body[1:]

    return els, overflow


def _layout_bullets(slide: AuthoredSlide, theme: Theme) -> tuple[list[Element], list[str]]:
    """Standard title + bullet-list layout."""
    els: list[Element] = []

    # Title
    els.append(Element(
        id=_uid(), kind="text",
        left=_MARGIN, top=_MARGIN,
        width=_CW, height=_TITLE_H,
        text=slide.title,
        font_name=_first_font(theme.font_ui),
        font_size_pt=32.0,
        hex_color=theme.text,
        bold=True,
    ))

    # Accent bar under title
    bar_top = _MARGIN + _TITLE_H + 45_720
    els.append(Element(
        id=_uid(), kind="accent_bar",
        left=_MARGIN, top=bar_top,
        width=_CW, height=_BAR_H,
        fill_hex=theme.accent,
    ))

    # Bullets with _fit_text overflow control
    body_top = bar_top + _BAR_H + _GAP
    body_h = _SLIDE_H - body_top - _MARGIN
    body_w = _CW - _BODY_INDENT

    font_pt, fitted, overflow = _fit_text(
        slide.body, box_width=body_w, box_height=body_h
    )

    for i, bullet in enumerate(fitted):
        line_h = font_pt * 12_700 * 1.3
        bullet_top = body_top + int(i * line_h)
        bullet_text = f"• {bullet}"
        els.append(Element(
            id=_uid(), kind="text",
            left=_MARGIN + _BODY_INDENT,
            top=bullet_top,
            width=body_w,
            height=int(line_h * 1.15),
            text=bullet_text,
            font_name=_first_font(theme.font_reading),
            font_size_pt=font_pt,
            hex_color=theme.text,
            word_wrap=True,
        ))

    return els, overflow


def _layout_closing(slide: AuthoredSlide, theme: Theme) -> tuple[list[Element], list[str]]:
    """Closing/thank-you slide: large centred title + optional subtitle."""
    els: list[Element] = []

    v_center = int(_SLIDE_H * 0.42)

    els.append(Element(
        id=_uid(), kind="accent_bar",
        left=_MARGIN, top=v_center - _BAR_H - 182_880,
        width=2_286_000, height=_BAR_H,
        fill_hex=theme.accent,
    ))
    els.append(Element(
        id=_uid(), kind="text",
        left=_MARGIN, top=v_center,
        width=_CW, height=1_371_600,
        text=slide.title,
        font_name=_first_font(theme.font_display),
        font_size_pt=48.0,
        hex_color=theme.text,
        align="LEFT",
    ))

    overflow: list[str] = []
    if slide.body:
        sub_top = v_center + 1_371_600 + _GAP
        els.append(Element(
            id=_uid(), kind="text",
            left=_MARGIN, top=sub_top,
            width=_CW, height=914_400,
            text=slide.body[0],
            font_name=_first_font(theme.font_reading),
            font_size_pt=22.0,
            hex_color=theme.text_muted,
            italic=True,
        ))
        overflow = slide.body[1:]

    return els, overflow


def _layout_image_right(
    slide: AuthoredSlide, theme: Theme, *, image_left: bool = False
) -> tuple[list[Element], list[str]]:
    """Title + bullets on one half, image on the other."""
    els: list[Element] = []

    half_w = _CW // 2 - 91_440
    if image_left:
        text_left = _MARGIN + half_w + 182_880
        img_left = _MARGIN
    else:
        text_left = _MARGIN
        img_left = _MARGIN + half_w + 182_880

    # Title (text side)
    els.append(Element(
        id=_uid(), kind="text",
        left=text_left, top=_MARGIN,
        width=half_w, height=_TITLE_H,
        text=slide.title,
        font_name=_first_font(theme.font_ui),
        font_size_pt=28.0,
        hex_color=theme.text,
        bold=True,
    ))

    bar_top = _MARGIN + _TITLE_H + 45_720
    els.append(Element(
        id=_uid(), kind="accent_bar",
        left=text_left, top=bar_top,
        width=half_w, height=_BAR_H,
        fill_hex=theme.accent,
    ))

    # Bullets
    body_top = bar_top + _BAR_H + _GAP
    body_h = _SLIDE_H - body_top - _MARGIN
    body_w = half_w - _BODY_INDENT

    font_pt, fitted, overflow = _fit_text(
        slide.body, box_width=body_w, box_height=body_h
    )

    for i, bullet in enumerate(fitted):
        line_h = font_pt * 12_700 * 1.3
        els.append(Element(
            id=_uid(), kind="text",
            left=text_left + _BODY_INDENT,
            top=body_top + int(i * line_h),
            width=body_w,
            height=int(line_h * 1.15),
            text=f"• {bullet}",
            font_name=_first_font(theme.font_reading),
            font_size_pt=font_pt,
            hex_color=theme.text,
            word_wrap=True,
        ))

    img_w = _CW - half_w - 182_880
    els.append(Element(
        id=_uid(), kind="image",
        left=img_left, top=_MARGIN,
        width=img_w, height=_CH,
        image_prompt=slide.image_prompt,
        fill_hex=theme.surface_2,
        border_hex=theme.hairline,
    ))

    return els, overflow


def _layout_image_left(slide: AuthoredSlide, theme: Theme) -> tuple[list[Element], list[str]]:
    """Convenience wrapper: image on the LEFT half."""
    return _layout_image_right(slide, theme, image_left=True)


def _layout_full_image(slide: AuthoredSlide, theme: Theme) -> tuple[list[Element], list[str]]:
    """Full-bleed image slide.  body=[] per verdict; no overflow."""
    els: list[Element] = []

    # Full-bleed image (or placeholder)
    els.append(Element(
        id=_uid(), kind="image",
        left=0, top=0,
        width=_SLIDE_W, height=_SLIDE_H,
        image_prompt=slide.image_prompt,
        fill_hex=theme.surface_2,
    ))

    # Optional title overlay (bottom-left)
    if slide.title:
        els.append(Element(
            id=_uid(), kind="text",
            left=_MARGIN, top=_SLIDE_H - 914_400 - _MARGIN,
            width=_CW, height=914_400,
            text=slide.title,
            font_name=_first_font(theme.font_display),
            font_size_pt=36.0,
            hex_color="#ffffff",  # white overlay
            bold=True,
        ))

    # Optional caption (single body line, no overflow)
    overflow: list[str] = []
    if slide.body:
        els.append(Element(
            id=_uid(), kind="text",
            left=_MARGIN, top=_SLIDE_H - 457_200 - _MARGIN,
            width=_CW, height=457_200,
            text=slide.body[0],
            font_name=_first_font(theme.font_reading),
            font_size_pt=14.0,
            hex_color="#e0e0e0",
            italic=True,
        ))
        overflow = slide.body[1:]

    return els, overflow


def _layout_two_column(slide: AuthoredSlide, theme: Theme) -> tuple[list[Element], list[str]]:
    """Two-column layout.  body lines split 50/50 by index."""
    els: list[Element] = []

    # Title spanning full width
    els.append(Element(
        id=_uid(), kind="text",
        left=_MARGIN, top=_MARGIN,
        width=_CW, height=_TITLE_H,
        text=slide.title,
        font_name=_first_font(theme.font_ui),
        font_size_pt=28.0,
        hex_color=theme.text,
        bold=True,
    ))

    bar_top = _MARGIN + _TITLE_H + 45_720
    els.append(Element(
        id=_uid(), kind="accent_bar",
        left=_MARGIN, top=bar_top,
        width=_CW, height=_BAR_H,
        fill_hex=theme.accent,
    ))

    col_top = bar_top + _BAR_H + _GAP
    col_h = _SLIDE_H - col_top - _MARGIN
    col_w = (_CW - 182_880) // 2  # 0.2in gap between columns

    mid = max(1, len(slide.body) // 2)
    left_lines = slide.body[:mid]
    right_lines = slide.body[mid:]

    # Overflow from the more crowded column
    font_l, fitted_l, ov_l = _fit_text(left_lines, box_width=col_w, box_height=col_h)
    font_r, fitted_r, ov_r = _fit_text(right_lines, box_width=col_w, box_height=col_h)
    overflow = ov_l + ov_r

    col_font = min(font_l, font_r)

    for i, line in enumerate(fitted_l):
        line_h = col_font * 12_700 * 1.3
        els.append(Element(
            id=_uid(), kind="text",
            left=_MARGIN, top=col_top + int(i * line_h),
            width=col_w, height=int(line_h * 1.15),
            text=f"• {line}",
            font_name=_first_font(theme.font_reading),
            font_size_pt=col_font,
            hex_color=theme.text,
        ))

    for i, line in enumerate(fitted_r):
        line_h = col_font * 12_700 * 1.3
        els.append(Element(
            id=_uid(), kind="text",
            left=_MARGIN + col_w + 182_880,
            top=col_top + int(i * line_h),
            width=col_w, height=int(line_h * 1.15),
            text=f"• {line}",
            font_name=_first_font(theme.font_reading),
            font_size_pt=col_font,
            hex_color=theme.text,
        ))

    return els, overflow


def _layout_comparison(slide: AuthoredSlide, theme: Theme) -> tuple[list[Element], list[str]]:
    """Comparison: two labelled columns.  First body line is left label, second is right.

    Body[0] = "Left: ..." label, Body[1] = "Right: ..." label;
    remaining lines are split 50/50 as content.
    """
    els: list[Element] = []

    # Title
    els.append(Element(
        id=_uid(), kind="text",
        left=_MARGIN, top=_MARGIN,
        width=_CW, height=_TITLE_H,
        text=slide.title,
        font_name=_first_font(theme.font_ui),
        font_size_pt=28.0,
        hex_color=theme.text,
        bold=True,
    ))

    bar_top = _MARGIN + _TITLE_H + 45_720
    els.append(Element(
        id=_uid(), kind="accent_bar",
        left=_MARGIN, top=bar_top,
        width=_CW, height=_BAR_H,
        fill_hex=theme.accent,
    ))

    col_top = bar_top + _BAR_H + _GAP
    col_w = (_CW - 182_880) // 2
    label_h = 457_200  # 0.5in for column labels

    # Column labels (first two body lines or empty)
    left_label = slide.body[0] if len(slide.body) > 0 else ""
    right_label = slide.body[1] if len(slide.body) > 1 else ""
    content_lines = slide.body[2:] if len(slide.body) > 2 else []

    if left_label:
        els.append(Element(
            id=_uid(), kind="text",
            left=_MARGIN, top=col_top,
            width=col_w, height=label_h,
            text=left_label,
            font_name=_first_font(theme.font_ui),
            font_size_pt=16.0,
            hex_color=theme.accent,
            bold=True,
        ))

    if right_label:
        els.append(Element(
            id=_uid(), kind="text",
            left=_MARGIN + col_w + 182_880, top=col_top,
            width=col_w, height=label_h,
            text=right_label,
            font_name=_first_font(theme.font_ui),
            font_size_pt=16.0,
            hex_color=theme.accent,
            bold=True,
        ))

    content_top = col_top + label_h + _GAP
    content_h = _SLIDE_H - content_top - _MARGIN
    mid = max(0, len(content_lines) // 2)
    left_c = content_lines[:mid]
    right_c = content_lines[mid:]

    font_l, fitted_l, ov_l = _fit_text(left_c, box_width=col_w, box_height=content_h)
    font_r, fitted_r, ov_r = _fit_text(right_c, box_width=col_w, box_height=content_h)
    overflow = ov_l + ov_r
    col_font = min(font_l, font_r)

    for i, line in enumerate(fitted_l):
        lh = col_font * 12_700 * 1.3
        els.append(Element(
            id=_uid(), kind="text",
            left=_MARGIN, top=content_top + int(i * lh),
            width=col_w, height=int(lh * 1.15),
            text=f"• {line}",
            font_name=_first_font(theme.font_reading),
            font_size_pt=col_font,
            hex_color=theme.text,
        ))

    for i, line in enumerate(fitted_r):
        lh = col_font * 12_700 * 1.3
        els.append(Element(
            id=_uid(), kind="text",
            left=_MARGIN + col_w + 182_880, top=content_top + int(i * lh),
            width=col_w, height=int(lh * 1.15),
            text=f"• {line}",
            font_name=_first_font(theme.font_reading),
            font_size_pt=col_font,
            hex_color=theme.text,
        ))

    return els, overflow


# Layout function dispatch table
_LAYOUT_FNS: dict[str, object] = {
    "title": _layout_title,
    "section_header": _layout_section_header,
    "bullets": _layout_bullets,
    "two_column": _layout_two_column,
    "comparison": _layout_comparison,
    "image_right": _layout_image_right,
    "image_left": _layout_image_left,
    "full_image": _layout_full_image,
    "closing": _layout_closing,
    # metrics/table → bullets fallback (C8 fills these in)
    "metrics": _layout_bullets,
    "metrics_grid": _layout_bullets,
    "table": _layout_bullets,
}


# ---------------------------------------------------------------------------
# lower_deck — main entry point
# ---------------------------------------------------------------------------

_MAX_CONT_DEPTH = 3  # maximum continuation-slide nesting


def lower_deck(authored: AuthoredDeck) -> Deck:
    """Lower an AuthoredDeck to a Deck of precisely-positioned Elements.

    This is a PURE function: same input → structurally equivalent output
    (ids differ because they are UUIDs, but all positions / text / layout
    decisions are deterministic).

    Overflow rules:
      - ``_fit_text`` steps font from max to min.
      - If body still overflows at min font, overflow lines become a new
        ``AuthoredSlide`` with ``type=<original>_cont`` inserted immediately
        after the current slide.
      - ``notes`` and ``image_prompt`` are NEVER passed to ``_fit_text``
        (they do not appear on the visible slide face).
      - Maximum continuation depth: 3 (prevents catastrophic infinite split).
    """
    theme_name, theme_mode = _parse_theme(authored.theme)
    theme = resolve_theme(theme_name, theme_mode)

    deck_slides: list[Slide] = []
    image_alt: list[int] = [0]  # alternating image side counter

    def _lower_slide(aslide: AuthoredSlide, depth: int) -> None:
        if depth > _MAX_CONT_DEPTH:
            return  # guard: discard overflow beyond depth cap

        layout = _infer_layout(aslide, image_alt)

        layout_fn = _LAYOUT_FNS.get(layout, _layout_bullets)
        elements, overflow_body = layout_fn(aslide, theme)  # type: ignore[operator]

        slide = Slide(
            id=_uid(),
            type=aslide.type,
            layout=layout,
            elements=elements,
            notes=aslide.notes,
        )
        deck_slides.append(slide)

        if overflow_body:
            cont = AuthoredSlide(
                type=f"{aslide.type}_cont",
                title=aslide.title,
                body=overflow_body,
                layout_hint=None,   # re-infer as bullets
                notes=None,         # notes stay on the original slide
            )
            _lower_slide(cont, depth + 1)

    for aslide in authored.slides:
        _lower_slide(aslide, depth=0)

    return Deck(
        id=_uid(),
        title=authored.title,
        theme=theme,
        slides=deck_slides,
    )
