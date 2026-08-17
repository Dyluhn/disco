"""Per-layout functions: image_right, image_left, full_image.

Extracted from ``_deck_schema.py`` to reduce module size; the public facade
re-imports these names unchanged. Each layout fn is ≤ 200 LOC (arch budget).
"""

from __future__ import annotations

from disco.core.brand.tokens import Theme

from ._authoring_schema import AuthoredSlide
from ._geometry import _BAR_H, _BODY_INDENT, _CH, _CW, _GAP, _MARGIN, _SLIDE_H, _SLIDE_W, _TITLE_H
from ._helpers import _first_font, _uid
from ._precise_schema import Element
from ._text_fit import _fit_text, _visible_body_count_without_orphan


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
    els.append(
        Element(
            id=_uid(),
            kind="text",
            left=text_left,
            top=_MARGIN,
            width=half_w,
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
            left=text_left,
            top=bar_top,
            width=half_w,
            height=_BAR_H,
            fill_hex=theme.accent,
        )
    )

    # Bullets
    body_top = bar_top + _BAR_H + _GAP
    body_h = _SLIDE_H - body_top - _MARGIN
    body_w = half_w - _BODY_INDENT

    font_pt, fitted, overflow = _fit_text(slide.body, box_width=body_w, box_height=body_h)

    for i, bullet in enumerate(fitted):
        line_h = font_pt * 12_700 * 1.3
        els.append(
            Element(
                id=_uid(),
                kind="text",
                left=text_left + _BODY_INDENT,
                top=body_top + int(i * line_h),
                width=body_w,
                height=int(line_h * 1.15),
                text=f"• {bullet}",
                font_name=_first_font(theme.font_reading),
                font_size_pt=font_pt,
                hex_color=theme.text,
                word_wrap=True,
            )
        )

    img_w = _CW - half_w - 182_880
    els.append(
        Element(
            id=_uid(),
            kind="image",
            left=img_left,
            top=_MARGIN,
            width=img_w,
            height=_CH,
            image_prompt=slide.image_prompt,
            fill_hex=theme.surface_2,
            border_hex=theme.hairline,
        )
    )

    return els, overflow


def _layout_image_left(slide: AuthoredSlide, theme: Theme) -> tuple[list[Element], list[str]]:
    """Convenience wrapper: image on the LEFT half."""
    return _layout_image_right(slide, theme, image_left=True)


def _layout_full_image(slide: AuthoredSlide, theme: Theme) -> tuple[list[Element], list[str]]:
    """Full-bleed image slide.  body=[] per verdict; no overflow."""
    els: list[Element] = []

    # Full-bleed image (or placeholder)
    els.append(
        Element(
            id=_uid(),
            kind="image",
            left=0,
            top=0,
            width=_SLIDE_W,
            height=_SLIDE_H,
            image_prompt=slide.image_prompt,
            fill_hex=theme.surface_2,
        )
    )

    # Bottom band: title stacked ABOVE the caption with REAL height accounting.
    # Gauntlet s-arxiv: a two-line wrapped title overprinted the caption because
    # both boxes were independently bottom-anchored ("letters stacked on
    # letters"). Estimate the title's wrapped line count from its length and
    # stack the boxes; the scrim is sized from the same numbers, so the white
    # type is always on the dark band regardless of artwork or title length.
    overflow: list[str] = []
    shown = _visible_body_count_without_orphan(len(slide.body), 1)
    caption_line_h = 274_320  # one 14pt italic line
    caption_pad = 137_160  # breathing room under the title block
    caption_block_h = (caption_line_h * max(0, shown) + caption_pad) if slide.body else 0
    _TITLE_CHARS_PER_LINE = 46  # 36pt bold across _CW — conservative
    title_lines = max(1, -(-len(slide.title) // _TITLE_CHARS_PER_LINE)) if slide.title else 0
    title_line_h = 548_640  # 36pt line + leading
    title_block_h = title_lines * title_line_h

    if slide.title or slide.body:
        scrim_h = title_block_h + caption_block_h + _MARGIN + 137_160
        els.append(
            Element(
                id=_uid(),
                kind="rect",
                left=0,
                top=_SLIDE_H - scrim_h,
                width=_SLIDE_W,
                height=scrim_h,
                fill_hex="#111111",
            )
        )

    # Title block sits directly above the caption block (both above margin).
    if slide.title:
        els.append(
            Element(
                id=_uid(),
                kind="text",
                left=_MARGIN,
                top=_SLIDE_H - _MARGIN - caption_block_h - title_block_h,
                width=_CW,
                height=title_block_h,
                text=slide.title,
                font_name=_first_font(theme.font_display),
                font_size_pt=36.0,
                hex_color="#ffffff",  # white overlay (scrim-backed above)
                bold=True,
            )
        )

    caption_top = _SLIDE_H - _MARGIN - (caption_line_h * max(0, shown))
    for i, line in enumerate(slide.body[:shown]):
        els.append(
            Element(
                id=_uid(),
                kind="text",
                left=_MARGIN,
                top=caption_top + i * caption_line_h,
                width=_CW,
                height=caption_line_h * 2,  # wrap room, next line is band
                text=line,
                font_name=_first_font(theme.font_reading),
                font_size_pt=14.0,
                hex_color="#e0e0e0",
                italic=True,
            )
        )
    if slide.body:
        overflow = slide.body[shown:]

    return els, overflow
