"""Validation of bounded inspect aggregates and live findings."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..events import KIND_ACTION, action_id_of, kind_of
from ..oracles.thrash import ProgressEpochs
from ._api_inspect import _InspectTraceAggregation
from ._api_types import (
    _INSPECT_AGGREGATION_REASON_ORDER,
    _INSPECT_PROJECTIONS,
)

_INSPECT_TRACE_TOP_LEVEL_KEYS = frozenset(
    {
        "conversation_id",
        "event_count",
        "dropped_event_count",
        "source_dropped_event_count",
        "events",
        "routing_decisions",
        "spans",
        "tool_scopes",
        "progress_shadows",
        "aggregation",
    }
)
_INSPECT_AGGREGATION_KEYS = frozenset(
    {
        "schema_version",
        "sample_count",
        "accepted_sample_count",
        "event_bearing_sample_count",
        "pretrace_unavailable_count",
        "unavailable_sample_count",
        "malformed_sample_count",
        "overlap_sample_count",
        "source_max_dropped_count",
        "unique_event_count",
        "first_retained_seq",
        "last_retained_seq",
        "continuity",
        "continuity_reason",
        "conflict_count",
        "declared_restart_count",
        "failure_reasons",
        "lossless",
        "finalized",
    }
)
_INSPECT_AGGREGATION_COUNTER_KEYS = (
    "sample_count",
    "accepted_sample_count",
    "event_bearing_sample_count",
    "pretrace_unavailable_count",
    "unavailable_sample_count",
    "malformed_sample_count",
    "overlap_sample_count",
    "source_max_dropped_count",
    "unique_event_count",
    "conflict_count",
    "declared_restart_count",
)


def _exact_nonneg_int(value: object) -> bool:
    return type(value) is int and value >= 0


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _inspect_events_and_projections(
    trace: dict[str, Any], flag: Callable[[str], None]
) -> tuple[list[dict[str, Any]] | None, dict[str, list[dict[str, Any]]]]:
    """Validate the canonical event list and re-derive every projection.

    Returns ``(events, derived_projections)``; ``events`` is ``None`` when the
    list itself is unusable (further event-derived checks are meaningless)."""

    derived: dict[str, list[dict[str, Any]]] = {
        field_name: [] for field_name in _INSPECT_PROJECTIONS.values()
    }
    events = trace.get("events")
    if not isinstance(events, list):
        flag("events_not_a_list")
        return None, derived
    last_seq: int | None = None
    for event in events:
        if not isinstance(event, dict):
            flag("event_shape")
            return None, derived
        seq = event.get("seq")
        if type(seq) is not int or seq <= 0 or (last_seq is not None and seq <= last_seq):
            flag("event_sequence")
            return None, derived
        last_seq = seq
        try:
            _canonical_json_bytes(event)
            projection, kind_is_outer = _InspectTraceAggregation._projection_for_event(event)
        except (TypeError, ValueError):
            flag("event_not_canonical")
            return None, derived
        derived[projection].append(
            {
                key: value
                for key, value in event.items()
                if key != "seq" and (key != "kind" or not kind_is_outer)
            }
        )
    return [dict(event) for event in events], derived


@dataclass(frozen=True)
class _AggregateValidation:
    aggregation: dict[str, Any]
    counters_valid: bool
    reasons: list[str] | None
    lossless: bool
    finalized: bool


def _aggregate_counters_are_valid(aggregation: dict[str, Any], flag: Callable[[str], None]) -> bool:
    valid = True
    for key in _INSPECT_AGGREGATION_COUNTER_KEYS:
        if not _exact_nonneg_int(aggregation.get(key)):
            flag(f"counter:{key}")
            valid = False
    return valid


def _canonical_failure_reasons(aggregation: dict[str, Any]) -> list[str] | None:
    raw = aggregation.get("failure_reasons")
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        return None
    canonical = [reason for reason in _INSPECT_AGGREGATION_REASON_ORDER if reason in set(raw)]
    return canonical if list(raw) == canonical else None


def _validate_aggregate_header(
    trace: dict[str, Any],
    conversation_id: str,
    flag: Callable[[str], None],
) -> _AggregateValidation | None:
    if set(trace) != _INSPECT_TRACE_TOP_LEVEL_KEYS:
        flag("top_level_keys")
    cid = trace.get("conversation_id")
    if not isinstance(cid, str) or cid != conversation_id:
        flag("conversation_id")
    aggregation = trace.get("aggregation")
    if not isinstance(aggregation, dict):
        flag("aggregation_missing")
        return None
    if set(aggregation) != _INSPECT_AGGREGATION_KEYS:
        flag("aggregation_keys")
    schema_version = aggregation.get("schema_version")
    if type(schema_version) is not int or schema_version != 1:
        flag("schema_version")
    counters_valid = _aggregate_counters_are_valid(aggregation, flag)
    lossless = aggregation.get("lossless")
    finalized = aggregation.get("finalized")
    if type(lossless) is not bool or type(finalized) is not bool:
        flag("lossless_finalized_types")
        return None
    reasons = _canonical_failure_reasons(aggregation)
    if reasons is None:
        flag("failure_reasons")
    return _AggregateValidation(
        aggregation=aggregation,
        counters_valid=counters_valid,
        reasons=reasons,
        lossless=lossless,
        finalized=finalized,
    )


def _validate_projection_counts(
    trace: dict[str, Any],
    aggregation: dict[str, Any],
    flag: Callable[[str], None],
) -> list[dict[str, Any]] | None:
    events, derived = _inspect_events_and_projections(trace, flag)
    if events is None:
        return None
    count = len(events)
    event_count = trace.get("event_count")
    if (
        type(event_count) is not int
        or event_count != count
        or aggregation.get("unique_event_count") != count
        or type(aggregation.get("unique_event_count")) is not int
    ):
        flag("event_count")
    _validate_retained_seqs(events, aggregation, flag)
    _validate_projections(trace, derived, flag)
    return events


def _validate_retained_seqs(
    events: list[dict[str, Any]],
    aggregation: dict[str, Any],
    flag: Callable[[str], None],
) -> None:
    expected_seqs = {
        "first_retained_seq": events[0]["seq"] if events else None,
        "last_retained_seq": events[-1]["seq"] if events else None,
    }
    for key, expected in expected_seqs.items():
        actual = aggregation.get(key)
        if expected is None and actual is not None:
            flag(f"retained_seq:{key}")
        elif expected is not None and (type(actual) is not int or actual != expected):
            flag(f"retained_seq:{key}")


def _validate_projections(
    trace: dict[str, Any],
    derived: dict[str, list[dict[str, Any]]],
    flag: Callable[[str], None],
) -> None:
    for field_name in _INSPECT_PROJECTIONS.values():
        try:
            actual_bytes = _canonical_json_bytes(trace.get(field_name))
        except (TypeError, ValueError):
            flag(f"projection:{field_name}")
            continue
        if actual_bytes != _canonical_json_bytes(derived[field_name]):
            flag(f"projection:{field_name}")


def _aggregate_counter(aggregation: dict[str, Any], key: str) -> int:
    value = aggregation.get(key)
    return value if type(value) is int else 0


def _validate_lossless_state(
    trace: dict[str, Any],
    context: _AggregateValidation,
    flag: Callable[[str], None],
) -> None:
    aggregation = context.aggregation
    reasons = context.reasons or []
    accepted = _aggregate_counter(aggregation, "accepted_sample_count")
    expected_lossless = context.finalized and accepted > 0 and not reasons
    if context.lossless != expected_lossless:
        flag("lossless_incoherent")
    _validate_dropped_count(trace, context.lossless, flag)
    _validate_continuity(trace, context, accepted, flag)


def _validate_dropped_count(
    trace: dict[str, Any], lossless: bool, flag: Callable[[str], None]
) -> None:
    dropped = trace.get("dropped_event_count")
    if lossless and (type(dropped) is not int or dropped != 0):
        flag("dropped_event_count")
    elif not lossless and dropped is not None:
        flag("dropped_event_count")


def _validate_continuity(
    trace: dict[str, Any],
    context: _AggregateValidation,
    accepted: int,
    flag: Callable[[str], None],
) -> None:
    aggregation = context.aggregation
    reasons = context.reasons or []
    expected_continuity = "complete" if context.lossless else "incomplete"
    if aggregation.get("continuity") != expected_continuity:
        flag("continuity")
    source_max = _aggregate_counter(aggregation, "source_max_dropped_count")
    if context.lossless:
        expected_reason = "overlap_proven" if source_max > 0 else "no_source_eviction_observed"
    else:
        expected_reason = reasons[0] if reasons else "collection_not_finalized"
    if aggregation.get("continuity_reason") != expected_reason:
        flag("continuity_reason")
    top_source = trace.get("source_dropped_event_count")
    if type(top_source) is not int or top_source != source_max:
        flag("source_dropped_event_count")
    if context.finalized and accepted == 0 and "no_valid_snapshot" not in reasons:
        flag("no_valid_snapshot_missing")


def _validate_sample_accounting(
    events: list[dict[str, Any]] | None,
    context: _AggregateValidation,
    flag: Callable[[str], None],
) -> None:
    aggregation = context.aggregation

    def counter(key: str) -> int:
        return _aggregate_counter(aggregation, key)

    accepted = counter("accepted_sample_count")
    event_bearing = counter("event_bearing_sample_count")
    if (
        accepted
        + counter("pretrace_unavailable_count")
        + counter("unavailable_sample_count")
        + counter("malformed_sample_count")
        > counter("sample_count")
        or event_bearing > accepted
        or counter("overlap_sample_count") > counter("sample_count")
    ):
        flag("sample_accounting")
    if events is not None and (
        (bool(events) and event_bearing == 0) or (not events and event_bearing > 0)
    ):
        flag("sample_accounting")


def _validate_reason_coherence(
    context: _AggregateValidation,
    flag: Callable[[str], None],
) -> None:
    aggregation = context.aggregation
    reasons = context.reasons or []

    def counter(key: str) -> int:
        return _aggregate_counter(aggregation, key)

    if counter("conflict_count") > 0 and "event_content_conflict" not in reasons:
        flag("conflict_reason_incoherent")
    if (
        counter("unavailable_sample_count") > 0
        and "inspect_unavailable" not in reasons
        and counter("declared_restart_count") == 0
    ):
        flag("unavailable_reason_incoherent")
    if counter("malformed_sample_count") > 0 and "malformed_snapshot" not in reasons:
        flag("malformed_reason_incoherent")
    if (
        context.lossless
        and counter("source_max_dropped_count") > 0
        and counter("overlap_sample_count") == 0
    ):
        flag("overlap_continuity_incoherent")


def inspect_aggregate_violations(trace: object, *, conversation_id: str) -> list[str]:
    """Bounded internal-consistency violations of a rendered inspect aggregate.

    The required-inspect gate must not trust the caller-owned ``lossless`` /
    ``finalized`` / ``dropped_event_count`` assertions: a forged or tampered
    dict can wear those values around an internally inconsistent trace.  This
    validator re-derives every projection and coherence fact from the canonical
    events and returns a deduplicated, bounded list of short violation labels;
    an empty list means the aggregate is exactly the shape ``render`` produces
    for this conversation.  It shares the aggregation's own constants so the
    producer and the gate cannot drift apart.
    """

    if not isinstance(trace, dict):
        return ["trace_not_an_object"]
    violations: list[str] = []

    def flag(label: str) -> None:
        if label not in violations:
            violations.append(label)

    context = _validate_aggregate_header(trace, conversation_id, flag)
    if context is None:
        return violations
    events = _validate_projection_counts(trace, context.aggregation, flag)
    if context.reasons is None or not context.counters_valid:
        return violations
    _validate_lossless_state(trace, context, flag)
    _validate_sample_accounting(events, context, flag)
    _validate_reason_coherence(context, flag)
    return violations


def _finding_referenced_seqs(failing_result: dict[str, Any]) -> list[int] | None:
    facts = failing_result.get("facts")
    if not isinstance(facts, dict):
        return None
    referenced: list[int] = []
    for key in ("action_seqs", "seqs", "cleanup_seqs"):
        values = facts.get(key)
        if isinstance(values, list):
            referenced.extend(value for value in values if type(value) is int)
    markers = facts.get("terminal_markers")
    if isinstance(markers, list):
        referenced.extend(
            marker["seq"]
            for marker in markers
            if isinstance(marker, dict) and type(marker.get("seq")) is int
        )
    return referenced


def _progress_epoch_after(events: list[dict[str, Any]], watermark: int) -> bool:
    known_action_ids = frozenset(
        str(action_id_of(event))
        for event in events
        if kind_of(event) == KIND_ACTION and action_id_of(event)
    )
    epochs = ProgressEpochs()
    for event in events:
        crossed = epochs.crosses(event, action_ids=known_action_ids)
        seq = event.get("seq")
        if crossed and type(seq) is int and seq > watermark:
            return True
    return False


def _live_thrash_finding_is_current(
    events: list[dict[str, Any]], failing_result: dict[str, Any]
) -> bool:
    """Whether a failing oracle result still describes the run's CURRENT state.

    k6g F2.4: historical lifetime evidence alone must never kill a resumed
    productive run. A finding is current only when NO trusted progress-epoch
    boundary (receipt-backed mutation, approved plan transition, user turn,
    typed blocking obligation) landed after the finding's latest contributing
    event. A finding that references no event seqs (e.g. hidden-repair counts
    from inspect spans) stays current — there is no progress evidence to
    supersede it, and failing open would un-bound genuine spend.
    """

    referenced = _finding_referenced_seqs(failing_result)
    if referenced is None:
        return True
    if not referenced:
        return True
    # The tracker must see EVERY event in order — a preview-generation
    # replacement is only visible as a change from the previously established
    # generation, so skipping the earlier events would hide the transition. Only
    # crossings AFTER the watermark count, which is what the filter below does.
    return not _progress_epoch_after(events, max(referenced))
