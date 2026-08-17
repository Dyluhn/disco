"""Deck (HTML slide artifact) rule family: type floor, text budget, bullet-list
monotony, missing imagery, glass sanity, orphan slides, and handwritten-HTML
detection. Extracted from `design_lint.py`.

`_rule_deck_glass` itself is DECOMPOSED (not just relocated) versus the
original 19-mccabe monolith: it had three independent phases — a CSS-block
missing-saturate scan, a per-slide missing-saturate scan, and a flat-backdrop
determination pass over the combined `glass_uses` list (which `break`s after
its first finding). Those are now three separately-named callables
(`_deck_glass_css_scan` / `_deck_glass_slide_scan` /
`_deck_glass_flat_backdrop_findings`), concatenated in the exact same order
the original three loops ran in (CSS findings, then slide findings, then the
single flat-backdrop finding) — so the pre-sort finding order out of this rule
is unchanged. Every finding field, message, and the early-`break` semantics are
unchanged — this is pure decomposition, not a behavior change."""

from __future__ import annotations

import re

from ._constants import (
    _CHOICE_DECK_ARCHETYPE_MONOTONY,
    _CHOICE_DECK_GLASS,
    _CHOICE_DECK_HANDWRITTEN_HTML,
    _CHOICE_DECK_IMAGERY,
    _CHOICE_DECK_ORPHAN_SLIDE,
    _CHOICE_DECK_TEXT_BUDGET,
    _CHOICE_DECK_TYPE_FLOOR,
    _CSS_BLOCK_RE,
    _DECK_BULLET_LIMIT,
    _DECK_PIPELINE_GENERATOR_MARKER,
    _DECK_TEXT_WORD_LIMIT,
    _DECK_TYPE_FLOOR_PX,
    _FONT_SIZE_DECL_RE,
    _WORD_RE,
)
from ._deck_slides import (
    _attr_value,
    _deck_image_count,
    _DeckSlide,
    _global_deck_backdrop_is_flat,
    _has_slide_nav_markers,
    _slide_has_flat_backdrop,
    _slide_has_media,
    _slide_text_blocks,
    _style_attrs,
)
from ._model import DesignFinding
from ._scan_helpers import (
    _backdrop_without_saturate,
    _bullet_count,
    _font_size_px,
    _has_colorful_backdrop,
    _line_of,
    _style_has_backdrop_filter,
    _word_count,
)


def _rule_deck_type_floor(path: str, text: str, slides: list[_DeckSlide]) -> list[DesignFinding]:
    findings: list[DesignFinding] = []
    for slide in slides:
        for style, offset in _style_attrs(slide):
            for m in _FONT_SIZE_DECL_RE.finditer(style):
                px = _font_size_px(m.group("value"), m.group("unit"))
                if px is None or px >= _DECK_TYPE_FLOOR_PX:
                    continue
                evidence = m.group(0).strip()
                findings.append(
                    DesignFinding(
                        rule_id="deck_type_floor",
                        severity="error",
                        path=path,
                        line=_line_of(text, offset + m.start()),
                        evidence=evidence,
                        choice_key=_CHOICE_DECK_TYPE_FLOOR,
                        message=(
                            f"Deck slide type floor violation on slide {slide.number}: "
                            f"inline {evidence} resolves below 24px."
                        ),
                    )
                )
    return findings


def _rule_deck_text_budget(path: str, slides: list[_DeckSlide]) -> list[DesignFinding]:
    findings: list[DesignFinding] = []
    for slide in slides:
        words = _word_count(slide.html)
        bullets = _bullet_count(slide.html)
        if words <= _DECK_TEXT_WORD_LIMIT and bullets <= _DECK_BULLET_LIMIT:
            continue
        findings.append(
            DesignFinding(
                rule_id="deck_text_budget",
                severity="warning",
                path=path,
                line=slide.line,
                evidence=f"{words} words, {bullets} bullet lines",
                choice_key=_CHOICE_DECK_TEXT_BUDGET,
                message=(
                    f"too much text on slide {slide.number}: {words} words and "
                    f"{bullets} bullet lines. Keep slides under about 90 words "
                    "and no more than 6 bullet lines."
                ),
            )
        )
    return findings


