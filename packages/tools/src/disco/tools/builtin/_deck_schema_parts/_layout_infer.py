"""``_infer_layout`` — total deterministic mapping (never returns None).

Extracted from ``_deck_schema.py`` to reduce module size; the public facade
re-imports these names unchanged.
"""

from __future__ import annotations

from ._authoring_schema import AuthoredSlide, LayoutHint


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
