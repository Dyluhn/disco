"""HTML-side slide-archetype + layout renderers, and the per-slide HTML dispatcher.

Moved out of ``_pptx_render.py``. ``_html_for_c1_slide`` was, pre-split, one
180-line / McCabe-48 if/elif chain dispatching on slide archetype/layout. Each
archetype/layout branch's body is now its own small helper
(``_html_for_archetype``, ``_html_title_style_layout``,
``_html_section_header_layout``, ``_html_two_column_layout``,
``_html_default_bullets_layout``) — pure extract-function decomposition, same
call sequence and same string literals as before, just named and reachable on
their own. ``_html_for_c1_slide`` itself is now a slim dispatcher that picks
the right helper.
"""

from __future__ import annotations

import html
from collections.abc import Callable

from disco.core.brand.tokens import Theme

from .._deck_schema import Element, Slide
from ._slide_archetype_shared import (
    _plain_element_text,
    _slide_archetype,
    _split_timeline_label,
    _title_element,
)

BodyEidFn = Callable[[int], str]


def _html_body_text(el: Element) -> str:
    return html.escape(_plain_element_text(el))


def _html_big_number(
    sid: str,
    title_html: str,
    title_el: Element | None,
    body: list[Element],
    body_eid: BodyEidFn,
) -> str:
    figure_el = body[0] if body else title_el
    support_el = body[1] if len(body) > 1 else None
    figure = (
        f'<div class="slide-big-number-figure" data-element-id="{body_eid(0)}" '
        f'data-slide-id="{sid}">{_html_body_text(figure_el)}</div>'
        if figure_el is not None
        else ""
    )
    support = (
        f'<p class="slide-big-number-support" data-element-id="{body_eid(1)}" '
        f'data-slide-id="{sid}">{_html_body_text(support_el)}</p>'
        if support_el is not None
        else ""
    )
    return f'<div class="slide-big-number-layout">{title_html}{figure}{support}</div>'


def _html_quote(
    sid: str,
    title_el: Element | None,
    body: list[Element],
    body_eid: BodyEidFn,
) -> str:
    quote_el = body[0] if body else title_el
    attr_el = body[1] if len(body) > 1 else None
    quote = (
        f'<blockquote class="slide-quote-text" data-element-id="{body_eid(0)}" '
        f'data-slide-id="{sid}">{_html_body_text(quote_el)}</blockquote>'
        if quote_el is not None
        else ""
    )
    attribution = (
        f'<figcaption class="slide-quote-attribution" data-element-id="{body_eid(1)}" '
        f'data-slide-id="{sid}">{_html_body_text(attr_el)}</figcaption>'
        if attr_el is not None
        else ""
    )
    return (
        '<figure class="slide-quote-layout">'
        '<div class="slide-quote-mark" aria-hidden="true">“</div>'
        f"{quote}{attribution}"
        "</figure>"
    )


def _html_timeline(
    sid: str,
    title_html: str,
    title_el: Element | None,
    body: list[Element],
    body_eid: BodyEidFn,
) -> str:
    nodes: list[str] = []
    labels: list[str] = []
    items = body or ([title_el] if title_el is not None else [])
    denom = max(1, len(items) - 1)
    for j, el in enumerate(items):
        x = 8.0 + (j * (100.0 - 16.0) / denom if len(items) > 1 else 42.0)
        head, detail = _split_timeline_label(_plain_element_text(el))
        label_cls = "above" if j % 2 == 0 else "below"
        nodes.append(f'<div class="slide-timeline-node" style="left:{x:.2f}%"></div>')
        labels.append(
            f'<div class="slide-timeline-label {label_cls}" style="left:{x:.2f}%" '
            f'data-element-id="{body_eid(j)}" data-slide-id="{sid}">'
            f"<strong>{html.escape(head)}</strong>{html.escape(detail)}</div>"
        )
    return (
        '<div class="slide-timeline-layout">'
        f'{title_html}<div class="slide-rule"></div>'
        '<div class="slide-timeline">'
        '<div class="slide-timeline-spine"></div>'
        f"{''.join(nodes)}{''.join(labels)}"
        "</div></div>"
    )


