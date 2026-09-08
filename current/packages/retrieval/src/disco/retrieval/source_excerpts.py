"""Bounded views into source text, with offsets into the unmodified passage.

Lexical relevance selects what to read; it is never a claim-support verdict.
The source remains whole in the pool, so callers can inspect other spans.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_TERM = re.compile(r"\d+(?:[.,]\d+)*|[^\W_][\w'-]{2,}", re.UNICODE)
_STOPWORDS = frozenset(
    "about against also and are been from have into that the their this through "
    "user was were what when where which with".split()
)
_DOT_LEADER_PAGE = re.compile(r"(?:\.\s*){3,}\s*\d+(?:\[[^\]]*\]\([^)]+\))?\s*$")
_DOT_LEADER = re.compile(r"(?:\.\s*){3,}")
_MARKDOWN_ANCHOR_ONLY = re.compile(r"\s*(?:(?:[-*+]\s+)|(?:\d+[.)]\s+))?\[[^\]]+\]\(#[^)]+\)\s*$")
_MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\([^)]+\)")
_TRUNCATED_TOC_LINE = re.compile(
    r"^\s*(?:\d+(?:\.\d+)*|Appendix\s+[A-Z])\s*"
    r"\[[^\]]*\]\([^)]+#[^)]+\)"
)

_SECTION_HEADING = re.compile(
    r"^(?: {0,3}#{1,6}\s+| {0,3}\d+(?:\.\d+)*(?:\s*\[[^\]]*\]\([^)]+\))?\.\s{2,})([^\n]{1,120})$"
)

_ANNOTATED_LINE_PREFIX = re.compile(r"^\s*\d+\[\]\([^)]+\)\s+")


def _section_title(line: str) -> str | None:
    if _DOT_LEADER.search(line):
        return None
    clean = _ANNOTATED_LINE_PREFIX.sub("", line).rstrip()
    match = _SECTION_HEADING.fullmatch(clean)
    return match.group(1) if match else None


def _candidate_offsets(text: str, max_chars: int) -> list[int]:
    offsets = set(range(0, len(text), max(1, max_chars // 2)))
    position = 0
    for line in text.splitlines(keepends=True):
        if _section_title(line.rstrip("\r\n")):
            offsets.add(position)
        position += len(line)
    return sorted(offsets)


def evidence_terms(text: str) -> set[str]:
    return set(_TERM.findall(text.casefold())) - _STOPWORDS


def _term_pairs(text: str) -> set[tuple[str, str]]:
    terms = [term for term in _TERM.findall(text.casefold()) if term not in _STOPWORDS]
    return set(zip(terms, terms[1:], strict=False))


def _markdown_toc_line(line: str) -> bool:
    links = _MARKDOWN_LINK.findall(line)
    if len(links) < 2 or sum("#" in link for link in links) < 2:
        return False
    furniture = _MARKDOWN_LINK.sub("", line)
    return not re.search(r"[^\s\-+*.,:;|()\[\]0-9.)]", furniture)


def _navigation_marker_indexes(lines: list[str]) -> tuple[set[int], ...]:
    return (
        {
            index
            for index, line in enumerate(lines)
            if _DOT_LEADER_PAGE.search(line)
            or _TRUNCATED_TOC_LINE.search(line)
            and _DOT_LEADER.search(line)
        },
        {index for index, line in enumerate(lines) if _MARKDOWN_ANCHOR_ONLY.fullmatch(line)},
        {index for index, line in enumerate(lines) if _markdown_toc_line(line)},
    )


def _navigation_line_indexes(window: str) -> set[int]:
    """Return lines that are part of an unmistakable navigation block.

    A few ordinary headings or numeric rows are not enough evidence.  The
    repeated page-number leaders and anchor-only Markdown links used by table
    of contents blocks are structural signals, while leaving prose and data
    tables in the normal relevance score.
    """
    lines = window.splitlines()
    nonempty = [line for line in lines if line.strip()]
    if len(nonempty) < 2:
        return set()

    for indexes in _navigation_marker_indexes(lines):
        if len(indexes) >= 2 and len(indexes) / len(nonempty) >= 0.2:
            return indexes
    return set()


def _relevance_score(window: str, terms: set[str], pairs: set[tuple[str, str]]) -> int:
    navigation = _navigation_line_indexes(window)
    if navigation:
        lines = window.splitlines()
        window = "\n".join(line for index, line in enumerate(lines) if index not in navigation)
    score = len(terms & evidence_terms(window)) + 2 * len(pairs & _term_pairs(window))
    # A named section's own text is more useful than an introduction that
    # merely lists several section titles. Boost matching phrases in real
    # headings only after navigation lines have been discounted. This affects
    # relevance, never source content or a claim-support judgment.
    heading_score = 0
    for index, line in enumerate(window.splitlines()):
        if heading := _section_title(line):
            phrase_matches = pairs & _term_pairs(heading)
            if phrase_matches:
                heading_terms = evidence_terms(heading)
                overlap = len(terms & heading_terms)
                specificity = 2 * overlap * overlap // max(1, len(heading_terms))
                heading_score = max(
                    heading_score,
                    overlap + 2 * len(phrase_matches) + specificity + int(index == 0),
                )
    return score + 2 * heading_score


@dataclass(frozen=True)
class SourceExcerpt:
    text: str
    start: int
    end: int


def relevant_excerpt(text: str, focus: str, *, max_chars: int) -> SourceExcerpt:
    """Select one overlapping window; preserve exact source bytes and offsets.

    Ties prefer the earlier window. A focus without matching terms gets the
    opening window, without pretending it answers the question. Half-window
    overlap retains context around matches near a window boundary. Matching
    section starts are also candidates so a heading can bring its body into view.
    """
    if max_chars < 1:
        raise ValueError("an excerpt needs a positive character allowance")
    terms = evidence_terms(focus)
    pairs = _term_pairs(focus)
    start = 0
    best_score = 0
    raw_start = 0
    raw_best_score = 0
    if len(text) > max_chars and terms:
        for offset in _candidate_offsets(text, max_chars):
            window = text[offset : offset + max_chars]
            # A specific quantity or phrase (e.g. a capacity with its unit)
            # carries more focus than the same tokens scattered across a page.
            raw_score = len(terms & evidence_terms(window)) + 2 * len(pairs & _term_pairs(window))
            if raw_score > raw_best_score:
                raw_start, raw_best_score = offset, raw_score
            score = _relevance_score(window, terms, pairs)
            if score > best_score:
                start, best_score = offset, score
    # A document made entirely of navigation still needs a useful bounded
    # fallback.  Keep the legacy lexical winner when filtering leaves no
    # substantive candidate.
    if best_score == 0:
        start = raw_start
    end = min(len(text), start + max_chars)
    return SourceExcerpt(text=text[start:end], start=start, end=end)
