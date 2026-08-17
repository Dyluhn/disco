"""Layout function dispatch table + ``_body_partition`` (which body positions a
layout renders vs. spills).

Extracted from ``_deck_schema.py`` to reduce module size; the public facade
re-imports these names unchanged.
"""

from __future__ import annotations

from disco.core.brand.tokens import Theme

from ._authoring_schema import AuthoredSlide, LayoutHint
from ._geometry import _CMP_CONTENT_H, _COL_W, _TWO_COL_H
from ._layouts_basic import _layout_bullets, _layout_closing, _layout_section_header, _layout_title
from ._layouts_columns import _layout_comparison, _layout_two_column
from ._layouts_image import _layout_full_image, _layout_image_left, _layout_image_right
from ._text_fit import _fit_text

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
        rendered = [p for p in (0, 1) if p < n]  # labels
        rendered += list(range(2, 2 + kl))  # left content head
        rendered += list(range(2 + mid, 2 + mid + kr))  # right content head
        overflow = list(range(2 + kl, 2 + mid))  # left content tail
        overflow += list(range(2 + mid + kr, n))  # right content tail
        return rendered, overflow

    # Contiguous-suffix layouts (bullets / image_* / title / section_header /
    # closing / full_image / metrics / table): overflow is the body tail.
    layout_fn = _LAYOUT_FNS.get(layout, _layout_bullets)
    _els, overflow_lines = layout_fn(aslide, theme)  # type: ignore[operator]
    k = n - len(overflow_lines)
    return list(range(0, k)), list(range(k, n))
