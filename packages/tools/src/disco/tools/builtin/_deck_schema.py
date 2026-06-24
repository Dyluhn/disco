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

from disco.core.brand import parse_template_id, resolve_theme
from disco.core.brand.tokens import Theme
from pydantic import BaseModel, Field

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

# Column geometry — SINGLE source of truth shared by the two_column / comparison
# layout fns AND _body_partition, so the editor's overflow split is byte-identical
# to the export's (BW-13 non-suffix overflow parity).
_COL_GAP = 182_880               # 0.2 in gap between the two columns
_COL_W = (_CW - _COL_GAP) // 2
_COL_BAR_TOP = _MARGIN + _TITLE_H + 45_720
_COL_TOP = _COL_BAR_TOP + _BAR_H + _GAP
_TWO_COL_H = _SLIDE_H - _COL_TOP - _MARGIN          # two_column body box height
_CMP_LABEL_H = 457_200           # 0.5 in column-label strip (comparison)
_CMP_CONTENT_TOP = _COL_TOP + _CMP_LABEL_H + _GAP
_CMP_CONTENT_H = _SLIDE_H - _CMP_CONTENT_TOP - _MARGIN  # comparison content box height

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
    """Structured chart payload (wired in C8).

    Field set frozen by the C4 experiment verdict (docs/slides-experiment-verdict.md §6).
    labels / series have empty-list defaults so ChartSpec(kind=...) is valid with
    no data (c8 rendering handles empty data gracefully with a placeholder shape).
    """

    kind: Literal["bar", "line", "pie", "scatter"]
    title: str = ""
    labels: list[str] = Field(default_factory=list)
    series: list[dict] = Field(default_factory=list)   # [{"name": str, "data": [float]}]


class TableSpec(BaseModel):
    """Structured table payload.

    headers / rows have empty-list defaults for the same reason as ChartSpec.
    """

    headers: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)


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
    # The deck's base template. The user can override it at export time via the
    # template selector; this is the agent's authoring-time default.
    theme: Literal[
        "disco-light",
        "disco-dark",
        "ink-light",
        "sepia-light",
        "signal-light",
        "midnight-dark",
        "neutral",
        "neutral-light",
    ] = "disco-light"
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
    type: str                   # from AuthoredSlide.type (e.g. "bullets_cont")
    layout: LayoutHint          # resolved, never None
    title: str = ""             # propagated from AuthoredSlide.title (for c8 duck-typing)
    elements: list[Element] = field(default_factory=list)
    notes: str | None = None
    chart: ChartSpec | None = None   # propagated when AuthoredSlide.chart is set
    table: TableSpec | None = None   # propagated when AuthoredSlide.table is set


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
    """Split a "{name}-{mode}" template id → (name, mode). Delegates to the brand
    catalogue's generalized parser so ANY registered template (disco/ink/sepia/
    signal/midnight/neutral…) works as a deck theme, not just the original three.
    resolve_theme takes two args, so callers split rather than pass the compound."""
    return parse_template_id(theme_str)


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
    # Continuation fragments (BW-12) must NEVER re-infer back to
    # title/section_header/closing — that rendered a DUPLICATE title page with
    # the same heading.  A "_cont" slide only ever carries spilled body text, so
    # it is always bullets, regardless of the base type.
    if slide.type.lower().endswith("_cont"):
        return "bullets"

    typ = slide.type.lower()
    if typ in _TYPE_MAP:
        return _TYPE_MAP[typ]

    # Default — covers "bullets", custom types
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

    col_top = _COL_TOP
    col_h = _TWO_COL_H
    col_w = _COL_W

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
            left=_MARGIN + col_w + _COL_GAP,
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

    col_top = _COL_TOP
    col_w = _COL_W
    label_h = _CMP_LABEL_H  # 0.5in for column labels

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
            left=_MARGIN + col_w + _COL_GAP, top=col_top,
            width=col_w, height=label_h,
            text=right_label,
            font_name=_first_font(theme.font_ui),
            font_size_pt=16.0,
            hex_color=theme.accent,
            bold=True,
        ))

    content_top = _CMP_CONTENT_TOP
    content_h = _CMP_CONTENT_H
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
            left=_MARGIN + col_w + _COL_GAP, top=content_top + int(i * lh),
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
# _body_partition — which body positions a layout renders vs. spills
# ---------------------------------------------------------------------------


