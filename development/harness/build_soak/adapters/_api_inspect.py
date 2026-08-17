"""Bounded inspect aggregation extracted behind the disco_api compatibility facade."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ._api_types import (
    _INSPECT_AGGREGATION_REASON_ORDER,
    _INSPECT_PROJECTIONS,
)


@dataclass
class _InspectTraceAggregation:
    """Canonical, overlap-proven aggregate of bounded inspect snapshots."""

    conversation_id: str
    canonical_events: dict[int, bytes] = field(default_factory=dict)
    sample_count: int = 0
    accepted_sample_count: int = 0
    event_bearing_sample_count: int = 0
    pretrace_unavailable_count: int = 0
    unavailable_sample_count: int = 0
    malformed_sample_count: int = 0
    overlap_sample_count: int = 0
    conflict_count: int = 0
    source_max_dropped_count: int = 0
    last_source_dropped_count: int | None = None
    last_snapshot_seqs: tuple[int, ...] = ()
    reasons: set[str] = field(default_factory=set)
    finalized: bool = False
    # Single-use license for a harness-declared stack restart; see
    # note_expected_stack_restart. Counted for auditability, not rendered.
    restart_pending: bool = False
    restart_outage_seen: bool = False
    declared_restart_count: int = 0

    @staticmethod
    def _canonical_event(event: dict[str, Any]) -> bytes:
        return json.dumps(
            event,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")

    @staticmethod
    def _projection_for_event(event: dict[str, Any]) -> tuple[str, bool]:
        """Return (projection field, kind_is_outer_discriminator).

        Inspect currently flattens ``{seq, kind=outer, **span_fields}``. A
        request-budget span legitimately has a payload field named ``kind``
        (for example ``soft``), which overwrites the outer ``span`` label. The
        exact span signature remains closed: non-empty ``span`` plus one of the
        three observability event phases. Any other discriminator collision is
        ambiguous and therefore rejected rather than silently losing a
        projection.
        """

        kind = event.get("kind")
        if not isinstance(kind, str) or not kind:
            raise ValueError("inspect event discriminator is not a non-empty string")
        span_signature = (
            isinstance(event.get("span"), str)
            and bool(event["span"])
            and event.get("event") in {"start", "end", "point"}
        )
        if kind == "span":
            if not span_signature:
                raise ValueError("inspect span event has no exact span signature")
            return "spans", True
        projection = _INSPECT_PROJECTIONS.get(kind)
        if projection is not None:
            if span_signature:
                raise ValueError("inspect event discriminator collides with a span")
            return projection, True
        if span_signature:
            return "spans", False
        raise ValueError("inspect event discriminator is ambiguous")

    def note_expected_pretrace_absence(self) -> None:
        self.sample_count += 1
        self.pretrace_unavailable_count += 1

    def note_expected_stack_restart(self) -> None:
        """Arm a single-use license for the outage THIS harness is about to cause.

        A `restart_after_terminal` scenario kills the agent-server on purpose, so
        the poller's next samples cannot reach it. That unreachability is not
        evidence loss and must not be scored as such — but the exemption has to be
        exactly as narrow as the declared act, or it becomes a blanket excuse.

        So: armed only by the drive, immediately before it restarts the stack;
        covers only unreachability, never a content conflict or a sequence
        regression; and consumed by the first accepted post-restart sample, after
        which unavailability is a real reason again. The samples still increment
        `unavailable_sample_count`, so the window stays auditable in the rendered
        aggregate without any change to its shape.

        This is only sound because the trace itself now survives the restart (the
        inspect journal): the post-restart stream continues the same monotonic
        sequence rather than renumbering from 1. If that durability regresses, the
        conflict and regression reasons still fire and still fail the run.
        """
        self.declared_restart_count += 1
        self.restart_pending = True

    def note_unavailable(self) -> None:
        self.sample_count += 1
        self.unavailable_sample_count += 1
        if not self.restart_pending:
            self.reasons.add("inspect_unavailable")
            return
        # The declared outage has now actually been observed. Until this point
        # the license must NOT be consumable: the drive arms it before issuing
        # the restart, and the poller keeps taking accepted samples for the
        # seconds the old server takes to die.
        self.restart_outage_seen = True

    def note_trace_disappeared(self) -> None:
        self.note_unavailable()
        if not self.restart_pending:
            self.reasons.add("source_reset")

    def note_poller_cancelled(self) -> None:
        self.reasons.add("poller_cancelled")

    def _reject_malformed(self, *reasons: str) -> None:
        self.malformed_sample_count += 1
        self.reasons.add("malformed_snapshot")
        self.reasons.update(reasons)

    def add_snapshot(self, snapshot: object) -> None:
        decoded = _decode_snapshot(self, snapshot)
        if decoded is None or not _snapshot_transition_is_valid(self, decoded):
            return
        _accept_snapshot(self, decoded)

    def finish(self) -> None:
        if self.finalized:
            return
        if self.accepted_sample_count == 0:
            self.reasons.add("no_valid_snapshot")
        self.finalized = True

    def active_agent_step_request_id(self) -> str | None:
        """Return the one currently open host-owned driver request, if provable.

        An ``agent.step`` span starts before the provider stream and ends in the
        span context manager on success, failure, timeout, or cancellation.  A
        latest unmatched start therefore proves that the product is still doing
        model work even though no durable ActionEvent exists yet.

        This is deliberately strict.  A tainted aggregate, a missing/foreign
        request id, duplicate starts, multiple unmatched requests, or any later
        driver-span event fails closed.  Those shapes must never turn an old
        observability record into an unbounded inactivity exemption.
        """

        if self.reasons or self.accepted_sample_count == 0:
            return None

        request_state = _active_agent_requests(self.canonical_events)
        if request_state is None:
            return None
        open_requests, latest = request_state
        if latest is None or latest[1] != "start" or len(open_requests) != 1:
            return None
        request_id = latest[2]
        return request_id if open_requests.get(request_id) == latest[0] else None

    def render(self) -> dict[str, Any]:
        events = _rendered_events(self.canonical_events)
        projections = _rendered_projections(events)
        reasons = [reason for reason in _INSPECT_AGGREGATION_REASON_ORDER if reason in self.reasons]
        lossless = self.finalized and self.accepted_sample_count > 0 and not reasons
        return {
            "conversation_id": self.conversation_id,
            "event_count": len(events),
            # This is the aggregate loss counter, not the source ring's.  It is
            # exactly zero only after the continuity proof closes.
            "dropped_event_count": 0 if lossless else None,
            "source_dropped_event_count": self.source_max_dropped_count,
            "events": events,
            **projections,
            "aggregation": _rendered_aggregation(self, events, reasons, lossless),
        }


@dataclass(frozen=True)
class _DecodedSnapshot:
    current: tuple[int, ...]
    encoded: dict[int, bytes]
    dropped: int


def _snapshot_header(
    owner: _InspectTraceAggregation, snapshot: object
) -> tuple[list[object], int] | None:
    owner.sample_count += 1
    if not isinstance(snapshot, dict):
        owner._reject_malformed()
        return None
    if snapshot.get("conversation_id") != owner.conversation_id:
        owner._reject_malformed("conversation_mismatch")
        return None
    events = snapshot.get("events")
    dropped = snapshot.get("dropped_event_count")
    event_count = snapshot.get("event_count")
    if (
        not isinstance(events, list)
        or type(dropped) is not int
        or dropped < 0
        or type(event_count) is not int
        or event_count != len(events)
    ):
        owner._reject_malformed()
        return None
    return events, dropped


def _decode_snapshot_event(
    owner: _InspectTraceAggregation,
    event: object,
    previous_seq: int | None,
) -> tuple[int, bytes] | None:
    if not isinstance(event, dict):
        owner._reject_malformed()
        return None
    seq = event.get("seq")
    if type(seq) is not int or seq <= 0:
        owner._reject_malformed()
        return None
    if previous_seq is not None and seq <= previous_seq:
        owner._reject_malformed("snapshot_sequence_regression")
        return None
    try:
        owner._projection_for_event(event)
        encoded = owner._canonical_event(event)
    except (TypeError, ValueError):
        owner._reject_malformed("ambiguous_event_discriminator")
        return None
    return seq, encoded


def _decode_snapshot(owner: _InspectTraceAggregation, snapshot: object) -> _DecodedSnapshot | None:
    header = _snapshot_header(owner, snapshot)
    if header is None:
        return None
    events, dropped = header
    seqs: list[int] = []
    encoded: dict[int, bytes] = {}
    for event in events:
        item = _decode_snapshot_event(owner, event, seqs[-1] if seqs else None)
        if item is None:
            return None
        seq, content = item
        seqs.append(seq)
        encoded[seq] = content
    return _DecodedSnapshot(tuple(seqs), encoded, dropped)


def _overlap_for(
    owner: _InspectTraceAggregation, current: tuple[int, ...]
) -> tuple[int, ...] | None:
    previous = owner.last_snapshot_seqs
    overlap = tuple(seq for seq in current if seq in set(previous))
    if not overlap:
        return ()
    owner.overlap_sample_count += 1
    first = overlap[0]
    if previous[previous.index(first) :] != overlap or current[: len(overlap)] != overlap:
        owner.reasons.add("snapshot_sequence_regression")
        return None
    return overlap


def _eviction_transition_is_valid(
    owner: _InspectTraceAggregation,
    current: tuple[int, ...],
    overlap: tuple[int, ...],
    dropped: int,
) -> bool:
    previous = owner.last_snapshot_seqs
    last_dropped = owner.last_source_dropped_count or 0
    if dropped > last_dropped and not overlap:
        owner.reasons.add("eviction_gap_no_overlap")
        return False
    if overlap and dropped > last_dropped:
        evicted = dropped - last_dropped
        if overlap != previous[min(evicted, len(previous)) :] or current[-1] <= previous[-1]:
            owner.reasons.update({"impossible_dropped_transition", "source_reset"})
            return False
    if dropped == owner.last_source_dropped_count and not set(previous) <= set(current):
        owner.reasons.add("snapshot_sequence_regression")
        return False
    return True


def _prior_snapshot_is_compatible(
    owner: _InspectTraceAggregation, decoded: _DecodedSnapshot
) -> bool:
    previous = owner.last_snapshot_seqs
    if not previous:
        return True
    current = decoded.current
    overlap = _overlap_for(owner, current)
    if overlap is None:
        return False
    if not overlap and current != previous:
        reason = (
            "eviction_gap_no_overlap"
            if decoded.dropped > (owner.last_source_dropped_count or 0)
            else "source_reset"
        )
        owner.reasons.add(reason)
        return False
    previous_max = previous[-1]
    if any(seq <= previous_max and seq not in owner.canonical_events for seq in current):
        owner.reasons.add("snapshot_sequence_regression")
        return False
    if current and current[-1] < previous_max:
        owner.reasons.update({"snapshot_sequence_regression", "source_reset"})
        return False
    return _eviction_transition_is_valid(owner, current, overlap, decoded.dropped)


def _snapshot_transition_is_valid(
    owner: _InspectTraceAggregation, decoded: _DecodedSnapshot
) -> bool:
    dropped = decoded.dropped
    owner.source_max_dropped_count = max(owner.source_max_dropped_count, dropped)
    if owner.event_bearing_sample_count == 0 and dropped > 0:
        owner.reasons.add("first_snapshot_already_dropped")
    if owner.last_source_dropped_count is not None and dropped < owner.last_source_dropped_count:
        owner.reasons.update({"dropped_count_regression", "source_reset"})
        return False
    conflicts = [
        seq
        for seq, content in decoded.encoded.items()
        if seq in owner.canonical_events and owner.canonical_events[seq] != content
    ]
    if conflicts:
        owner.conflict_count += len(conflicts)
        owner.reasons.add("event_content_conflict")
        return False
    return _prior_snapshot_is_compatible(owner, decoded)


def _accept_snapshot(owner: _InspectTraceAggregation, decoded: _DecodedSnapshot) -> None:
    owner.canonical_events.update(decoded.encoded)
    owner.accepted_sample_count += 1
    if owner.restart_outage_seen:
        owner.restart_pending = False
        owner.restart_outage_seen = False
    if decoded.current:
        owner.event_bearing_sample_count += 1
    owner.last_source_dropped_count = decoded.dropped
    owner.last_snapshot_seqs = decoded.current


def _active_agent_requests(
    canonical_events: dict[int, bytes],
) -> tuple[dict[str, int], tuple[int, str, str] | None] | None:
    open_requests: dict[str, int] = {}
    latest: tuple[int, str, str] | None = None
    for seq in sorted(canonical_events):
        try:
            event = json.loads(canonical_events[seq].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(event, dict):
            continue
        if event.get("span") != "agent.step" or event.get("role") != "agent_driver":
            continue
        phase = event.get("event")
        request_id = event.get("request_id")
        if phase not in {"start", "end"} or not isinstance(request_id, str) or not request_id:
            return None
        if phase == "start" and request_id in open_requests:
            return None
        if phase == "end" and request_id not in open_requests:
            return None
        if phase == "start":
            open_requests[request_id] = seq
        else:
            del open_requests[request_id]
        latest = (seq, phase, request_id)
    return open_requests, latest


def _rendered_events(canonical_events: dict[int, bytes]) -> list[dict[str, Any]]:
    return [json.loads(canonical_events[seq].decode("utf-8")) for seq in sorted(canonical_events)]


def _rendered_projections(
    events: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    projections: dict[str, list[dict[str, Any]]] = {
        field_name: [] for field_name in _INSPECT_PROJECTIONS.values()
    }
    for event in events:
        projection, kind_is_outer = _InspectTraceAggregation._projection_for_event(event)
        projections[projection].append(
            {
                key: value
                for key, value in event.items()
                if key != "seq" and (key != "kind" or not kind_is_outer)
            }
        )
    return projections


def _rendered_aggregation(
    owner: _InspectTraceAggregation,
    events: list[dict[str, Any]],
    reasons: list[str],
    lossless: bool,
) -> dict[str, Any]:
    if lossless:
        continuity_reason = (
            "overlap_proven"
            if owner.source_max_dropped_count > 0
            else "no_source_eviction_observed"
        )
    else:
        continuity_reason = reasons[0] if reasons else "collection_not_finalized"
    seqs = sorted(owner.canonical_events)
    return {
        "schema_version": 1,
        "sample_count": owner.sample_count,
        "accepted_sample_count": owner.accepted_sample_count,
        "event_bearing_sample_count": owner.event_bearing_sample_count,
        "pretrace_unavailable_count": owner.pretrace_unavailable_count,
        "unavailable_sample_count": owner.unavailable_sample_count,
        "malformed_sample_count": owner.malformed_sample_count,
        "overlap_sample_count": owner.overlap_sample_count,
        "source_max_dropped_count": owner.source_max_dropped_count,
        "unique_event_count": len(events),
        "first_retained_seq": seqs[0] if seqs else None,
        "last_retained_seq": seqs[-1] if seqs else None,
        "continuity": "complete" if lossless else "incomplete",
        "continuity_reason": continuity_reason,
        "conflict_count": owner.conflict_count,
        "declared_restart_count": owner.declared_restart_count,
        "failure_reasons": reasons,
        "lossless": lossless,
        "finalized": owner.finalized,
    }
