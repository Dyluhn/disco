"""Per-layout functions: title, section_header, bullets, closing.

Extracted from ``_deck_schema.py`` to reduce module size; the public facade
re-imports these names unchanged. Each layout fn is ≤ 200 LOC (arch budget).
"""

from __future__ import annotations

from disco.core.brand.tokens import Theme

from ._authoring_schema import AuthoredSlide
from ._geometry import _BAR_H, _BODY_INDENT, _CW, _GAP, _MARGIN, _SLIDE_H, _TITLE_H
from ._helpers import _first_font, _uid
from ._precise_schema import Element
from ._text_fit import _fit_text, _visible_body_count_without_orphan


def _layout_title(slide: AuthoredSlide, theme: Theme) -> tuple[list[Element], list[str]]:
    """Title slide: large display title + optional subtitle."""
    els: list[Element] = []

    v_origin = int(_SLIDE_H * 0.28)

    # Accent bar
    els.append(
        Element(
            id=_uid(),
            kind="accent_bar",
            left=_MARGIN,
            top=v_origin,
            width=2_286_000,
            height=_BAR_H,
            fill_hex=theme.accent,
        )
    )

    # Display title
    title_top = v_origin + 182_880
    title_h = 1_828_800
    els.append(
        Element(
            id=_uid(),
            kind="text",
            left=_MARGIN,
            top=title_top,
            width=_CW,
            height=title_h,
            text=slide.title,
            font_name=_first_font(theme.font_display),
            font_size_pt=52.0,
            hex_color=theme.text,
        )
    )

    # Subtitle (first body line only; image_prompt excluded per verdict)
    overflow: list[str] = []
    shown = _visible_body_count_without_orphan(len(slide.body), 1)
    for i, line in enumerate(slide.body[:shown]):
        sub_top = title_top + title_h + int(i * 365_760)
        els.append(
            Element(
                id=_uid(),
                kind="text",
                left=_MARGIN,
                top=sub_top,
                width=_CW,
                height=365_760,
                text=line,
                font_name=_first_font(theme.font_reading),
                font_size_pt=24.0,
                hex_color=theme.text_muted,
                italic=True,
            )
        )
    if slide.body:
        overflow = slide.body[shown:]

    return els, overflow


def _layout_section_header(slide: AuthoredSlide, theme: Theme) -> tuple[list[Element], list[str]]:
    """Section-divider: vertically centred title with kicker + accent bar."""
    els: list[Element] = []

    v_center = _SLIDE_H // 2
    bar_top = v_center - 228_600

    # Kicker
    kicker_top = bar_top - 457_200
    els.append(
        Element(
            id=_uid(),
            kind="text",
            left=_MARGIN,
            top=kicker_top,
            width=_CW,
            height=457_200,
            text="SECTION",
            font_name=_first_font(theme.font_ui),
            font_size_pt=9.0,
            hex_color=theme.accent,
            bold=True,
        )
    )

    # Accent bar
    els.append(
        Element(
            id=_uid(),
            kind="accent_bar",
            left=_MARGIN,
            top=bar_top,
            width=1_371_600,
            height=_BAR_H,
            fill_hex=theme.accent,
        )
    )

    # Title
    title_top = bar_top + 182_880
    title_h = 1_371_600
    els.append(
        Element(
            id=_uid(),
            kind="text",
            left=_MARGIN,
            top=title_top,
            width=_CW,
            height=title_h,
            text=slide.title,
            font_name=_first_font(theme.font_display),
            font_size_pt=40.0,
            hex_color=theme.text,
        )
    )

    overflow: list[str] = []
    shown = _visible_body_count_without_orphan(len(slide.body), 1)
    for i, line in enumerate(slide.body[:shown]):
        sub_top = title_top + title_h + int(i * 320_040)
        els.append(
            Element(
                id=_uid(),
                kind="text",
                left=_MARGIN,
                top=sub_top,
                width=_CW,
                height=320_040,
                text=line,
                font_name=_first_font(theme.font_reading),
                font_size_pt=18.0,
                hex_color=theme.text_muted,
                italic=True,
            )
        )
    if slide.body:
        overflow = slide.body[shown:]

    return els, overflow


def _layout_bullets(slide: AuthoredSlide, theme: Theme) -> tuple[list[Element], list[str]]:
    """Standard title + bullet-list layout."""
    els: list[Element] = []

    # Title
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
            font_size_pt=32.0,
            hex_color=theme.text,
            bold=True,
        )
    )

    # Accent bar under title
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

    # Bullets with _fit_text overflow control
    body_top = bar_top + _BAR_H + _GAP
    body_h = _SLIDE_H - body_top - _MARGIN
    body_w = _CW - _BODY_INDENT

    font_pt, fitted, overflow = _fit_text(slide.body, box_width=body_w, box_height=body_h)

    for i, bullet in enumerate(fitted):
        line_h = font_pt * 12_700 * 1.3
        bullet_top = body_top + int(i * line_h)
        bullet_text = f"• {bullet}"
        els.append(
            Element(
                id=_uid(),
                kind="text",
                left=_MARGIN + _BODY_INDENT,
                top=bullet_top,
                width=body_w,
                height=int(line_h * 1.15),
                text=bullet_text,
                font_name=_first_font(theme.font_reading),
                font_size_pt=font_pt,
                hex_color=theme.text,
                word_wrap=True,
            )
        )

    return els, overflow


def _layout_closing(slide: AuthoredSlide, theme: Theme) -> tuple[list[Element], list[str]]:
    """Closing/thank-you slide: large centred title + optional subtitle."""
    els: list[Element] = []

    v_center = int(_SLIDE_H * 0.42)

    els.append(
        Element(
            id=_uid(),
            kind="accent_bar",
            left=_MARGIN,
            top=v_center - _BAR_H - 182_880,
            width=2_286_000,
            height=_BAR_H,
            fill_hex=theme.accent,
        )
    )
    els.append(
        Element(
            id=_uid(),
            kind="text",
            left=_MARGIN,
            top=v_center,
            width=_CW,
            height=1_371_600,
            text=slide.title,
            font_name=_first_font(theme.font_display),
            font_size_pt=48.0,
            hex_color=theme.text,
            align="LEFT",
        )
    )

    overflow: list[str] = []
    shown = _visible_body_count_without_orphan(len(slide.body), 1)
    for i, line in enumerate(slide.body[:shown]):
        sub_top = v_center + 1_371_600 + _GAP + int(i * 342_900)
        els.append(
            Element(
                id=_uid(),
                kind="text",
                left=_MARGIN,
                top=sub_top,
                width=_CW,
                height=342_900,
                text=line,
                font_name=_first_font(theme.font_reading),
                font_size_pt=22.0,
                hex_color=theme.text_muted,
                italic=True,
            )
        )
    if slide.body:
        overflow = slide.body[shown:]

    return els, overflow