def _body_partition(
    aslide: AuthoredSlide, layout: LayoutHint, theme: Theme
) -> tuple[list[int], list[int]]:
    """Partition ``aslide.body`` positions into ``(rendered, overflow)``.

    Returns the indices into ``aslide.body`` this layout actually shows vs. spills,
    IN RENDER ORDER.  This is the single source of truth the editor lowering uses
    to map each rendered bullet back to its authored ``/body/{j}`` pointer, and it
    must agree EXACTLY with what the layout fn draws / drops (BW-13).

    Two overflow kinds:
      • SUFFIX (contiguous): every layout except the column layouts spills a
        contiguous tail of the body, so the split is derived from the layout fn's
        own overflow length.
      • NON-SUFFIX (column layouts): ``two_column`` / ``comparison`` consume the
        body NON-contiguously — each column drops ITS OWN tail, so the rendered
        set interleaves the two column heads and the overflow is the UNION of the
        two column tails.  The naive ``body[:consumed]`` prefix model is wrong for
        this case, which is exactly the editor↔export pointer drift BW-13 fixes.
    """
    n = len(aslide.body)
    if n == 0:
        return [], []

    if layout == "two_column":
        mid = max(1, n // 2)
        _fl, fitted_l, _ovl = _fit_text(aslide.body[:mid], _COL_W, _TWO_COL_H)
        _fr, fitted_r, _ovr = _fit_text(aslide.body[mid:], _COL_W, _TWO_COL_H)
        kl, kr = len(fitted_l), len(fitted_r)
        # Render order mirrors the layout fn: left head, then right head.
        rendered = list(range(0, kl)) + list(range(mid, mid + kr))
        # Overflow mirrors ov_l + ov_r: left tail, then right tail.
        overflow = list(range(kl, mid)) + list(range(mid + kr, n))
        return rendered, overflow

    if layout == "comparison":
        # body[0]/body[1] are the (always-shown) column labels; body[2:] is the
        # 50/50-split content that can overflow per column.
        content = aslide.body[2:]
        nc = len(content)
        mid = max(0, nc // 2)
        _fl, fitted_l, _ovl = _fit_text(content[:mid], _COL_W, _CMP_CONTENT_H)
        _fr, fitted_r, _ovr = _fit_text(content[mid:], _COL_W, _CMP_CONTENT_H)
        kl, kr = len(fitted_l), len(fitted_r)
        rendered = [p for p in (0, 1) if p < n]              # labels
        rendered += list(range(2, 2 + kl))                   # left content head
        rendered += list(range(2 + mid, 2 + mid + kr))       # right content head
        overflow = list(range(2 + kl, 2 + mid))              # left content tail
        overflow += list(range(2 + mid + kr, n))             # right content tail
        return rendered, overflow

    # Contiguous-suffix layouts (bullets / image_* / title / section_header /
    # closing / full_image / metrics / table): overflow is the body tail.
    layout_fn = _LAYOUT_FNS.get(layout, _layout_bullets)
    _els, overflow_lines = layout_fn(aslide, theme)  # type: ignore[operator]
    k = n - len(overflow_lines)
    return list(range(0, k)), list(range(k, n))


# ---------------------------------------------------------------------------
# lower_deck — main entry point
# ---------------------------------------------------------------------------

_MAX_CONT_DEPTH = 3  # maximum continuation-slide nesting


@dataclass
class _EffSlide:
    """One *effective* slide after overflow expansion.

    The render path (``lower_deck``) and the editor path
    (``lower_deck_for_editor``) BOTH iterate this same expansion so their slide
    counts are guaranteed identical (BW-13).  Each fragment carries ``render_body``
    (exactly the body lines that belong on THIS slide, in render order) and the
    parallel ``body_index_map`` (the ORIGINAL authored-body index of each rendered
    line) so the editor can map every spilled bullet back to its real
    ``/slides/{orig_index}/body/{j}`` JSON pointer.

    ``body_index_map`` (not a single ``body_offset``) is required because the
    column layouts consume the body NON-contiguously: ``render_body[k]`` is the
    line ``aslide.body``-local, and ``body_index_map[k]`` is where it really lives
    in the ORIGINAL slide's body — these are NOT a contiguous run for two_column /
    comparison overflow (BW-13 non-suffix case).
    """

    aslide: AuthoredSlide      # original slide OR a synthesized "_cont" fragment
    layout: LayoutHint         # resolved ONCE here (image-side alternation included)
    render_body: list[str]     # body lines this fragment shows, in render order
    body_index_map: list[int]  # original-body index of each render_body line
    orig_index: int            # index into authored.slides (image bytes + pointer base)
    is_cont: bool


def _expand_slides(
    slides: list[AuthoredSlide], theme: Theme, image_alt: list[int]
) -> list[_EffSlide]:
    """Deterministically expand authored slides into the effective rendered set.

    Runs the SAME font-fit / overflow logic as lowering: a slide whose body
    overflows at the font floor spills its tail into a ``bullets`` continuation
    fragment (NOT a re-inferred title/section/closing — that produced BW-12's
    duplicate title pages).  ``image_alt`` is advanced HERE only; callers must
    consume ``_EffSlide.layout`` rather than re-inferring (which would
    double-advance the image-side alternation counter).

    ``index_map`` threads each fragment's local body position back to its index in
    the ORIGINAL authored slide, so a NON-suffix (column) overflow keeps every
    spilled bullet pointed at the right ``/body/{j}`` (BW-13).
    """
    eff: list[_EffSlide] = []

    def _walk(
        aslide: AuthoredSlide, depth: int, orig_index: int, index_map: list[int]
    ) -> None:
        if depth > _MAX_CONT_DEPTH:
            return  # guard: discard overflow beyond depth cap
        layout = _infer_layout(aslide, image_alt)
        rendered_pos, overflow_pos = _body_partition(aslide, layout, theme)
        eff.append(_EffSlide(
            aslide=aslide,
            layout=layout,
            render_body=[aslide.body[p] for p in rendered_pos],
            body_index_map=[index_map[p] for p in rendered_pos],
            orig_index=orig_index,
            is_cont=depth > 0,
        ))
        if overflow_pos:
            # Spilled lines, in overflow order, with their ORIGINAL indices — same
            # content the export's layout fn drops (determinism preserved).
            cont = AuthoredSlide(
                type=f"{aslide.type}_cont",
                title=f"{aslide.title} (cont.)",
                body=[aslide.body[p] for p in overflow_pos],
                layout_hint="bullets",   # FORCE bullets — never map back to title/section/closing
                notes=None,              # notes stay on the original slide
            )
            _walk(cont, depth + 1, orig_index, [index_map[p] for p in overflow_pos])

    for i, aslide in enumerate(slides):
        _walk(aslide, depth=0, orig_index=i, index_map=list(range(len(aslide.body))))
    return eff


def lower_deck(
    authored: AuthoredDeck,
    *,
    theme_override: str | None = None,
    image_assets: dict[int, bytes] | None = None,
) -> Deck:
    """Lower an AuthoredDeck to a Deck of precisely-positioned Elements.

    This is a PURE function: same input → structurally equivalent output
    (ids differ because they are UUIDs, but all positions / text / layout
    decisions are deterministic).

    ``theme_override`` ("{name}-{mode}" template id) re-themes the deck at render
    time WITHOUT mutating the authored sidecar — this is the slide-deck template
    selector's render-on-demand path. None → use the deck's authored theme.

    ``image_assets`` (C7 wire) maps an AUTHORED-slide index → generated image bytes.
    The image element lowered from that slide carries the bytes so the renderer
    embeds the real picture instead of the ``[image]`` placeholder. None → no
    images generated yet (the re-theme / editor paths), so placeholders render as
    before.

    Overflow rules:
      - ``_fit_text`` steps font from max to min.
      - If body still overflows at min font, overflow lines become a new
        ``AuthoredSlide`` with ``type=<original>_cont`` inserted immediately
        after the current slide.
      - ``notes`` and ``image_prompt`` are NEVER passed to ``_fit_text``
        (they do not appear on the visible slide face).
      - Maximum continuation depth: 3 (prevents catastrophic infinite split).
    """
    theme_name, theme_mode = _parse_theme(theme_override or authored.theme)
    theme = resolve_theme(theme_name, theme_mode)

    # Shared overflow expansion — identical to the editor's, so counts match (BW-13).
    image_alt: list[int] = [0]  # alternating image side counter
    eff_slides = _expand_slides(authored.slides, theme, image_alt)

    deck_slides: list[Slide] = []
    for eff in eff_slides:
        aslide = eff.aslide
        layout = eff.layout
        layout_fn = _LAYOUT_FNS.get(layout, _layout_bullets)
        # The layout fn drops this fragment's own overflow internally; the
        # expansion already created the matching continuation _EffSlide for it.
        elements, _overflow = layout_fn(aslide, theme)  # type: ignore[operator]

        # C7 wire: attach generated image bytes to this slide's image element(s).
        # Only ORIGINAL (non-cont) slides carry an image_prompt; continuations are
        # text-only, so nothing is attached for them.
        if image_assets and not eff.is_cont and eff.orig_index in image_assets:
            for el in elements:
                if el.kind == "image":
                    el.image_bytes = image_assets[eff.orig_index]

        deck_slides.append(Slide(
            id=_uid(),
            type=aslide.type,
            title=aslide.title,
            layout=layout,
            elements=elements,
            notes=aslide.notes,
            chart=aslide.chart,
            table=aslide.table,
        ))

    return Deck(
        id=_uid(),
        title=authored.title,
        theme=theme,
        slides=deck_slides,
    )


# ---------------------------------------------------------------------------
# Layer 3 — editor geometry (percentage-based, consumed by React DeckEditor)
# ---------------------------------------------------------------------------

@dataclass
class ElementGeometry:
    """Position + size as percentage of the 16:9 slide canvas (0-100)."""

    x: float
    y: float
    w: float
    h: float


@dataclass
class LoweredElement:
    """One element as the React editor sees it — percentage geometry + JSON pointer."""

    element_id: str        # e.g. "slide-0:title"
    slide_id: str          # e.g. "slide-0"
    kind: str              # "title" | "subtitle" | "bullet" | "chart" | "table" | "image_prompt"
    content: str           # display text
    geometry: ElementGeometry
    font_size_vw: float    # font-size in vw units
    font_weight: str       # "normal" | "bold"
    font_style: str        # "normal" | "italic"
    json_pointer: str      # RFC-6901 path into AuthoredDeck (e.g. "/slides/0/title")


@dataclass
class LoweredSlide:
    """One slide for the editor — geometry in % coordinates."""

    slide_id: str
    slide_idx: int
    layout: str
    bg_color: str
    elements: list[LoweredElement] = field(default_factory=list)


@dataclass
class LoweredDeck:
    """Full lowered deck for the React editor."""

    title: str
    theme_name: str
    theme_mode: str
    slides: list[LoweredSlide] = field(default_factory=list)


# ---------------------------------------------------------------------------
# lower_deck_for_editor — produce a LoweredDeck from an AuthoredDeck
# ---------------------------------------------------------------------------

# Percentage constants derived from the EMU layout (author → editor geometry)
_PCT = 100.0

# Title strip: left=4%, top=6%, width=92%, height=13%
_E_TITLE_LEFT   = round(_MARGIN / _SLIDE_W * _PCT, 2)        # ≈ 3.75
_E_TITLE_TOP    = round(_MARGIN / _SLIDE_H * _PCT, 2)        # ≈ 6.67
_E_TITLE_W      = round(_CW / _SLIDE_W * _PCT, 2)            # ≈ 92.5
_E_TITLE_H      = round(_TITLE_H / _SLIDE_H * _PCT, 2)       # ≈ 13.33

# Body area: below title+bar
_E_BODY_TOP     = round(_BODY_TOP / _SLIDE_H * _PCT, 2)      # ≈ 26.7
_E_BODY_H       = round(_BODY_H / _SLIDE_H * _PCT, 2)        # ≈ 66.7
_E_BODY_W       = round((_CW - _BODY_INDENT) / _SLIDE_W * _PCT, 2)  # ≈ 88.6
_E_BODY_LEFT    = round((_MARGIN + _BODY_INDENT) / _SLIDE_W * _PCT, 2)  # ≈ 5.6

# Per-bullet height: ~8% of slide
_E_BULLET_H     = 8.0


def lower_deck_for_editor(authored: AuthoredDeck) -> LoweredDeck:
    """Lower an AuthoredDeck to a LoweredDeck with percentage geometry for the React editor.

    Produces one LoweredElement per visible authored field (title, body[j], chart,
    table, image_prompt).  ``notes`` are excluded (not displayed on the slide face).
    Geometry is expressed as % of the 16:9 canvas so the editor canvas can use
    ``position:absolute; left: {x}%; top: {y}%; width: {w}%; height: {h}%``.
    """
    theme_name, theme_mode = _parse_theme(authored.theme)
    theme = resolve_theme(theme_name, theme_mode)

    # SAME overflow expansion as lower_deck, so the editor shows exactly the
    # slides the export produces (BW-13).  ``orig``/``boff`` map this fragment's
    # fields back to their real JSON pointers in the authored deck.
    image_alt: list[int] = [0]
    eff_slides = _expand_slides(authored.slides, theme, image_alt)

    lslides: list[LoweredSlide] = []

    for si, eff in enumerate(eff_slides):
        aslide = eff.aslide
        layout = eff.layout
        orig = eff.orig_index
        render_body = eff.render_body
        body_index_map = eff.body_index_map
        slide_id = f"slide-{si}"
        bg_color = theme.surface_1 if layout == "section_header" else theme.bg
        elements: list[LoweredElement] = []

        def _add(
            kind: str,
            content: str,
            x: float, y: float, w: float, h: float,
            fsz: float, fw: str, fi: str,
            jptr: str,
        ) -> None:
            elements.append(LoweredElement(
                element_id=f"{slide_id}:{kind}" if ":" not in kind else f"{slide_id}:{kind}",
                slide_id=slide_id,
                kind=kind.split(":")[-1] if ":" in kind else kind,
                content=content,
                geometry=ElementGeometry(x=x, y=y, w=w, h=h),
                font_size_vw=fsz,
                font_weight=fw,
                font_style=fi,
                json_pointer=jptr,
            ))

        # Title element (always present).  Continuation fragments point back at
        # the ORIGINAL slide's title (their "(cont.)" suffix is display-only).
        _add(
            kind="title", content=aslide.title,
            x=_E_TITLE_LEFT, y=_E_TITLE_TOP, w=_E_TITLE_W, h=_E_TITLE_H,
            fsz=2.5, fw="bold", fi="normal",
            jptr=f"/slides/{orig}/title",
        )
        elements[-1].element_id = f"{slide_id}:title"

        # Subtitle (first body line on title/section/closing slides).  Its real
        # authored index comes from body_index_map (NOT a contiguous offset).
        if layout in ("title", "section_header", "closing") and render_body:
            sub_top = _E_TITLE_TOP + _E_TITLE_H + 2.0
            _add(
                kind="subtitle", content=render_body[0],
                x=_E_TITLE_LEFT, y=sub_top, w=_E_TITLE_W, h=_E_BULLET_H,
                fsz=1.8, fw="normal", fi="italic",
                jptr=f"/slides/{orig}/body/{body_index_map[0]}",
            )
            elements[-1].element_id = f"{slide_id}:subtitle"
            body_start = 1
        else:
            body_start = 0

        # Bullet body lines — only the lines that belong to THIS fragment.  Each
        # rendered line maps back to its ORIGINAL authored index via
        # body_index_map, so NON-suffix (column) overflow points correctly (BW-13).
        body_y = _E_BODY_TOP
        for local_j in range(body_start, len(render_body)):
            line = render_body[local_j]
            orig_bj = body_index_map[local_j]
            content_stripped = line.lstrip("• ")
            _add(
                kind=f"body:{orig_bj}", content=content_stripped,
                x=_E_BODY_LEFT, y=body_y, w=_E_BODY_W, h=_E_BULLET_H,
                fsz=1.6, fw="normal", fi="normal",
                jptr=f"/slides/{orig}/body/{orig_bj}",
            )
            elements[-1].element_id = f"{slide_id}:body:{orig_bj}"
            elements[-1].kind = "bullet"
            body_y = min(body_y + _E_BULLET_H + 1.0, 90.0)

        # Chart placeholder element (originals only — continuations are text-only)
        if aslide.chart is not None:
            chart_desc = f"[{aslide.chart.kind} chart] {aslide.chart.title}"
            _add(
                kind="chart", content=chart_desc,
                x=_E_TITLE_LEFT, y=_E_BODY_TOP, w=_E_TITLE_W, h=55.0,
                fsz=1.4, fw="normal", fi="normal",
                jptr=f"/slides/{orig}/chart",
            )
            elements[-1].element_id = f"{slide_id}:chart"

        # Table placeholder element
        if aslide.table is not None:
            table_desc = f"[table] {' | '.join(aslide.table.headers[:4])}"
            _add(
                kind="table", content=table_desc,
                x=_E_TITLE_LEFT, y=_E_BODY_TOP, w=_E_TITLE_W, h=55.0,
                fsz=1.4, fw="normal", fi="normal",
                jptr=f"/slides/{orig}/table",
            )
            elements[-1].element_id = f"{slide_id}:table"

        # Image prompt placeholder element
        if aslide.image_prompt is not None:
            _add(
                kind="image_prompt", content=aslide.image_prompt,
                x=_E_TITLE_LEFT, y=_E_BODY_TOP, w=_E_TITLE_W, h=55.0,
                fsz=1.4, fw="normal", fi="normal",
                jptr=f"/slides/{orig}/image_prompt",
            )
            elements[-1].element_id = f"{slide_id}:image_prompt"

        lslides.append(LoweredSlide(
            slide_id=slide_id,
            slide_idx=si,
            layout=layout,
            bg_color=bg_color,
            elements=elements,
        ))

    return LoweredDeck(
        title=authored.title,
        theme_name=theme_name,
        theme_mode=theme_mode,
        slides=lslides,
    )