def _html_two_by_two(
    sid: str,
    title_html: str,
    body: list[Element],
    body_eid: BodyEidFn,
) -> str:
    # Axis labels ONLY when the model authored them (6+ body lines: x, y, 4 cells).
    # Same no-false-affordances rule as the PPTX path: never invent an analytical
    # framing ("Higher impact/certainty") the quadrants don't actually plot.
    axis_x: str | None = None
    axis_y: str | None = None
    cells = body[:4]
    if len(body) >= 6:
        axis_x = _plain_element_text(body[0])
        axis_y = _plain_element_text(body[1])
        cells = body[2:6]
    cell_html: list[str] = []
    for j in range(4):
        text = _html_body_text(cells[j]) if j < len(cells) else ""
        body_pos = j + (2 if len(body) >= 6 else 0)
        cell_html.append(
            f'<div class="slide-two-by-two-cell" data-quadrant="{j + 1}" '
            f'data-element-id="{body_eid(body_pos)}" data-slide-id="{sid}">{text}</div>'
        )
    axis_html = ""
    if axis_x:
        axis_html += f'<div class="slide-two-by-two-axis x-end">{html.escape(axis_x)}</div>'
    if axis_y:
        axis_html += f'<div class="slide-two-by-two-axis y-end">{html.escape(axis_y)}</div>'
    return (
        '<div class="slide-two-by-two-layout">'
        f'{title_html}<div class="slide-rule"></div>'
        '<div class="slide-two-by-two-grid">'
        f"{axis_html}"
        f"{''.join(cell_html)}"
        "</div></div>"
    )


def _img_src(el: Element) -> str | None:
    """C7 wire: the HTML <img src> for an image Element — an inline data-URI from
    the generated bytes (sandbox-agnostic, no extra HTTP fetch), else the stored
    path, else None (→ the caller renders the [image] placeholder)."""
    if el.image_bytes:
        import base64

        sig = el.image_bytes[:3]
        mime = "image/jpeg" if sig == b"\xff\xd8\xff" else "image/png"
        b64 = base64.b64encode(el.image_bytes).decode("ascii")
        return f"data:{mime};base64,{b64}"
    if el.image_path:
        return html.escape(el.image_path)
    return None


def _art_fallback_svg(variant: int = 0, *, style: str = "") -> str:
    """Inline-SVG generative-art fallback for an empty image slot (the HTML
    analogue of the PPTX `_image_placeholder` shape composition). Uses the
    deck's CSS custom properties so it re-themes with the template. Dylan
    2026-07-07: 'if image gen doesn't work then generate SVGs'."""
    v = variant % 3
    if v == 0:
        art = (
            '<circle cx="66" cy="30" r="24" fill="var(--surface-2)"/>'
            '<circle cx="66" cy="30" r="30" fill="none" '
            'stroke="var(--hairline-strong)" stroke-width="0.6"/>'
            '<circle cx="25" cy="68" r="4" fill="var(--accent)"/>'
            '<rect x="12" y="82" width="34" height="1.2" fill="var(--hairline-strong)"/>'
        )
    elif v == 1:
        art = (
            '<rect x="0" y="62" width="100" height="5" fill="var(--surface-2)"/>'
            '<circle cx="33" cy="62" r="12" fill="var(--accent)"/>'
            '<circle cx="80" cy="24" r="7" fill="var(--surface-2)"/>'
        )
    else:
        cols = "".join(
            f'<rect x="{22 * i + 11}" y="20" width="6" height="56" fill="var(--surface-2)"/>'
            for i in range(4)
        )
        art = cols + '<rect x="74" y="64" width="9" height="9" fill="var(--accent)"/>'
    return (
        f'<svg viewBox="0 0 100 100" preserveAspectRatio="xMidYMid slice" '
        f'aria-hidden="true" style="background:var(--surface-1);{style}">{art}</svg>'
    )


