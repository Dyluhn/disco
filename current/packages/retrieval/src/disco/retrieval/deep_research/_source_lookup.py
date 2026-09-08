"""Exact source location and bounded views; no retrieval or semantic judgment."""

from __future__ import annotations

from typing import Any

from ..source_excerpts import relevant_excerpt
from ..text_match import locate_all, normalized_form, normalized_index

SOURCE_LOOKUP_INSTRUCTION = (
    'For an exact phrase or number, use {"inspect": [{"source_id": "s1", '
    '"find": "literal text"}]}. Find is case-sensitive, returns an excerpt and '
    "up to eight matching character offsets with short exact context, and truthfully "
    "reports no match. The excerpts share the existing 2200-character source allowance. "
    "With find, optional start searches at or after that character offset (default zero). "
    "Matching ignores what page extraction added — whitespace inside a word, soft hyphens, "
    'markdown emphasis around a unit — so "207 kWh" finds a cell reading "207 _kWh/year_". '
    "Without find, start reads at that offset; focus locates relevant context when neither is set. "
    "An empty find means no literal lookup. Focus may also describe what you are checking. "
)

MAX_MATCH_OFFSETS = 8
MATCH_CONTEXT_CHARS = 120


def inspection_selector_error(focus: Any, start: Any, find: Any) -> str | None:
    if not isinstance(focus, str) or len(focus) > 300:
        return 'inspection "focus" must be a string of at most 300 characters'
    if start is not None and (type(start) is not int or start < 0):
        return 'inspection "start" must be a non-negative character offset'
    if not isinstance(find, str) or len(find) > 300:
        return 'inspection "find" must be a literal string of at most 300 characters'
    return None


def locate_source(
    text: str, *, focus: str, start: int | None, find: str, max_chars: int
) -> tuple[int | None, dict[str, Any]]:
    """Return an exact span start plus bounded match metadata or a truthful error."""
    if start is not None and start >= len(text):
        return None, {"error": "Offset is outside the source.", "source_chars": len(text)}
    if not find:
        if start is None:
            start = relevant_excerpt(text, focus, max_chars=max_chars).start
        return start, {}
    search_start = start or 0
    # The page, not the extraction's artefacts: a cell reading "207 _kWh/year_"
    # answers a search for "207 kWh", and a passage reading "ou tflow" answers
    # one for "outflow". Both were recorded as absent by a byte comparison, and
    # a report said a standards table could not be verified from the source
    # that states it (`..text_match`).
    index = normalized_index(text)
    spans = [
        span
        for span in locate_all(find, index, limit=MAX_MATCH_OFFSETS + 1)
        if span[0] >= search_start
    ]
    matches = [span[0] for span in spans]
    metadata: dict[str, Any] = {
        "search_start": search_start,
        "searched_normalised": normalized_form(find),
        "matches": [
            {
                "offset": offset,
                "start": max(0, offset - 40),
                "end": min(len(text), max(0, offset - 40) + MATCH_CONTEXT_CHARS),
                "text": text[max(0, offset - 40) : max(0, offset - 40) + MATCH_CONTEXT_CHARS],
            }
            for offset in matches[:MAX_MATCH_OFFSETS]
        ],
        "more_matches": len(matches) > MAX_MATCH_OFFSETS,
    }
    if not matches:
        return None, {
            **metadata,
            "error": (
                f"No match for {normalized_form(find)!r} at or after character "
                f"{search_start} in this source. That is the form actually "
                "searched: the comparison ignores whitespace, soft hyphens and "
                "markdown emphasis, and is otherwise case-sensitive and exact. "
                "Try a shorter literal or focus."
            ),
            "source_chars": len(text),
        }
    return max(0, matches[0] - min(300, max_chars // 4)), metadata
