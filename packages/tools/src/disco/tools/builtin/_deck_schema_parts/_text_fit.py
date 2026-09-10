"""``_fit_text`` — overflow control (font step-down + continuation split).

Extracted from ``_deck_schema.py`` to reduce module size; the public facade
re-imports these names unchanged.
"""

from __future__ import annotations

from ._geometry import _BODY_INDENT, _CW

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

    overflow = lines[len(fitted) :]
    if 0 < len(overflow) < 2:
        return font, lines, []
    return font, fitted, overflow


def _visible_body_count_without_orphan(total: int, preferred: int) -> int:
    """Return the number of body lines to show before splitting.

    Sparse one-line continuations read as accidental orphan slides. If the normal
    split would leave exactly one tail line, keep that tail on the current slide
    and let the rendered density relax.
    """
    if total <= preferred:
        return total
    overflow = total - preferred
    if overflow < 2:
        return total
    return preferred