def _html_image_layout(
    layout: str,
    texts_sorted: list[Element],
    images: list[Element],
    title_tag: Callable[..., str],
    bullet_li: Callable[[Element, int], str],
) -> str:
    title_el = texts_sorted[0] if texts_sorted else None
    body_els = texts_sorted[1:]
    title_html = title_tag(title_el, "slide-heading") if title_el else ""
    bullet_items = "".join(bullet_li(el, j) for j, el in enumerate(body_els))
    bullets_html = f'<ul class="slide-bullets">{bullet_items}</ul>' if bullet_items else ""

    if layout == "full_image":
        full_src = _img_src(images[0]) if images else None
        if full_src is None:
            # UI-24: no image provider configured -> there IS no art. Painting the
            # generative-SVG fallback under the scrim put small white type over a
            # pale grey wash (unreadable in the light theme). With art absent, drop
            # the art + scrim entirely and render the same legible text-only
            # composition the `title` layout uses: the slide's own solid ground,
            # a real 4vw title, and body copy at full theme contrast.
            title_only = title_tag(title_el, "slide-title", "h1") if title_el else ""
            return f'<div class="slide-title-bar"></div>\n{title_only}\n{bullets_html}'
        return (
            '<div class="slide-full-image-wrap">'
            f'<img class="slide-full-image-bg" src="{full_src}" alt="">'
            '<div class="slide-image-scrim" aria-hidden="true"></div>'
            f'<div class="slide-full-image-copy">{title_html}{bullets_html}</div>'
            "</div>"
        )

    img_html = ""
    if images:
        img_src = _img_src(images[0])
        if img_src:
            img_html = (
                f'<img src="{img_src}" alt="" style="max-width:48%;max-height:90%;'
                'object-fit:contain;">'
            )
        else:
            img_html = _art_fallback_svg(
                2, style="width:46%;height:80%;border:1px solid var(--hairline);"
            )

    text_div = (
        '<div style="flex:1;display:flex;flex-direction:column;">'
        f'{title_html}<div class="slide-rule"></div>{bullets_html}'
        "</div>"
    )
    if layout == "image_left":
        return (
            '<div style="display:flex;gap:4%;width:100%;height:100%;align-items:center;">'
            f"{img_html}{text_div}</div>"
        )
    return (
        '<div style="display:flex;gap:4%;width:100%;height:100%;align-items:center;">'
        f"{text_div}{img_html}</div>"
    )


def _html_for_archetype(
    archetype: str,
    sid: str,
    archetype_title: Element | None,
    archetype_body: list[Element],
    title_tag: Callable[..., str],
    body_eid: BodyEidFn,
) -> str | None:
    """The big_number/quote/timeline/two_by_two branch of the C1 slide dispatcher.

    Returns ``None`` when *archetype* matches none of the four (the caller then
    falls through to the layout-keyed branches).
    """
    if archetype == "big_number":
        title_html = title_tag(archetype_title, "slide-kicker", "p") if archetype_title else ""
        return _html_big_number(sid, title_html, archetype_title, archetype_body, body_eid)

    if archetype == "quote":
        return _html_quote(sid, archetype_title, archetype_body, body_eid)

    if archetype == "timeline":
        title_html = title_tag(archetype_title, "slide-heading") if archetype_title else ""
        return _html_timeline(sid, title_html, archetype_title, archetype_body, body_eid)

    if archetype == "two_by_two":
        title_html = title_tag(archetype_title, "slide-heading") if archetype_title else ""
        return _html_two_by_two(sid, title_html, archetype_body, body_eid)

    return None


