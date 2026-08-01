"""``lower_deck_for_editor`` — produce a LoweredDeck from an AuthoredDeck.

Extracted from ``_deck_schema.py`` to reduce module size; the public facade
re-imports these names unchanged.

``lower_deck_for_editor`` was split into one helper per element kind it emits
(title / subtitle / body bullets / chart / table / image_prompt) purely to stay
under the callable logical-line cap — the computation, element order, and every
``element_id`` / ``json_pointer`` string are unchanged from the original single
function.
"""

from __future__ import annotations

from disco.core.brand import resolve_theme
from disco.core.brand.tokens import Theme

from ._authoring_schema import AuthoredDeck, AuthoredSlide
from ._editor_schema import ElementGeometry, LoweredDeck, LoweredElement, LoweredSlide
from ._expand import _EffSlide, _expand_slides
from ._geometry import (
    _BODY_H,
    _BODY_INDENT,
    _BODY_TOP,
    _CW,
    _MARGIN,
    _SLIDE_H,
    _SLIDE_W,
    _TITLE_H,
)
from ._helpers import _parse_theme

# Percentage constants derived from the EMU layout (author → editor geometry)
_PCT = 100.0

# Title strip: left=4%, top=6%, width=92%, height=13%
_E_TITLE_LEFT = round(_MARGIN / _SLIDE_W * _PCT, 2)  # ≈ 3.75
_E_TITLE_TOP = round(_MARGIN / _SLIDE_H * _PCT, 2)  # ≈ 6.67
_E_TITLE_W = round(_CW / _SLIDE_W * _PCT, 2)  # ≈ 92.5
_E_TITLE_H = round(_TITLE_H / _SLIDE_H * _PCT, 2)  # ≈ 13.33

# Body area: below title+bar
_E_BODY_TOP = round(_BODY_TOP / _SLIDE_H * _PCT, 2)  # ≈ 26.7
_E_BODY_H = round(_BODY_H / _SLIDE_H * _PCT, 2)  # ≈ 66.7
_E_BODY_W = round((_CW - _BODY_INDENT) / _SLIDE_W * _PCT, 2)  # ≈ 88.6
_E_BODY_LEFT = round((_MARGIN + _BODY_INDENT) / _SLIDE_W * _PCT, 2)  # ≈ 5.6

# Per-bullet height: ~8% of slide
_E_BULLET_H = 8.0


def _add_element(
    elements: list[LoweredElement],
    slide_id: str,
    kind: str,
    content: str,
    x: float,
    y: float,
    w: float,
    h: float,
    fsz: float,
    fw: str,
    fi: str,
    jptr: str,
) -> LoweredElement:
    """Build + append one LoweredElement, mirroring the original nested ``_add``
    closure exactly (including its element_id derivation, which is the same
    ``f"{slide_id}:{kind}"`` string regardless of whether ``kind`` has a colon)."""
    element = LoweredElement(
        element_id=(f"{slide_id}:{kind}" if ":" not in kind else f"{slide_id}:{kind}"),
        slide_id=slide_id,
        kind=kind.split(":")[-1] if ":" in kind else kind,
        content=content,
        geometry=ElementGeometry(x=x, y=y, w=w, h=h),
        font_size_vw=fsz,
        font_weight=fw,
        font_style=fi,
        json_pointer=jptr,
    )
    elements.append(element)
    return element


def _editor_title_element(
    elements: list[LoweredElement], slide_id: str, aslide: AuthoredSlide, orig: int
) -> None:
    """Title element (always present).  Continuation fragments point back at
    the ORIGINAL slide's title (their "(cont.)" suffix is display-only)."""
    el = _add_element(
        elements,
        slide_id,
        kind="title",
        content=aslide.title,
        x=_E_TITLE_LEFT,
        y=_E_TITLE_TOP,
        w=_E_TITLE_W,
        h=_E_TITLE_H,
        fsz=2.5,
        fw="bold",
        fi="normal",
        jptr=f"/slides/{orig}/title",
    )
    el.element_id = f"{slide_id}:title"


def _editor_subtitle_element(
    elements: list[LoweredElement],
    slide_id: str,
    layout: str,
    render_body: list[str],
    orig: int,
    body_index_map: list[int],
) -> int:
    """Subtitle (first body line on title/section/closing slides).  Its real
    authored index comes from body_index_map (NOT a contiguous offset).
    Returns the body_start offset for the bullet-body step below."""
    if layout in ("title", "section_header", "closing") and render_body:
        sub_top = _E_TITLE_TOP + _E_TITLE_H + 2.0
        el = _add_element(
            elements,
            slide_id,
            kind="subtitle",
            content=render_body[0],
            x=_E_TITLE_LEFT,
            y=sub_top,
            w=_E_TITLE_W,
            h=_E_BULLET_H,
            fsz=1.8,
            fw="normal",
            fi="italic",
            jptr=f"/slides/{orig}/body/{body_index_map[0]}",
        )
        el.element_id = f"{slide_id}:subtitle"
        return 1
    return 0


