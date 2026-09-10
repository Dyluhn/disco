"""C1 — Two-layer deck schema + deterministic lowering.

Layer 1 (AuthoredDeck / AuthoredSlide): the loose authoring schema the LLM
emits.  Field set FROZEN by the C4 experiment verdict
(development/notes/slides-experiment-verdict.md §6).  Do NOT change the AuthoredSlide
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

This module is the public compatibility/export facade over
:mod:`disco.tools.builtin._deck_schema_parts`, which holds the cohesive private
implementation (authoring/precise/editor schemas, geometry constants, text
fitting, layout inference, the per-layout functions, overflow expansion, and
the two lowering entry points) so every public — and every test-reached
private — symbol keeps this same import path
(``disco.tools.builtin._deck_schema.lower_deck`` and friends).
"""

from __future__ import annotations

from ._deck_schema_parts._authoring_schema import (
    AccentSpec,
    AuthoredDeck,
    AuthoredSlide,
    ChartSpec,
    FontPairingSpec,
    LayoutHint,
    LightDarkTokenPair,
    SlideArchetype,
    TableSpec,
)

# --- compatibility re-exports (PKG-10-MEDIA) --------------------------------
# Names this module exposed BEFORE the parts split. Consumers and the test
# suite reach several of them through this module object (including via
# monkeypatch), so the facade must keep exposing every one. Restored
# programmatically by diffing this module's top-level names against its
# parent commit; PKG-13-FACADES owns their eventual deletion.
from ._deck_schema_parts._authoring_schema import (
    BaseModel as BaseModel,
)
from ._deck_schema_parts._authoring_schema import (
    Field as Field,
)
from ._deck_schema_parts._authoring_schema import (
    Literal as Literal,
)
from ._deck_schema_parts._editor_lower import (
    _BODY_H as _BODY_H,
)
from ._deck_schema_parts._editor_lower import (
    _BODY_INDENT as _BODY_INDENT,
)
from ._deck_schema_parts._editor_lower import (
    _BODY_TOP as _BODY_TOP,
)
from ._deck_schema_parts._editor_lower import (
    _CW as _CW,
)
from ._deck_schema_parts._editor_lower import (
    _E_BODY_H as _E_BODY_H,
)
from ._deck_schema_parts._editor_lower import (
    _E_BODY_LEFT as _E_BODY_LEFT,
)
from ._deck_schema_parts._editor_lower import (
    _E_BODY_TOP as _E_BODY_TOP,
)
from ._deck_schema_parts._editor_lower import (
    _E_BODY_W as _E_BODY_W,
)
from ._deck_schema_parts._editor_lower import (
    _E_BULLET_H as _E_BULLET_H,
)
from ._deck_schema_parts._editor_lower import (
    _E_TITLE_H as _E_TITLE_H,
)
from ._deck_schema_parts._editor_lower import (
    _E_TITLE_LEFT as _E_TITLE_LEFT,
)
from ._deck_schema_parts._editor_lower import (
    _E_TITLE_TOP as _E_TITLE_TOP,
)
from ._deck_schema_parts._editor_lower import (
    _E_TITLE_W as _E_TITLE_W,
)
from ._deck_schema_parts._editor_lower import (
    _MARGIN as _MARGIN,
)
from ._deck_schema_parts._editor_lower import (
    _PCT as _PCT,
)
from ._deck_schema_parts._editor_lower import (
    _SLIDE_H as _SLIDE_H,
)
from ._deck_schema_parts._editor_lower import (
    _SLIDE_W as _SLIDE_W,
)
from ._deck_schema_parts._editor_lower import (
    _TITLE_H as _TITLE_H,
)
from ._deck_schema_parts._editor_lower import (
    Theme as Theme,
)
from ._deck_schema_parts._editor_lower import (
    _EffSlide as _EffSlide,
)
from ._deck_schema_parts._editor_lower import (
    _expand_slides as _expand_slides,
)
from ._deck_schema_parts._editor_lower import (
    _parse_theme as _parse_theme,
)
from ._deck_schema_parts._editor_lower import lower_deck_for_editor
from ._deck_schema_parts._editor_lower import (
    resolve_theme as resolve_theme,
)
from ._deck_schema_parts._editor_schema import (
    ElementGeometry,
    LoweredDeck,
    LoweredElement,
    LoweredSlide,
)
from ._deck_schema_parts._editor_schema import (
    dataclass as dataclass,
)
from ._deck_schema_parts._editor_schema import (
    field as field,
)
from ._deck_schema_parts._expand import (
    _MAX_CONT_DEPTH as _MAX_CONT_DEPTH,
)
from ._deck_schema_parts._expand import (
    _body_partition as _body_partition,
)
from ._deck_schema_parts._expand import (
    _infer_layout as _infer_layout,
)
from ._deck_schema_parts._geometry import (
    _BAR_H as _BAR_H,
)
from ._deck_schema_parts._geometry import (
    _CH as _CH,
)
from ._deck_schema_parts._geometry import (
    _CMP_CONTENT_H as _CMP_CONTENT_H,
)
from ._deck_schema_parts._geometry import (
    _CMP_CONTENT_TOP as _CMP_CONTENT_TOP,
)
from ._deck_schema_parts._geometry import (
    _CMP_LABEL_H as _CMP_LABEL_H,
)
from ._deck_schema_parts._geometry import (
    _COL_BAR_TOP as _COL_BAR_TOP,
)
from ._deck_schema_parts._geometry import (
    _COL_GAP as _COL_GAP,
)
from ._deck_schema_parts._geometry import (
    _COL_TOP as _COL_TOP,
)
from ._deck_schema_parts._geometry import (
    _COL_W as _COL_W,
)
from ._deck_schema_parts._geometry import (
    _GAP as _GAP,
)
from ._deck_schema_parts._geometry import (
    _TWO_COL_H as _TWO_COL_H,
)
from ._deck_schema_parts._helpers import (
    _first_font as _first_font,
)
from ._deck_schema_parts._helpers import (
    _uid as _uid,
)
from ._deck_schema_parts._helpers import (
    parse_template_id as parse_template_id,
)
from ._deck_schema_parts._helpers import (
    uuid as uuid,
)
from ._deck_schema_parts._layout_dispatch import (
    _LAYOUT_FNS as _LAYOUT_FNS,
)
from ._deck_schema_parts._layout_dispatch import (
    _fit_text as _fit_text,
)
from ._deck_schema_parts._layout_dispatch import (
    _layout_bullets as _layout_bullets,
)
from ._deck_schema_parts._layout_dispatch import (
    _layout_closing as _layout_closing,
)
from ._deck_schema_parts._layout_dispatch import (
    _layout_comparison as _layout_comparison,
)
from ._deck_schema_parts._layout_dispatch import (
    _layout_full_image as _layout_full_image,
)
from ._deck_schema_parts._layout_dispatch import (
    _layout_image_left as _layout_image_left,
)
from ._deck_schema_parts._layout_dispatch import (
    _layout_image_right as _layout_image_right,
)
from ._deck_schema_parts._layout_dispatch import (
    _layout_section_header as _layout_section_header,
)
from ._deck_schema_parts._layout_dispatch import (
    _layout_title as _layout_title,
)
from ._deck_schema_parts._layout_dispatch import (
    _layout_two_column as _layout_two_column,
)
from ._deck_schema_parts._layouts_basic import (
    _visible_body_count_without_orphan as _visible_body_count_without_orphan,
)
from ._deck_schema_parts._lower import lower_deck
from ._deck_schema_parts._precise_schema import Deck, Element, Slide
from ._deck_schema_parts._text_fit import (
    _BODY_FONT_MAX as _BODY_FONT_MAX,
)
from ._deck_schema_parts._text_fit import (
    _BODY_FONT_MIN as _BODY_FONT_MIN,
)
from ._deck_schema_parts._text_fit import (
    _BODY_FONT_STEP as _BODY_FONT_STEP,
)
from ._deck_schema_parts._text_fit import (
    _REF_BOX_WIDTH_EMU as _REF_BOX_WIDTH_EMU,
)
from ._deck_schema_parts._text_fit import (
    _REF_CHARS_PER_LINE as _REF_CHARS_PER_LINE,
)
from ._deck_schema_parts._text_fit import (
    _REF_FONT_PT as _REF_FONT_PT,
)
from ._deck_schema_parts._text_fit import (
    _chars_per_line as _chars_per_line,
)
from ._deck_schema_parts._text_fit import (
    _lines_per_box as _lines_per_box,
)

__all__ = [
    "AccentSpec",
    "AuthoredDeck",
    "AuthoredSlide",
    "ChartSpec",
    "Deck",
    "Element",
    "ElementGeometry",
    "FontPairingSpec",
    "LayoutHint",
    "LightDarkTokenPair",
    "LoweredDeck",
    "LoweredElement",
    "LoweredSlide",
    "Slide",
    "SlideArchetype",
    "TableSpec",
    "lower_deck",
    "lower_deck_for_editor",
]
