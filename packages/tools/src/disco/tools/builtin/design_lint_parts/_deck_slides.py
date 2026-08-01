"""Deck-slide extraction + per-slide attribute/backdrop helpers shared by the
deck rule family (`_rules_deck.py`). Extracted from `design_lint.py` verbatim
(logic unchanged)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ._constants import _CLASS_ATTR_RE, _CSS_BLOCK_RE, _SECTION_OPEN_RE, _STYLE_ATTR_RE
from ._scan_helpers import (
    _has_colorful_backdrop,
    _line_of,
    _style_has_flat_background,
    _visible_text,
)


@dataclass(frozen=True)
class _DeckSlide:
    """One slide section found in a generated HTML deck."""

    number: int
    line: int
    attrs: str
    html: str
    open_start: int
    html_start: int


def _attr_value(attrs: str, name: str) -> str | None:
    m = re.search(
        rf"\b{re.escape(name)}\s*=\s*(['\"])(?P<value>.*?)\1",
        attrs,
        re.I | re.DOTALL,
    )
    return m.group("value") if m is not None else None


def _has_attr(attrs: str, name: str) -> bool:
    return re.search(rf"\b{re.escape(name)}\s*=", attrs, re.I) is not None


def _class_tokens(raw: str) -> set[str]:
    return {token.strip().lower() for token in re.split(r"\s+", raw) if token.strip()}


def _has_class(attrs: str, token: str) -> bool:
    classes = _attr_value(attrs, "class")
    return classes is not None and token.lower() in _class_tokens(classes)


def _has_deck_wrapper(text: str) -> bool:
    return any("deck" in _class_tokens(m.group("class")) for m in _CLASS_ATTR_RE.finditer(text))


def _is_deck_section_candidate(attrs: str) -> bool:
    return (
        _has_attr(attrs, "data-slide-id")
        or _has_attr(attrs, "data-label")
        or _has_class(attrs, "slide")
    )


def _extract_deck_slides(text: str) -> list[_DeckSlide]:
    """Return slide sections only when the markup looks like a deck artifact.

    The actual deck renderer stamps `<div class="deck">` and
    `<section class="slide" data-slide-id=... data-layout=...>`. Some artifact
    paths use `<section data-label=...>` instead, so repeated data-label sections
    are also accepted as deck-like. Ordinary web pages with a single labelled
    section stay out of the deck rule pack.
    """
    candidates: list[tuple[re.Match[str], str, int, int]] = []
    strong_markers = 0
    data_label_markers = 0
    for m in _SECTION_OPEN_RE.finditer(text):
        attrs = m.group("attrs") or ""
        if not _is_deck_section_candidate(attrs):
            continue
        close = re.search(r"</section\s*>", text[m.end() :], re.I)
        if close is None:
            continue
        body_start = m.end()
        body_end = m.end() + close.start()
        candidates.append((m, attrs, body_start, body_end))
        if _has_attr(attrs, "data-slide-id") or _has_class(attrs, "slide"):
            strong_markers += 1
        if _has_attr(attrs, "data-label"):
            data_label_markers += 1

    if not candidates:
        return []
    if not (_has_deck_wrapper(text) or strong_markers > 0 or data_label_markers >= 2):
        return []

    slides: list[_DeckSlide] = []
    for i, (m, attrs, body_start, body_end) in enumerate(candidates, 1):
        slides.append(
            _DeckSlide(
                number=i,
                line=_line_of(text, m.start()),
                attrs=attrs,
                html=text[body_start:body_end],
                open_start=m.start(),
                html_start=body_start,
            )
        )
    return slides


def _style_attrs(slide: _DeckSlide) -> list[tuple[str, int]]:
    styles: list[tuple[str, int]] = []
    for m in _STYLE_ATTR_RE.finditer(slide.attrs):
        styles.append((m.group("style"), slide.open_start + m.start()))
    for m in _STYLE_ATTR_RE.finditer(slide.html):
        styles.append((m.group("style"), slide.html_start + m.start()))
    return styles


def _deck_image_count(slides: list[_DeckSlide]) -> int:
    count = 0
    for slide in slides:
        raw = f"{slide.attrs}\n{slide.html}"
        count += len(re.findall(r"<\s*(?:img|picture|source)\b", raw, re.I))
        count += len(re.findall(r"\bbackground(?:-image)?\s*:[^;{}]*url\(", raw, re.I))
    return count


def _slide_has_flat_backdrop(slide: _DeckSlide) -> bool:
    return any(
        _style_has_flat_background(m.group("style")) for m in _STYLE_ATTR_RE.finditer(slide.attrs)
    )


def _global_deck_backdrop_is_flat(text: str) -> bool:
    for m in _CSS_BLOCK_RE.finditer(text):
        selector = m.group(1).lower()
        if not any(tok in selector for tok in (".slide", ".deck", "body")):
            continue
        body = m.group(2)
        if _has_colorful_backdrop(body):
            return False
        if _style_has_flat_background(body):
            return True
    return False


def _slide_text_blocks(slide: _DeckSlide) -> list[tuple[str, str]]:
    blocks: list[tuple[str, str]] = []
    for m in re.finditer(
        r"<(?P<tag>h[1-6]|p|li|blockquote|figcaption)\b[^>]*>"
        r"(?P<body>.*?)</(?P=tag)\s*>",
        slide.html,
        re.I | re.DOTALL,
    ):
        text = _visible_text(m.group("body"))
        if text:
            blocks.append((m.group("tag").lower(), text))
    return blocks


def _slide_has_media(slide: _DeckSlide) -> bool:
    return (
        re.search(
            r"<\s*(?:img|picture|svg|canvas|video|table)\b",
            slide.html,
            re.I,
        )
        is not None
    )


def _has_slide_nav_markers(text: str) -> bool:
    return (
        re.search(
            r"\b(?:slide-nav|btn-prev|btn-next|currentSlide|goToSlide|nextSlide|prevSlide|"
            r"ArrowRight|ArrowLeft)\b",
            text,
            re.I,
        )
        is not None
    )