def _slide_is_pure_bullet_list(slide: _DeckSlide) -> bool:
    layout = (_attr_value(slide.attrs, "data-layout") or "").strip().lower()
    if layout in {"image_right", "image_left", "full_image", "section_header", "title", "closing"}:
        return False
    if layout in {"bullets", "bullet", "bullet_list"}:
        return _bullet_count(slide.html) > 0
    if _bullet_count(slide.html) == 0:
        return False
    if re.search(r"<\s*(?:img|picture|svg|canvas|video|table|figure)\b", slide.html, re.I):
        return False
    non_bullet_blocks = re.findall(r"<\s*(?:p|blockquote|pre|table|figure)\b", slide.html, re.I)
    return len(non_bullet_blocks) == 0


def _rule_deck_bullet_monotony(path: str, slides: list[_DeckSlide]) -> list[DesignFinding]:
    findings: list[DesignFinding] = []
    run_start: int | None = None
    for idx, slide in enumerate(slides):
        if _slide_is_pure_bullet_list(slide):
            if run_start is None:
                run_start = idx
            continue
        if run_start is not None and idx - run_start >= 3:
            first = slides[run_start]
            last = slides[idx - 1]
            findings.append(
                DesignFinding(
                    rule_id="deck_bullet_monotony",
                    severity="warning",
                    path=path,
                    line=first.line,
                    evidence=f"slides {first.number}-{last.number} are pure bullet lists",
                    choice_key=_CHOICE_DECK_ARCHETYPE_MONOTONY,
                    message=(
                        f"Pure bullet-list monotony across slides {first.number}-{last.number}. "
                        "Vary the run with imagery, section, quote, chart, or comparison slides."
                    ),
                )
            )
        run_start = None
    if run_start is not None and len(slides) - run_start >= 3:
        first = slides[run_start]
        last = slides[-1]
        findings.append(
            DesignFinding(
                rule_id="deck_bullet_monotony",
                severity="warning",
                path=path,
                line=first.line,
                evidence=f"slides {first.number}-{last.number} are pure bullet lists",
                choice_key=_CHOICE_DECK_ARCHETYPE_MONOTONY,
                message=(
                    f"Pure bullet-list monotony across slides {first.number}-{last.number}. "
                    "Vary the run with imagery, section, quote, chart, or comparison slides."
                ),
            )
        )
    return findings


def _rule_deck_missing_imagery(path: str, slides: list[_DeckSlide]) -> list[DesignFinding]:
    if _deck_image_count(slides) > 0:
        return []
    return [
        DesignFinding(
            rule_id="deck_missing_imagery",
            severity="warning",
            path=path,
            line=slides[0].line if slides else 1,
            evidence="0 images across deck slides",
            choice_key=_CHOICE_DECK_IMAGERY,
            message="no imagery: cover/divider slides should carry generated art",
        )
    ]


def _deck_glass_css_scan(
    path: str, text: str
) -> tuple[list[DesignFinding], list[tuple[_DeckSlide | None, int, str]]]:
    findings: list[DesignFinding] = []
    glass_uses: list[tuple[_DeckSlide | None, int, str]] = []
    for m in _CSS_BLOCK_RE.finditer(text):
        body = m.group(2)
        if not _style_has_backdrop_filter(body):
            continue
        evidence = _backdrop_without_saturate(body)
        if evidence is not None:
            findings.append(
                DesignFinding(
                    rule_id="deck_glass_missing_saturate",
                    severity="warning",
                    path=path,
                    line=_line_of(text, m.start()),
                    evidence=evidence,
                    choice_key=_CHOICE_DECK_GLASS,
                    message="backdrop-filter without saturate() makes deck glass read as gray mud",
                )
            )
        glass_uses.append((None, m.start(), evidence or "backdrop-filter"))
    return findings, glass_uses


def _deck_glass_slide_scan(
    path: str, text: str, slides: list[_DeckSlide]
) -> tuple[list[DesignFinding], list[tuple[_DeckSlide | None, int, str]]]:
    findings: list[DesignFinding] = []
    glass_uses: list[tuple[_DeckSlide | None, int, str]] = []
    for slide in slides:
        for style, offset in _style_attrs(slide):
            if not _style_has_backdrop_filter(style):
                continue
            evidence = _backdrop_without_saturate(style)
            if evidence is not None:
                findings.append(
                    DesignFinding(
                        rule_id="deck_glass_missing_saturate",
                        severity="warning",
                        path=path,
                        line=_line_of(text, offset),
                        evidence=evidence,
                        choice_key=_CHOICE_DECK_GLASS,
                        message=(
                            "backdrop-filter without saturate() makes deck glass read as gray mud"
                        ),
                    )
                )
            glass_uses.append((slide, offset, evidence or "backdrop-filter"))
    return findings, glass_uses


