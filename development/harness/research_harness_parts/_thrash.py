"""Pure Deep Research thrash signals.

This module intentionally consumes the observer's redacted wire/model trace and
does not call providers or inspect product state.  It is a diagnostic instrument:
it names repeated work and no-progress spans without trying to steer the product.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

_QUERY_KEYS = frozenset({"query", "issued_query", "search_query", "probe"})
_QUERY_LIST_KEYS = frozenset({"issued_queries"})
_DEFICIENCY_KEYS = frozenset({"deficiencies", "deficiency", "review_notes", "review_failures"})
_MALFORMED_KEYS = frozenset({"malformed", "parse_error", "invalid", "schema_error"})
_RESEARCH_CONTROL_STAGES = frozenset(
    {"research_turn", "research_control", "research_control_turn"}
)
_COUNT_KEYS = frozenset(
    {
        "added",
        "new_evidence_count",
        "new_passage_count",
        "passage_count",
        "source_count",
        "evidence_count",
        "rows",
    }
)


def _normalize_query(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().casefold())


def _hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _walk(value: Any) -> list[tuple[str, Any]]:
    if isinstance(value, Mapping):
        rows: list[tuple[str, Any]] = []
        for key, item in value.items():
            rows.append((str(key), item))
            rows.extend(_walk(item))
        return rows
    if isinstance(value, list):
        rows = []
        for item in value:
            rows.extend(_walk(item))
        return rows
    return []


def _queries(events: Sequence[Any]) -> list[str]:
    found: list[str] = []
    for event in events:
        payload = getattr(event, "payload", event)
        phase = str(getattr(event, "phase", "")).casefold()
        for key, value in _walk(payload):
            if key not in _QUERY_KEYS and key not in _QUERY_LIST_KEYS:
                continue
            # The original user question is often repeated inside report/state
            # frames.  Only count search/probe query fields as research work;
            # explicit issued_query fields remain useful in any phase.
            if key in {"query", "probe"} and phase not in {"search", "planning"}:
                continue
            values = value if isinstance(value, list) else [value]
            for item in values:
                if isinstance(item, str) and item.strip():
                    found.append(_normalize_query(item))
    return found


def _decision_queries(model_io: Sequence[Mapping[str, Any]]) -> list[str]:
    """Queries the researcher proposed, including host-rejected repeats."""
    found: list[str] = []
    for row in model_io:
        decision = row.get("declared_decision")
        if not isinstance(decision, Mapping):
            continue
        raw = decision.get("queries")
        if not isinstance(raw, list):
            continue
        found.extend(
            _normalize_query(item)
            for item in raw
            if isinstance(item, str) and item.strip()
        )
    return found


def _max_streak(values: Sequence[str]) -> int:
    longest = current = 0
    previous: str | None = None
    for value in values:
        current = current + 1 if value == previous else 1
        previous = value
        longest = max(longest, current)
    return longest


def _deficiency_fingerprints(
    events: Sequence[Any], model_io: Sequence[Mapping[str, Any]]
) -> list[str]:
    values: list[str] = []
    for source in [*(getattr(event, "payload", event) for event in events), *model_io]:
        for key, value in _walk(source):
            if key not in _DEFICIENCY_KEYS:
                continue
            rows = value if isinstance(value, list) else [value]
            normalized = [str(row).strip().casefold() for row in rows if str(row).strip()]
            if normalized:
                values.append(_hash(normalized))
    return values


def _malformed_count(events: Sequence[Any], model_io: Sequence[Mapping[str, Any]]) -> int:
    """Count malformed research-control turns without flagging report retries.

    A stage-bearing model I/O record has enough context to distinguish the
    research controller from the report writer/reviewer.  Only the former is
    research-loop thrash.  Older event/model records do not carry a stage, so
    their historical malformed-field behavior remains unchanged.
    """
    count = 0
    sources: list[Any] = [*(getattr(event, "payload", event) for event in events)]
    for source in sources:
        for key, value in _walk(source):
            if key in _MALFORMED_KEYS and bool(value):
                count += 1
        if isinstance(source, Mapping) and str(source.get("type") or "").lower() in {
            "malformed",
            "parse_error",
        }:
            count += 1
    for source in model_io:
        stage = source.get("stage")
        if stage is not None:
            normalized_stage = re.sub(r"[^a-z0-9]+", "_", str(stage).casefold()).strip("_")
            if normalized_stage not in _RESEARCH_CONTROL_STAGES:
                continue
        for key, value in _walk(source):
            if key in _MALFORMED_KEYS and bool(value):
                count += 1
        if str(source.get("type") or "").lower() in {"malformed", "parse_error"}:
            count += 1
    return count


def _mapping_rows(value: Any) -> list[Mapping[str, Any]]:
    """Return nested mappings once, preserving wire order.

    Live frames commonly wrap the same observation as ``event.tool_result``
    while replay frames put it at the top level.  Looking at mappings instead
    of individual count fields lets the no-progress calculation keep the
    observation's round and success/failure status together.
    """
    rows: list[Mapping[str, Any]] = []

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            rows.append(item)
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return rows


def _summary_rows(events: Sequence[Any]) -> list[Mapping[str, Any]]:
    """Find explicit per-turn search summaries, de-duplicated by content."""
    found: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for event in events:
        payload = getattr(event, "payload", event)
        for row in _mapping_rows(payload):
            if row.get("kind") != "search_turn_summary":
                continue
            fingerprint = _hash(row)
            if fingerprint not in seen:
                seen.add(fingerprint)
                found.append(row)
    return found


def _observation_rows(events: Sequence[Any]) -> list[tuple[str, Mapping[str, Any]]]:
    """Return legacy search observations keyed by their round when present.

    Older harness frames have no explicit turn summary.  They do expose one
    observation per query, so grouping those records by ``round`` prevents
    three zero-added queries in one turn from looking like three turns.
    """
    found: list[tuple[str, Mapping[str, Any]]] = []
    seen: set[str] = set()
    for event_index, event in enumerate(events):
        payload = getattr(event, "payload", event)
        phase = str(getattr(event, "phase", "")).casefold()
        if phase not in {"search", "extract", "gap"}:
            continue
        for row in _mapping_rows(payload):
            if not any(
                key in row for key in (*_COUNT_KEYS, "ok", "no_progress", "zero_progress")
            ):
                continue
            if "ok" not in row and "round" not in row and row is not payload:
                continue
            fingerprint = f"{event_index}:{_hash(row)}"
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            round_value = row.get("round")
            key = (
                f"{phase}:{round_value}" if round_value is not None else f"event:{event_index}"
            )
            found.append((key, row))
    return found


def _row_progress(row: Mapping[str, Any]) -> tuple[bool, bool]:
    """Return ``(successful, added_evidence)`` for one observation."""
    if row.get("ok") is False:
        return False, False
    for key in _COUNT_KEYS:
        value = row.get(key)
        if isinstance(value, (int, float)):
            return True, value > 0
    if bool(row.get("no_progress") or row.get("zero_progress")):
        return True, False
    return True, True


def _no_progress_streak(events: Sequence[Any]) -> tuple[int, int]:
    """Return ``(longest_zero_yield_turn_streak, retrieval_failure_turns)``.

    A turn is no-progress only when at least one query completed successfully
    and no successful query added evidence.  A turn containing only retrieval
    failures remains visible as a failure signal but is not model thrash.
    """
    summaries = _summary_rows(events)
    if summaries:
        rows = summaries
        groups: list[tuple[bool, bool]] = []
        failure_turns = 0
        for row in rows:
            queries = row.get("queries", 0)
            failed = row.get("failed_queries", 0)
            if not isinstance(queries, int) or queries <= 0:
                continue
            if not isinstance(failed, int) or failed < 0:
                failed = 0
            successful = queries > failed
            added = row.get("new_admitted")
            progressed = isinstance(added, (int, float)) and added > 0
            groups.append((successful, progressed))
            if failed == queries:
                failure_turns += 1
    else:
        grouped: dict[str, list[Mapping[str, Any]]] = {}
        order: list[str] = []
        for key, row in _observation_rows(events):
            if key not in grouped:
                grouped[key] = []
                order.append(key)
            grouped[key].append(row)
        groups = []
        failure_turns = 0
        for key in order:
            observations = grouped[key]
            outcomes = [_row_progress(row) for row in observations]
            successful = any(ok for ok, _added in outcomes)
            progressed = any(added for ok, added in outcomes if ok)
            if not successful and outcomes:
                failure_turns += 1
            groups.append((successful, progressed))

    longest = current = 0
    for successful, progressed in groups:
        zero_yield = successful and not progressed
        current = current + 1 if zero_yield else 0
        longest = max(longest, current)
    return longest, failure_turns


def analyze_thrash(
    events: Sequence[Any],
    *,
    model_io: Sequence[Mapping[str, Any]] = (),
    max_repeated_query: int = 2,
    max_no_progress_streak: int = 2,
    max_repeated_deficiency: int = 1,
) -> dict[str, Any]:
    """Return deterministic thrash facts and hard findings for one run."""
    # Prefer the structured researcher decisions when inspection is enabled:
    # those include repeated queries that the host correctly refused to run.
    # Falling back to wire search events keeps replay/fake mode useful.
    queries = _decision_queries(model_io) or _queries(events)
    counts: dict[str, int] = {}
    for query in queries:
        counts[query] = counts.get(query, 0) + 1
    repeated_deficiencies = _deficiency_fingerprints(events, model_io)
    deficiency_counts: dict[str, int] = {}
    for fingerprint in repeated_deficiencies:
        deficiency_counts[fingerprint] = deficiency_counts.get(fingerprint, 0) + 1
    no_progress, retrieval_failure_turns = _no_progress_streak(events)
    malformed = _malformed_count(events, model_io)
    findings: list[dict[str, Any]] = []
    for query, count in sorted(counts.items()):
        if count > max_repeated_query:
            findings.append(
                {
                    "code": "REPEATED_QUERY",
                    "severity": "hard",
                    "count": count,
                    "query_hash": _hash(query),
                }
            )
    if no_progress > max_no_progress_streak:
        findings.append({"code": "NO_PROGRESS", "severity": "hard", "streak": no_progress})
    if malformed:
        findings.append({"code": "MALFORMED_TURN", "severity": "hard", "count": malformed})
    for fingerprint, count in sorted(deficiency_counts.items()):
        if count > max_repeated_deficiency:
            findings.append(
                {
                    "code": "REPEATED_DEFICIENCY",
                    "severity": "hard",
                    "count": count,
                    "fingerprint": fingerprint,
                }
            )
    return {
        "passed": not findings,
        "signals": {
            "query_count": len(queries),
            "unique_query_count": len(counts),
            "max_repeated_query": max(counts.values(), default=0),
            "max_repeated_query_streak": _max_streak(queries),
            "max_no_progress_streak": no_progress,
            "retrieval_failure_turns": retrieval_failure_turns,
            "malformed_turns": malformed,
            "max_repeated_deficiency": max(deficiency_counts.values(), default=0),
            "model_io_count": len(model_io),
        },
        "findings": findings,
    }


__all__ = ["analyze_thrash"]
