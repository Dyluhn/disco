"""Per-layout functions: two_column, comparison.

Extracted from ``_deck_schema.py`` to reduce module size; the public facade
re-imports these names unchanged. Each layout fn is ≤ 200 LOC (arch budget).

``_layout_comparison`` was split into small step helpers (one per original
section: title/bar, column labels, content columns) purely to stay under the
callable logical-line cap — the computation and element order are unchanged.
"""

from __future__ import annotations

from disco.core.brand.tokens import Theme

from ._authoring_schema import AuthoredSlide
from ._geometry import (
    _BAR_H,
    _CMP_CONTENT_H,
    _CMP_CONTENT_TOP,
    _CMP_LABEL_H,
    _COL_GAP,
    _COL_TOP,
    _COL_W,
    _CW,
    _MARGIN,
    _TITLE_H,
    _TWO_COL_H,
)
from ._helpers import _first_font, _uid
from ._precise_schema import Element
from ._text_fit import _fit_text


def _layout_two_column(slide: AuthoredSlide, theme: Theme) -> tuple[list[Element], list[str]]:
    """Two-column layout.  body lines split 50/50 by index."""
    els: list[Element] = []

    # Title spanning full width
    els.append(
        Element(
            id=_uid(),
            kind="text",
            left=_MARGIN,
            top=_MARGIN,
            width=_CW,
            height=_TITLE_H,
            text=slide.title,
            font_name=_first_font(theme.font_ui),
            font_size_pt=28.0,
            hex_color=theme.text,
            bold=True,
        )
    )

    bar_top = _MARGIN + _TITLE_H + 45_720
    els.append(
        Element(
            id=_uid(),
            kind="accent_bar",
            left=_MARGIN,
            top=bar_top,
            width=_CW,
            height=_BAR_H,
            fill_hex=theme.accent,
        )
    )

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
        els.append(
            Element(
                id=_uid(),
                kind="text",
                left=_MARGIN,
                top=col_top + int(i * line_h),
                width=col_w,
                height=int(line_h * 1.15),
                text=f"• {line}",
                font_name=_first_font(theme.font_reading),
                font_size_pt=col_font,
                hex_color=theme.text,
            )
        )

    for i, line in enumerate(fitted_r):
        line_h = col_font * 12_700 * 1.3
        els.append(
            Element(
                id=_uid(),
                kind="text",
                left=_MARGIN + col_w + _COL_GAP,
                top=col_top + int(i * line_h),
                width=col_w,
                height=int(line_h * 1.15),
                text=f"• {line}",
                font_name=_first_font(theme.font_reading),
                font_size_pt=col_font,
                hex_color=theme.text,
            )
        )

    return els, overflow


def _comparison_title_and_bar(slide: AuthoredSlide, theme: Theme) -> list[Element]:
    """Comparison step 1/3: full-width title + accent bar (same geometry as
    the other title+bar layouts)."""
    els: list[Element] = []

    els.append(
        Element(
            id=_uid(),
            kind="text",
            left=_MARGIN,
            top=_MARGIN,
            width=_CW,
            height=_TITLE_H,
            text=slide.title,
            font_name=_first_font(theme.font_ui),
            font_size_pt=28.0,
            hex_color=theme.text,
            bold=True,
        )
    )

    bar_top = _MARGIN + _TITLE_H + 45_720
    els.append(
        Element(
            id=_uid(),
            kind="accent_bar",
            left=_MARGIN,
            top=bar_top,
            width=_CW,
            height=_BAR_H,
            fill_hex=theme.accent,
        )
    )

    return els


def _comparison_column_labels(
    slide: AuthoredSlide, theme: Theme
) -> tuple[list[Element], list[str]]:
    """Comparison step 2/3: the two (optional) column-label elements — body[0] is
    the left label, body[1] is the right label — plus the remaining content
    lines (body[2:]) the content-columns step splits 50/50."""
    els: list[Element] = []

    col_top = _COL_TOP
    col_w = _COL_W
    label_h = _CMP_LABEL_H  # 0.5in for column labels

    # Column labels (first two body lines or empty)
    left_label = slide.body[0] if len(slide.body) > 0 else ""
    right_label = slide.body[1] if len(slide.body) > 1 else ""
    content_lines = slide.body[2:] if len(slide.body) > 2 else []

    if left_label:
        els.append(
            Element(
                id=_uid(),
                kind="text",
                left=_MARGIN,
                top=col_top,
                width=col_w,
                height=label_h,
                text=left_label,
                font_name=_first_font(theme.font_ui),
                font_size_pt=16.0,
                hex_color=theme.accent,
                bold=True,
            )
        )

    if right_label:
        els.append(
            Element(
                id=_uid(),
                kind="text",
                left=_MARGIN + col_w + _COL_GAP,
                top=col_top,
                width=col_w,
                height=label_h,
                text=right_label,
                font_name=_first_font(theme.font_ui),
                font_size_pt=16.0,
                hex_color=theme.accent,
                bold=True,
            )
        )

    return els, content_lines


def _comparison_content_columns(
    content_lines: list[str], theme: Theme
) -> tuple[list[Element], list[str]]:
    """Comparison step 3/3: split ``content_lines`` 50/50 into two fitted
    columns (same _fit_text overflow control as the other layouts)."""
    els: list[Element] = []

    content_top = _CMP_CONTENT_TOP
    content_h = _CMP_CONTENT_H
    col_w = _COL_W
    mid = max(0, len(content_lines) // 2)
    left_c = content_lines[:mid]
    right_c = content_lines[mid:]

    font_l, fitted_l, ov_l = _fit_text(left_c, box_width=col_w, box_height=content_h)
    font_r, fitted_r, ov_r = _fit_text(right_c, box_width=col_w, box_height=content_h)
    overflow = ov_l + ov_r
    col_font = min(font_l, font_r)

    for i, line in enumerate(fitted_l):
        lh = col_font * 12_700 * 1.3
        els.append(
            Element(
                id=_uid(),
                kind="text",
                left=_MARGIN,
                top=content_top + int(i * lh),
                width=col_w,
                height=int(lh * 1.15),
                text=f"• {line}",
                font_name=_first_font(theme.font_reading),
                font_size_pt=col_font,
                hex_color=theme.text,
            )
        )

    for i, line in enumerate(fitted_r):
        lh = col_font * 12_700 * 1.3
        els.append(
            Element(
                id=_uid(),
                kind="text",
                left=_MARGIN + col_w + _COL_GAP,
                top=content_top + int(i * lh),
                width=col_w,
                height=int(lh * 1.15),
                text=f"• {line}",
                font_name=_first_font(theme.font_reading),
                font_size_pt=col_font,
                hex_color=theme.text,
            )
        )

    return els, overflow


def _layout_comparison(slide: AuthoredSlide, theme: Theme) -> tuple[list[Element], list[str]]:
    """Comparison: two labelled columns.  First body line is left label, second is right.

    Body[0] = "Left: ..." label, Body[1] = "Right: ..." label;
    remaining lines are split 50/50 as content.
    """
    els: list[Element] = []
    els.extend(_comparison_title_and_bar(slide, theme))

    label_els, content_lines = _comparison_column_labels(slide, theme)
    els.extend(label_els)

    content_els, overflow = _comparison_content_columns(content_lines, theme)
    els.extend(content_els)

    return els, overflow
