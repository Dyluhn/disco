"""Receipt-grounded redundant-read escape policy."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..effects import CoverageSpan, EffectCapability, MutationReceipt, ObservationReceipt
from ..events import (
    ActionEvent,
    CondensationEvent,
    Event,
    EventSource,
    ObservationEvent,
    ToolCall,
)
from . import signals
from .resource_context import merge_spans
from .stuck import successful_mutation_with_receipt

_STUCK_ESCAPE_EQUIVALENT_READ_BYPASS_TOOLS = frozenset(
    {"shell", "shell_exec", "code_exec", "delegate_explore"}
)


def _paired_trusted_observation(
    event: Event,
    actions: dict[str, ActionEvent],
    boundary_seq: int,
) -> tuple[ObservationEvent, ActionEvent] | None:
    if not isinstance(event, ObservationEvent):
        return None
    if event.source != EventSource.ENVIRONMENT or event.seq is None:
        return None
    if event.seq <= boundary_seq:
        return None
    action = actions.get(event.action_id or "")
    if action is None or action.tool_call is None or action.seq is None:
        return None
    if action.source != EventSource.AGENT:
        return None
    if not boundary_seq < action.seq < event.seq:
        return None
    if event.tool_result.call_id != action.tool_call.call_id:
        return None
    return event, action


def trusted_receipt_since(events: list[Event], boundary_seq: int) -> bool:
    actions = {
        event.id: event
        for event in events
        if isinstance(event, ActionEvent) and event.tool_call is not None
    }
    return any(
        successful_mutation_with_receipt(*pair)
        for event in events
        if (pair := _paired_trusted_observation(event, actions, boundary_seq)) is not None
    )


def stuck_escape_blocked_tools_for_step(
    events: list[Event],
) -> frozenset[str]:
    escape_seq = signals.stuck_escape_seq(events)
    if escape_seq is None:
        return frozenset()
    if "file_read" not in signals.stuck_escape_blocked_tools(events):
        return frozenset()
    if trusted_receipt_since(events, escape_seq):
        return frozenset()
    return _STUCK_ESCAPE_EQUIVALENT_READ_BYPASS_TOOLS


@dataclass
class _ReceiptState:
    latest_digest: str | None = None
    latest_digest_seq: int = -1
    last_path_mutation_seq: int = -1
    covered_by_digest: dict[str, set[tuple[int, int]]] = field(default_factory=dict)
    total_by_digest: dict[str, int | None] = field(default_factory=dict)
    coverage_seq_by_digest: dict[str, int] = field(default_factory=dict)
    opaque_mutation_seq: int = -1


def _visible(
    seq: int | None,
    forgotten: list[tuple[int, int]],
) -> bool:
    return seq is not None and all(not start <= seq <= end for start, end in forgotten)


def _record_mutation_receipt(
    state: _ReceiptState,
    event: ObservationEvent,
    receipt: MutationReceipt,
    path: str,
) -> None:
    assert event.seq is not None
    resource = receipt.resource
    if resource.namespace != "workspace.file" or resource.identifier != path:
        return
    if event.seq < state.latest_digest_seq:
        return
    state.latest_digest = receipt.after.digest if receipt.after is not None else None
    state.latest_digest_seq = event.seq
    state.last_path_mutation_seq = event.seq


def _record_observation_receipt(
    state: _ReceiptState,
    event: ObservationEvent,
    receipt: ObservationReceipt,
    path: str,
    forgotten: list[tuple[int, int]],
) -> None:
    assert event.seq is not None
    resource = receipt.revision.resource
    if receipt.capability is not EffectCapability.WORKSPACE_CONTENT_READ:
        return
    if resource.namespace != "workspace.file" or resource.identifier != path:
        return
    digest = receipt.revision.digest
    if event.seq > state.latest_digest_seq:
        state.latest_digest = digest
        state.latest_digest_seq = event.seq
    if not _visible(event.seq, forgotten):
        return
    spans = state.covered_by_digest.setdefault(digest, set())
    if receipt.complete and receipt.coverage.total is not None:
        spans.add((0, receipt.coverage.total))
    spans.update((span.start, span.end) for span in receipt.coverage.spans)
    if receipt.coverage.total is not None:
        state.total_by_digest[digest] = receipt.coverage.total
    state.coverage_seq_by_digest[digest] = max(
        state.coverage_seq_by_digest.get(digest, -1),
        event.seq,
    )


def _consume_effect_receipt(
    state: _ReceiptState,
    event: ObservationEvent,
    receipt: object,
    path: str,
    forgotten: list[tuple[int, int]],
) -> bool:
    if isinstance(receipt, MutationReceipt):
        _record_mutation_receipt(state, event, receipt, path)
        return True
    if isinstance(receipt, ObservationReceipt):
        _record_observation_receipt(
            state,
            event,
            receipt,
            path,
            forgotten,
        )
    return False


def _consume_receipt_observation(
    state: _ReceiptState,
    event: Event,
    actions: dict[str, ActionEvent],
    path: str,
    forgotten: list[tuple[int, int]],
) -> None:
    if not isinstance(event, ObservationEvent):
        return
    if (
        event.source != EventSource.ENVIRONMENT
        or event.seq is None
        or not event.tool_result.success
    ):
        return
    action = actions.get(event.action_id or "")
    if action is None or action.tool_call is None:
        return
    if action.source != EventSource.AGENT or event.tool_result.call_id != action.tool_call.call_id:
        return
    named_mutation = False
    for receipt in event.tool_result.effect_receipts:
        named_mutation = (
            _consume_effect_receipt(
                state,
                event,
                receipt,
                path,
                forgotten,
            )
            or named_mutation
        )
    if (
        not named_mutation
        and successful_mutation_with_receipt(event, action)
        and event.seq > state.opaque_mutation_seq
    ):
        state.opaque_mutation_seq = event.seq


def _requested_range(
    tool_call: ToolCall,
    total: int,
) -> tuple[int, int, int]:
    raw_offset = tool_call.arguments.get("offset")
    raw_limit = tool_call.arguments.get("limit")
    offset = raw_offset if type(raw_offset) is int and raw_offset >= 1 else 1
    limit = raw_limit if type(raw_limit) is int and raw_limit >= 1 else None
    start = offset - 1
    end = total if limit is None else min(total, start + limit)
    return offset, start, end


def _range_is_covered(
    covered: set[tuple[int, int]],
    start: int,
    end: int,
    total: int,
) -> bool:
    if start >= total:
        return True
    cursor = start
    for span_start, span_end in sorted(covered):
        if span_start > cursor:
            break
        cursor = max(cursor, span_end)
        if cursor >= end:
            return True
    return cursor >= end


def _receipt_state(
    events: list[Event],
    path: str,
) -> _ReceiptState:
    forgotten = [
        (event.forgotten_start_seq, event.forgotten_end_seq)
        for event in events
        if isinstance(event, CondensationEvent)
    ]
    actions = {
        event.id: event
        for event in events
        if isinstance(event, ActionEvent) and event.tool_call is not None
    }
    state = _ReceiptState()
    for event in events:
        _consume_receipt_observation(
            state,
            event,
            actions,
            path,
            forgotten,
        )
    return state


def stuck_escape_redundant_read_refusal(
    events: list[Event],
    tool_call: ToolCall,
) -> dict[str, object] | None:
    escape_seq = signals.stuck_escape_seq(events)
    if escape_seq is None or tool_call.tool_name != "file_read":
        return None
    if "file_read" not in signals.stuck_escape_blocked_tools(events):
        return None
    path = signals.normalized_workspace_read_path(tool_call.arguments.get("path"))
    if path is None:
        return None
    state = _receipt_state(events, path)
    if state.latest_digest is None:
        return None
    covered = state.covered_by_digest.get(state.latest_digest)
    total = state.total_by_digest.get(state.latest_digest)
    if not covered or total is None:
        return None
    coverage_seq = state.coverage_seq_by_digest.get(state.latest_digest, -1)
    if state.opaque_mutation_seq > coverage_seq:
        return None
    offset, request_start, request_end = _requested_range(tool_call, total)
    if not _range_is_covered(covered, request_start, request_end, total):
        return None
    held = merge_spans(tuple(CoverageSpan(start=start, end=end) for start, end in sorted(covered)))
    return {
        "path": path,
        "requested_start_line": min(offset, total + 1),
        "requested_end_line": max(request_end, min(offset, total + 1)),
        "total_lines": total,
        "held_spans": [(span.start + 1, span.end) for span in held],
        "digest_prefix": state.latest_digest[:12],
        "refusal_floor_seq": max(
            escape_seq,
            state.last_path_mutation_seq,
            state.opaque_mutation_seq,
        ),
    }