def _html_title_style_layout(
    texts_sorted: list[Element],
    title_tag: Callable[..., str],
    sub_tag: Callable[[Element, str], str],
) -> str:
    """The ``title``/``closing`` layout body — identical markup for both, since
    both are a title-bar + big title + stacked subtitles."""
    primary = texts_sorted[0] if texts_sorted else None
    rest = texts_sorted[1:]
    title_html = title_tag(primary, "slide-title", "h1") if primary else ""
    sub_html = "\n".join(sub_tag(el, "slide-subtitle") for el in rest)
    return f'<div class="slide-title-bar"></div>\n{title_html}\n{sub_html}'


def _html_section_header_layout(
    texts: list[Element],
    title_tag: Callable[..., str],
    sub_tag: Callable[[Element, str], str],
) -> str:
    kicker_texts = [t for t in texts if t.font_size_pt <= 12]
    head_texts = [t for t in texts if t.font_size_pt > 12]
    kicker_sorted = sorted(kicker_texts, key=lambda e: e.font_size_pt)
    head_sorted = sorted(head_texts, key=lambda e: e.font_size_pt, reverse=True)
    kicker = (
        f'<p class="slide-section-kicker">{html.escape(kicker_sorted[0].text)}</p>'
        if kicker_sorted
        else ""
    )
    title = title_tag(head_sorted[0], "slide-section-title", "h2") if head_sorted else ""
    sub = "\n".join(sub_tag(el, "slide-subtitle") for el in head_sorted[1:])
    return f'{kicker}<div class="slide-title-bar"></div>\n{title}\n{sub}'


