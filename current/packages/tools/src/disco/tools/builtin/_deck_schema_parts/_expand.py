"""``_expand_slides`` — deterministic overflow expansion shared by the render
path (``lower_deck``) and the editor path (``lower_deck_for_editor``) so their
slide counts are guaranteed identical (BW-13).

Extracted from ``_deck_schema.py`` to reduce module size; the public facade
re-imports these names unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass

from disco.core.brand.tokens import Theme

from ._authoring_schema import AuthoredSlide, LayoutHint
from ._layout_dispatch import _body_partition
from ._layout_infer import _infer_layout

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

    aslide: AuthoredSlide  # original slide OR a synthesized "_cont" fragment
    layout: LayoutHint  # resolved ONCE here (image-side alternation included)
    render_body: list[str]  # body lines this fragment shows, in render order
    body_index_map: list[int]  # original-body index of each render_body line
    orig_index: int  # index into authored.slides (image bytes + pointer base)
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

    def _walk(aslide: AuthoredSlide, depth: int, orig_index: int, index_map: list[int]) -> None:
        if depth > _MAX_CONT_DEPTH:
            return  # guard: discard overflow beyond depth cap
        layout = _infer_layout(aslide, image_alt)
        rendered_pos, overflow_pos = _body_partition(aslide, layout, theme)
        eff.append(
            _EffSlide(
                aslide=aslide,
                layout=layout,
                render_body=[aslide.body[p] for p in rendered_pos],
                body_index_map=[index_map[p] for p in rendered_pos],
                orig_index=orig_index,
                is_cont=depth > 0,
            )
        )
        if overflow_pos:
            # Spilled lines, in overflow order, with their ORIGINAL indices — same
            # content the export's layout fn drops (determinism preserved).
            cont = AuthoredSlide(
                type=f"{aslide.type}_cont",
                title=f"{aslide.title} (cont.)",
                body=[aslide.body[p] for p in overflow_pos],
                layout_hint="bullets",  # FORCE bullets — never map back to title/section/closing
                notes=None,  # notes stay on the original slide
            )
            _walk(cont, depth + 1, orig_index, [index_map[p] for p in overflow_pos])

    for i, aslide in enumerate(slides):
        _walk(aslide, depth=0, orig_index=i, index_map=list(range(len(aslide.body))))
    return eff
