"""Task-focused evidence views for the writer; full sources remain immutable."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from ..models import Passage
from ..source_excerpts import relevant_excerpt
from ._source_inspection import EXCERPT_CHARS

_EVIDENCE_CHAR_BUDGET = 220_000
_OMITTED_RANGE_SEPARATOR = "\n… [other source text omitted] …\n"
_MIDDLE_OMISSION = "\n… [middle omitted] …\n"
_CONTEXT_LIMITATION_NOTICE = "[evidence context limited by budget]"
_NOTES_SEPARATOR = "\n\n"


def _inspected_ranges(
    passage: Passage, trail: Sequence[Mapping[str, Any]]
) -> list[tuple[int, int]]:
    digest = hashlib.sha256(passage.text.encode()).hexdigest()
    ranges: list[tuple[int, int]] = []
    for row in reversed(trail):
        start, end = row.get("start"), row.get("end")
        if (
            row.get("kind") == "source_inspection"
            and row.get("ok") is True
            and row.get("source_id") == passage.id
            and row.get("source_sha256") == digest
            and type(start) is int
            and type(end) is int
            and 0 <= start < end <= len(passage.text)
        ):
            ranges.append((start, end))
    return ranges


def _merge_ranges(ranges: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _expand_ranges(
    ranges: Sequence[tuple[int, int]], *, text_length: int, max_chars: int
) -> list[tuple[int, int]]:
    """Use spare content allowance to widen every selected range around it."""
    selected = _merge_ranges(ranges)
    while selected:
        available = max_chars - sum(end - start for start, end in selected)
        if available <= 0:
            break
        per_range, remainder = divmod(available, len(selected))
        expanded: list[tuple[int, int]] = []
        for index, (start, end) in enumerate(selected):
            extra = per_range + (index < remainder)
            left = min(start, (extra + 1) // 2)
            right = min(text_length - end, extra - left)
            unused = extra - left - right
            if unused:
                additional_left = min(start - left, unused)
                left += additional_left
            expanded.append((start - left, end + right))
        merged = _merge_ranges(expanded)
        if merged == selected:
            break
        selected = merged
    return selected


def _body_markup_length(ranges: Sequence[tuple[int, int]]) -> int:
    return sum(len(f"[source characters {start}:{end}]\n") for start, end in ranges) + max(
        0, len(ranges) - 1
    ) * len(_OMITTED_RANGE_SEPARATOR)


def _maximum_body_markup_length(range_count: int, text_length: int) -> int:
    header_length = len(f"[source characters {text_length}:{text_length}]\n")
    return range_count * header_length + max(0, range_count - 1) * len(_OMITTED_RANGE_SEPARATOR)


def _source_header(passage: Passage, label: str) -> str:
    title = passage.source_title or passage.source_url or label
    return f"[{label}] {title}\n{passage.source_url}\n"


def _bounded_limitation_notice(char_budget: int) -> str:
    return _CONTEXT_LIMITATION_NOTICE[: max(0, char_budget)]


def _compact_unfocused_body(body: str, share: int) -> str:
    if share <= len(_MIDDLE_OMISSION):
        return _bounded_limitation_notice(share)
    content_budget = share - len(_MIDDLE_OMISSION)
    head_chars = int(content_budget * 0.7)
    tail_chars = int(content_budget * 0.25)
    head = body[:head_chars].rstrip()
    tail = body[-tail_chars:].lstrip() if tail_chars else ""
    return f"{head}{_MIDDLE_OMISSION}{tail}"


def _render_passage_body(
    passage: Passage,
    share: int,
    query: str,
    focuses: Sequence[str],
    trail: Sequence[Mapping[str, Any]],
) -> str:
    body = passage.text.strip()
    if len(body) <= share:
        return body
    if query:
        return _focused_body(passage, focuses, share, trail)
    return _compact_unfocused_body(body, share)


def _focused_body(
    passage: Passage, focuses: Sequence[str], share: int, trail: Sequence[Mapping[str, Any]]
) -> str:
    """Retain inspected and relevant middle spans within the existing allowance."""
    window_chars = min(EXCERPT_CHARS, max(1, (share - 160) // min(3, max(2, len(focuses)))))
    windows = [relevant_excerpt(passage.text, focus, max_chars=window_chars) for focus in focuses]
    candidates = [(window.start, window.end) for window in windows[:-1]]
    candidates.extend(_inspected_ranges(passage, trail))
    candidates.append((windows[-1].start, windows[-1].end))
    selected: list[tuple[int, int]] = []
    for candidate in candidates:
        merged = _merge_ranges([*selected, candidate])
        content_length = sum(end - start for start, end in merged)
        if content_length + _body_markup_length(merged) <= share:
            selected = merged
    # Expand around every selected range, preserving surrounding conditions.
    # Re-scoring a larger window can favor introductory keyword coverage instead.
    content_allowance = max(
        0, share - _maximum_body_markup_length(len(selected), len(passage.text))
    )
    selected = _expand_ranges(selected, text_length=len(passage.text), max_chars=content_allowance)
    return _OMITTED_RANGE_SEPARATOR.join(
        f"[source characters {start}:{end}]\n{passage.text[start:end]}" for start, end in selected
    )


def _fit_lines(block: str, limit: int) -> str:
    """Drop whole trailing lines until the block fits: never half a quote."""
    lines = block.split("\n")
    while lines and len("\n".join(lines)) > limit:
        lines.pop()
    return "\n".join(lines)


def _passage_view(
    passage: Passage,
    share: int,
    notes_block: str,
    query: str,
    focuses: Sequence[str],
    trail: Sequence[Mapping[str, Any]],
) -> str:
    """This source as the writer reads it: validated notes first, then windows.

    Notes are charged to the source's own share rather than added to it, so a
    read source spends its allowance on quotes proved against its text and the
    keyword windows keep whatever remains. A source with no notes is rendered
    exactly as it always was.

    A source whose whole text fits its share keeps that text and shows no
    notes. Its quotes are already in front of the writer, and spending the
    share on them would push out the sentences around them — this stage adds
    evidence to a report and must never be the reason some is missing.
    """
    if not notes_block or len(passage.text.strip()) <= share:
        return _render_passage_body(passage, share, query, focuses, trail)
    block = _fit_lines(notes_block, share)
    remaining = share - len(block) - len(_NOTES_SEPARATOR)
    if not block:
        return _render_passage_body(passage, share, query, focuses, trail)
    if remaining <= 0:
        return block
    windows = _render_passage_body(passage, remaining, query, focuses, trail)
    return f"{block}{_NOTES_SEPARATOR}{windows}"


def _coverage_angles(passage_id: str, coverage: Mapping[str, Any] | None) -> list[str]:
    covered = (coverage or {}).get("covered", [])
    return list(
        dict.fromkeys(
            row["angle"]
            for row in covered
            if isinstance(row, Mapping)
            and isinstance(row.get("angle"), str)
            and isinstance(row.get("evidence_ids"), list)
            and passage_id in row["evidence_ids"]
        )
    )


def _body_allowances(
    passages: Sequence[Passage], weights: Mapping[str, int], budget: int
) -> dict[str, int]:
    """Redistribute unused space before compacting any source.

    Weighted shares matter only when the complete pool does not fit. Short
    documents must not reserve space that another source needs for its text.
    """
    remaining = {passage.id: len(passage.text.strip()) for passage in passages}
    allowances: dict[str, int] = {}
    available = max(0, budget)
    while remaining:
        total_weight = sum(weights[source_id] for source_id in remaining)
        shares = {
            source_id: available * weights[source_id] // total_weight for source_id in remaining
        }
        complete = [source_id for source_id, size in remaining.items() if size <= shares[source_id]]
        if not complete:
            allowances.update(shares)
            break
        for source_id in complete:
            size = remaining.pop(source_id)
            allowances[source_id] = size
            available -= size
    return allowances


def format_evidence_pool(
    passages: list[Passage],
    *,
    char_budget: int = _EVIDENCE_CHAR_BUDGET,
    aliases: Mapping[str, str] | None = None,
    query: str = "",
    coverage: Mapping[str, Any] | None = None,
    trail: Sequence[Mapping[str, Any]] = (),
    notes: Mapping[str, str] | None = None,
) -> str:
    """Render the full pool as `[id] title\\nURL\\ntext` blocks, adaptively
    selecting inspected and relevant source context so the total
    stays inside the writer's context budget.

    `aliases` maps a canonical passage id to the SHORT ordinal label the writer
    is taught to cite (`_citation_aliases`). When it is supplied the canonical
    id does not appear in the writer's prompt at all: one identity for the
    model, short enough to copy sixty times without inventing its own scheme.

    `notes` maps a passage id to the rendered NOTES block from reading that
    source whole (`_source_reading`). It is placed first inside that source's
    existing share; the total pool budget is unchanged.
    """
    if char_budget <= 0:
        return ""
    if not passages:
        return (
            "(no evidence)"
            if char_budget >= len("(no evidence)")
            else _bounded_limitation_notice(char_budget)
        )
    labels = {p.id: (aliases or {}).get(p.id, p.id) for p in passages}
    headers = {p.id: _source_header(p, labels[p.id]) for p in passages}
    overhead = sum(len(headers[p.id]) for p in passages) + max(0, len(passages) - 1) * 2
    if overhead > char_budget:
        return _bounded_limitation_notice(char_budget)
    angles_by_id = {passage.id: _coverage_angles(passage.id, coverage) for passage in passages}
    weights = {
        passage.id: 1 + len(angles_by_id[passage.id]) + len(set(_inspected_ranges(passage, trail)))
        for passage in passages
    }
    allowances = _body_allowances(passages, weights, char_budget - overhead)
    parts: list[str] = []
    for passage in passages:
        body = _passage_view(
            passage,
            allowances[passage.id],
            (notes or {}).get(passage.id, ""),
            query,
            [*angles_by_id[passage.id], query],
            trail,
        )
        parts.append(f"{headers[passage.id]}{body}")
    return "\n\n".join(parts)