def _deck_glass_flat_backdrop_findings(
    path: str,
    text: str,
    slides: list[_DeckSlide],
    glass_uses: list[tuple[_DeckSlide | None, int, str]],
) -> list[DesignFinding]:
    findings: list[DesignFinding] = []
    global_flat = _global_deck_backdrop_is_flat(text)
    deck_has_colorful_backdrop = any(
        _has_colorful_backdrop(f"{slide.attrs}\n{slide.html}") for slide in slides
    )
    for slide, offset, evidence in glass_uses:
        if slide is not None:
            raw = f"{slide.attrs}\n{slide.html}"
            if _has_colorful_backdrop(raw):
                continue
            if _slide_has_flat_backdrop(slide) or global_flat:
                findings.append(
                    DesignFinding(
                        rule_id="deck_glass_flat_backdrop",
                        severity="warning",
                        path=path,
                        line=_line_of(text, offset),
                        evidence=evidence,
                        choice_key=_CHOICE_DECK_GLASS,
                        message=(
                            f"glass on slide {slide.number} sits over a flat single-color "
                            "backdrop; glass needs a colorful or image backdrop"
                        ),
                    )
                )
                break
            continue
        if global_flat and not deck_has_colorful_backdrop:
            findings.append(
                DesignFinding(
                    rule_id="deck_glass_flat_backdrop",
                    severity="warning",
                    path=path,
                    line=_line_of(text, offset),
                    evidence=evidence,
                    choice_key=_CHOICE_DECK_GLASS,
                    message=(
                        "glass sits over a flat single-color deck backdrop; glass needs a "
                        "colorful or image backdrop"
                    ),
                )
            )
            break
    return findings


def _rule_deck_glass(path: str, text: str, slides: list[_DeckSlide]) -> list[DesignFinding]:
    css_findings, css_glass_uses = _deck_glass_css_scan(path, text)
    slide_findings, slide_glass_uses = _deck_glass_slide_scan(path, text, slides)
    findings = css_findings + slide_findings
    glass_uses = css_glass_uses + slide_glass_uses
    if not glass_uses:
        return findings
    findings += _deck_glass_flat_backdrop_findings(path, text, slides, glass_uses)
    return findings


def _rule_deck_orphan_slide(path: str, slides: list[_DeckSlide]) -> list[DesignFinding]:
    findings: list[DesignFinding] = []
    skip_layouts = {"title", "section_header", "section", "closing", "full_image"}
    skip_archetypes = {
        "title",
        "section_divider",
        "closing",
        "full_bleed_image",
        "photo_grid",
    }
    for slide in slides:
        layout = (_attr_value(slide.attrs, "data-layout") or "").strip().lower()
        archetype = (_attr_value(slide.attrs, "data-archetype") or "").strip().lower()
        if layout in skip_layouts or archetype in skip_archetypes or _slide_has_media(slide):
            continue
        blocks = _slide_text_blocks(slide)
        headings = [text for tag, text in blocks if tag.startswith("h")]
        content = [text for tag, text in blocks if not tag.startswith("h")]
        if not headings or len(content) > 1:
            continue
        if content and len(_WORD_RE.findall(content[0])) > 12:
            continue
        findings.append(
            DesignFinding(
                rule_id="deck_orphan_slide",
                severity="warning",
                path=path,
                line=slide.line,
                evidence=(f"slide {slide.number}: title plus {len(content)} short content line(s)"),
                choice_key=_CHOICE_DECK_ORPHAN_SLIDE,
                message=(
                    f"Slide {slide.number} reads like an orphan: a non-divider slide has "
                    "a title and at most one short content line. Merge it, add substance, "
                    "or use a real quote/metric/visual archetype."
                ),
            )
        )
    return findings


def _rule_deck_handwritten_html(path: str, text: str) -> list[DesignFinding]:
    if _DECK_PIPELINE_GENERATOR_MARKER in text:
        return []
    data_label_sections = list(
        re.finditer(r"<section\b[^>]*\bdata-label\s*=", text, re.I | re.DOTALL)
    )
    if len(data_label_sections) < 2 or not _has_slide_nav_markers(text):
        return []
    first = data_label_sections[0]
    return [
        DesignFinding(
            rule_id="deck_handwritten_html",
            severity="warning",
            path=path,
            line=_line_of(text, first.start()),
            evidence="<section data-label> slide deck without pipeline generator marker",
            choice_key=_CHOICE_DECK_HANDWRITTEN_HTML,
            message=(
                "Handwritten deck-shaped HTML detected; use slides_generate — the host "
                "renderer prevents overlap/spill."
            ),
        )
    ]
