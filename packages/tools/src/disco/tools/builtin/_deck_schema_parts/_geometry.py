"""Slide canvas geometry (16:9) — the single source of truth for EMU positions.

Extracted from ``_deck_schema.py`` to reduce module size. These constants are
shared by the per-layout functions, ``_body_partition``, and the editor's
percentage-geometry lowering (``lower_deck_for_editor``) so the editor's overflow
split stays byte-identical to the export's (BW-13 non-suffix overflow parity).
"""

from __future__ import annotations

_SLIDE_W = 12_192_000  # 13.33 inches
_SLIDE_H = 6_858_000  # 7.5 inches
_MARGIN = 457_200  # 0.5 inch
_CW = _SLIDE_W - 2 * _MARGIN  # 11 277 600
_CH = _SLIDE_H - 2 * _MARGIN  # 5 943 600
_TITLE_H = 914_400  # ≈1 in title strip
_BAR_H = 91_440  # 0.1 in accent bar
_GAP = 91_440  # 0.1 in gap between elements

# Content box (below title + bar) used for body text
_BODY_TOP = _MARGIN + _TITLE_H + _GAP + _BAR_H + _GAP
_BODY_H = _SLIDE_H - _BODY_TOP - _MARGIN
_BODY_INDENT = 228_600  # 0.25 in left indent for bullets

# Column geometry — SINGLE source of truth shared by the two_column / comparison
# layout fns AND _body_partition, so the editor's overflow split is byte-identical
# to the export's (BW-13 non-suffix overflow parity).
_COL_GAP = 182_880  # 0.2 in gap between the two columns
_COL_W = (_CW - _COL_GAP) // 2
_COL_BAR_TOP = _MARGIN + _TITLE_H + 45_720
_COL_TOP = _COL_BAR_TOP + _BAR_H + _GAP
_TWO_COL_H = _SLIDE_H - _COL_TOP - _MARGIN  # two_column body box height
_CMP_LABEL_H = 457_200  # 0.5 in column-label strip (comparison)
_CMP_CONTENT_TOP = _COL_TOP + _CMP_LABEL_H + _GAP
_CMP_CONTENT_H = _SLIDE_H - _CMP_CONTENT_TOP - _MARGIN  # comparison content box height
