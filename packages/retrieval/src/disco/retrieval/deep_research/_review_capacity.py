"""Spend spare review context on exact source ranges within serialized bounds."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from ..models import Passage
from ..source_excerpts import SourceExcerpt
from ._claim_review import MAX_EVIDENCE_RANGE_CHARS
from ._writer_evidence import _expand_ranges, _merge_ranges


def evidence_line(
    passage_id: str, passage: Passage, labels: Sequence[str], excerpt: SourceExcerpt
) -> str:
    return json.dumps(
        {
            "citation_id": f"[[{passage_id}]]",
            "title": passage.source_title[:250],
            "url": passage.source_url[:1000],
            "relevance": list(labels),
            "excerpt": excerpt.text,
            "start": excerpt.start,
            "end": excerpt.end,
            "source_chars": len(passage.text),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _range_lines(
    passage_id: str, passage: Passage, labels: Sequence[str], ranges: Sequence[tuple[int, int]]
) -> list[str]:
    lines: list[str] = []
    for start, end in ranges:
        for offset in range(start, end, MAX_EVIDENCE_RANGE_CHARS):
            limit = min(end, offset + MAX_EVIDENCE_RANGE_CHARS)
            excerpt = SourceExcerpt(passage.text[offset:limit], offset, limit)
            lines.append(evidence_line(passage_id, passage, labels, excerpt))
    return lines


def _cost(lines: Sequence[str]) -> int:
    return sum(len(line) + 1 for line in lines)


def _expanded_lines(
    passage_id: str,
    passage: Passage,
    labels: Sequence[str],
    windows: Sequence[SourceExcerpt],
    original: list[str],
    allowance: int,
) -> list[str]:
    ranges = _merge_ranges([(window.start, window.end) for window in windows])
    low = sum(end - start for start, end in ranges)
    high = min(len(passage.text), allowance)
    best = original
    # JSON escaping and range metadata consume capacity too. Search actual
    # serialized cost; never remove an admitted range to fit expanded context.
    while low <= high:
        size = (low + high) // 2
        expanded = _expand_ranges(ranges, text_length=len(passage.text), max_chars=size)
        lines = _range_lines(passage_id, passage, labels, expanded)
        if _cost(lines) <= allowance:
            best = lines
            low = size + 1
        else:
            high = size - 1
    return best


def expand_review_sources(
    selected: Mapping[str, Sequence[SourceExcerpt]],
    by_id: Mapping[str, Passage],
    labels: Mapping[str, Sequence[str]],
    budget: int,
) -> list[str]:
    """Expand admitted windows fairly, charging exact JSON and newline costs."""
    packed = {
        passage_id: [
            evidence_line(passage_id, by_id[passage_id], labels[passage_id], window)
            for window in windows
        ]
        for passage_id, windows in selected.items()
    }
    used = sum(_cost(lines) for lines in packed.values())
    ordered = sorted(selected, key=lambda passage_id: len(by_id[passage_id].text))
    for index, passage_id in enumerate(ordered):
        original = packed[passage_id]
        allowance = _cost(original) + max(0, budget - used) // (len(ordered) - index)
        expanded = _expanded_lines(
            passage_id,
            by_id[passage_id],
            labels[passage_id],
            selected[passage_id],
            original,
            allowance,
        )
        used += _cost(expanded) - _cost(original)
        packed[passage_id] = expanded
    return [line for lines in packed.values() for line in lines]