def _editor_body_elements(
    elements: list[LoweredElement],
    slide_id: str,
    render_body: list[str],
    body_start: int,
    orig: int,
    body_index_map: list[int],
) -> None:
    """Bullet body lines — only the lines that belong to THIS fragment.  Each
    rendered line maps back to its ORIGINAL authored index via body_index_map,
    so NON-suffix (column) overflow points correctly (BW-13)."""
    body_y = _E_BODY_TOP
    for local_j in range(body_start, len(render_body)):
        line = render_body[local_j]
        orig_bj = body_index_map[local_j]
        content_stripped = line.lstrip("• ")
        el = _add_element(
            elements,
            slide_id,
            kind=f"body:{orig_bj}",
            content=content_stripped,
            x=_E_BODY_LEFT,
            y=body_y,
            w=_E_BODY_W,
            h=_E_BULLET_H,
            fsz=1.6,
            fw="normal",
            fi="normal",
            jptr=f"/slides/{orig}/body/{orig_bj}",
        )
        el.element_id = f"{slide_id}:body:{orig_bj}"
        el.kind = "bullet"
        body_y = min(body_y + _E_BULLET_H + 1.0, 90.0)


def _editor_chart_element(
    elements: list[LoweredElement], slide_id: str, aslide: AuthoredSlide, orig: int
) -> None:
    """Chart placeholder element (originals only — continuations are text-only)."""
    if aslide.chart is None:
        return
    chart_desc = f"[{aslide.chart.kind} chart] {aslide.chart.title}"
    el = _add_element(
        elements,
        slide_id,
        kind="chart",
        content=chart_desc,
        x=_E_TITLE_LEFT,
        y=_E_BODY_TOP,
        w=_E_TITLE_W,
        h=55.0,
        fsz=1.4,
        fw="normal",
        fi="normal",
        jptr=f"/slides/{orig}/chart",
    )
    el.element_id = f"{slide_id}:chart"


def _editor_table_element(
    elements: list[LoweredElement], slide_id: str, aslide: AuthoredSlide, orig: int
) -> None:
    """Table placeholder element."""
    if aslide.table is None:
        return
    table_desc = f"[table] {' | '.join(aslide.table.headers[:4])}"
    el = _add_element(
        elements,
        slide_id,
        kind="table",
        content=table_desc,
        x=_E_TITLE_LEFT,
        y=_E_BODY_TOP,
        w=_E_TITLE_W,
        h=55.0,
        fsz=1.4,
        fw="normal",
        fi="normal",
        jptr=f"/slides/{orig}/table",
    )
    el.element_id = f"{slide_id}:table"


def _editor_image_prompt_element(
    elements: list[LoweredElement], slide_id: str, aslide: AuthoredSlide, orig: int
) -> None:
    """Image prompt placeholder element."""
    if aslide.image_prompt is None:
        return
    el = _add_element(
        elements,
        slide_id,
        kind="image_prompt",
        content=aslide.image_prompt,
        x=_E_TITLE_LEFT,
        y=_E_BODY_TOP,
        w=_E_TITLE_W,
        h=55.0,
        fsz=1.4,
        fw="normal",
        fi="normal",
        jptr=f"/slides/{orig}/image_prompt",
    )
    el.element_id = f"{slide_id}:image_prompt"


def _lower_editor_slide(si: int, eff: _EffSlide, theme: Theme) -> LoweredSlide:
    """Build one LoweredSlide from one expanded ``_EffSlide`` fragment."""
    aslide = eff.aslide
    layout = eff.layout
    orig = eff.orig_index
    render_body = eff.render_body
    body_index_map = eff.body_index_map
    slide_id = f"slide-{si}"
    bg_color = theme.surface_1 if layout == "section_header" else theme.bg
    elements: list[LoweredElement] = []

    _editor_title_element(elements, slide_id, aslide, orig)
    body_start = _editor_subtitle_element(
        elements, slide_id, layout, render_body, orig, body_index_map
    )
    _editor_body_elements(elements, slide_id, render_body, body_start, orig, body_index_map)
    _editor_chart_element(elements, slide_id, aslide, orig)
    _editor_table_element(elements, slide_id, aslide, orig)
    _editor_image_prompt_element(elements, slide_id, aslide, orig)

    return LoweredSlide(
        slide_id=slide_id,
        slide_idx=si,
        layout=layout,
        bg_color=bg_color,
        elements=elements,
    )


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

    lslides = [_lower_editor_slide(si, eff, theme) for si, eff in enumerate(eff_slides)]

    return LoweredDeck(
        title=authored.title,
        theme_name=theme_name,
        theme_mode=theme_mode,
        slides=lslides,
    )