def _html_two_column_layout(
    texts: list[Element],
    texts_sorted: list[Element],
    title_tag: Callable[..., str],
    bullet_li: Callable[[Element, int], str],
) -> str:
    title_el = next((t for t in texts_sorted if t.bold and t.font_size_pt > 20), None)
    body_els = [t for t in texts if t is not title_el]
    mid = max(1, len(body_els) // 2)
    left_els = body_els[:mid]
    right_els = body_els[mid:]
    title_html = title_tag(title_el, "slide-heading") if title_el else ""
    left_items = "".join(bullet_li(e, j) for j, e in enumerate(left_els))
    right_items = "".join(bullet_li(e, j + len(left_els)) for j, e in enumerate(right_els))
    left_ul = f'<ul class="slide-bullets">{left_items}</ul>'
    right_ul = f'<ul class="slide-bullets">{right_items}</ul>'
    return (
        f'{title_html}<div class="slide-rule"></div>'
        f'<div style="display:flex;gap:4%;width:100%;">'
        f'<div style="flex:1">{left_ul}</div>'
        f'<div style="flex:1">{right_ul}</div>'
        f"</div>"
    )


def _html_default_bullets_layout(
    texts_sorted: list[Element],
    title_tag: Callable[..., str],
    bullet_li: Callable[[Element, int], str],
) -> str:
    title_el = texts_sorted[0] if texts_sorted else None
    body_els = texts_sorted[1:] if len(texts_sorted) > 1 else []
    title_html = title_tag(title_el, "slide-heading") if title_el else ""
    bullet_items = "".join(bullet_li(el, j) for j, el in enumerate(body_els))
    bullets_html = f'<ul class="slide-bullets">{bullet_items}</ul>' if bullet_items else ""
    return f'{title_html}\n<div class="slide-rule"></div>\n{bullets_html}'


def _html_for_c1_slide(slide: Slide, theme: Theme, *, slide_idx: int = 0) -> str:
    """Return inner HTML for a C1 Slide by extracting its text Elements.

    ``slide_idx`` is the 0-based position in the deck; used to stamp
    ``data-element-id`` attributes so the SelectionOverlay + deckResolver can
    identify elements in the rendered HTML.
    """
    sid = f"slide-{slide_idx}"

    # C8: chart/table slides delegate to specialised HTML generators. The wrapper
    # carries ONLY data-slide-id (slide nav) — NOT a data-element-id. The whole
    # chart/table is not an editable TEXT element; stamping it `{sid}:title` made the
    # in-app editor overlay a title-edit box over the entire chart (a false affordance
    # writing /slides/N/title). Charts/tables are edited via their own tools.
    if slide.chart is not None:
        from disco.tools.builtin._c8_chart_layouts import html_chart_content

        inner = html_chart_content(slide.title, slide.chart, theme)
        return f'<div data-slide-id="{sid}">{inner}</div>'
    if slide.table is not None:
        from disco.tools.builtin._c8_chart_layouts import html_table_content

        inner = html_table_content(slide.title, slide.table, theme)
        return f'<div data-slide-id="{sid}">{inner}</div>'

    # Separate text and image elements
    texts = [el for el in slide.elements if el.kind == "text"]
    images = [el for el in slide.elements if el.kind == "image"]

    if not texts and not images:
        return ""

    # Sort text by font size desc (largest = most prominent = title)
    texts_sorted = sorted(texts, key=lambda e: e.font_size_pt, reverse=True)

    layout = slide.layout

    def _title_tag(el: Element, cls: str, tag: str = "h2") -> str:
        eid = f"{sid}:title"
        return (
            f'<{tag} class="{cls}" '
            f'data-element-id="{eid}" data-slide-id="{sid}">'
            f"{html.escape(el.text)}</{tag}>"
        )

    def _sub_tag(el: Element, cls: str) -> str:
        eid = f"{sid}:subtitle"
        return (
            f'<p class="{cls}" '
            f'data-element-id="{eid}" data-slide-id="{sid}">'
            f"{html.escape(el.text)}</p>"
        )

    # BW-13: map a rendered bullet's POSITION (its index among this slide's body
    # lines, in render order) back to its ORIGINAL authored-body index via the
    # slide's body_index_map — the SAME mapping lower_deck_for_editor uses. This
    # keeps the rendered data-element-id ("{sid}:body:{orig}") aligned 1:1 with the
    # editor pointer model for overflow / continuation / non-contiguous column spill
    # (where render position != authored index). Empty map (hand-built / MinimalDeck
    # Slide) → identity, so simple decks render byte-identically.
    body_index_map: list[int] = getattr(slide, "body_index_map", None) or []

    def _orig_bidx(render_pos: int) -> int:
        return body_index_map[render_pos] if render_pos < len(body_index_map) else render_pos

    def _body_eid(render_pos: int) -> str:
        return f"{sid}:body:{_orig_bidx(render_pos)}"

    def _bullet_li(el: Element, render_pos: int) -> str:
        return (
            f'<li data-element-id="{_body_eid(render_pos)}" data-slide-id="{sid}">'
            f"{_html_body_text(el)}</li>"
        )

    def _archetype_body_els(title_el: Element | None) -> list[Element]:
        return [el for el in texts if el is not title_el and el.text.strip()]

    archetype = _slide_archetype(slide)
    archetype_title = _title_element(slide)
    archetype_body = _archetype_body_els(archetype_title)

    archetype_html = _html_for_archetype(
        archetype, sid, archetype_title, archetype_body, _title_tag, _body_eid
    )
    if archetype_html is not None:
        return archetype_html

    if layout in ("title", "closing"):
        return _html_title_style_layout(texts_sorted, _title_tag, _sub_tag)

    if layout == "section_header":
        return _html_section_header_layout(texts, _title_tag, _sub_tag)

    if layout in ("image_right", "image_left", "full_image"):
        return _html_image_layout(layout, texts_sorted, images, _title_tag, _bullet_li)

    if layout in ("two_column", "comparison"):
        return _html_two_column_layout(texts, texts_sorted, _title_tag, _bullet_li)

    # Default: bullets
    return _html_default_bullets_layout(texts_sorted, _title_tag, _bullet_li)
