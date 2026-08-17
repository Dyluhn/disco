"""Slide-archetype introspection helpers shared by the PPTX and HTML archetype
renderers (big_number / quote / timeline / two_by_two).

Moved verbatim out of ``_pptx_render.py``.
"""

from __future__ import annotations

from .._deck_schema import Element, Slide


def _slide_archetype(slide: Slide) -> str:
    return str(getattr(slide, "archetype", None) or "").strip().lower()


def _plain_element_text(el: Element) -> str:
    return el.text[2:] if el.text.startswith("• ") else el.text


def _title_element(slide: Slide) -> Element | None:
    texts = [el for el in slide.elements if el.kind == "text" and el.text.strip()]
    if not texts:
        return None
    bold_heads = [el for el in texts if el.bold and el.font_size_pt >= 20]
    return max(bold_heads or texts, key=lambda el: el.font_size_pt)


def _body_strings(slide: Slide) -> list[str]:
    title_el = _title_element(slide)
    out: list[str] = []
    for el in slide.elements:
        if el.kind != "text" or el is title_el:
            continue
        text = _plain_element_text(el).strip()
        if text:
            out.append(text)
    return out


def _split_timeline_label(raw: str) -> tuple[str, str]:
    for sep in (" — ", " - ", "|", ":"):
        if sep in raw:
            left, right = raw.split(sep, 1)
            return left.strip(), right.strip()
    return raw.strip(), ""
