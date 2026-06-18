"""Content equality ignoring volatile fields — event-state-contract.md §6.3.

Stuck detection (BoD §12.5) and dedup must compare events by *semantic* content,
not by identity. Volatile fields legitimately vary between otherwise-equivalent
events; they are enumerated here (binding) and excluded from comparison:

    id, seq, timestamp, meta, and the correlation ids:
    ToolCall.call_id, ObservationEvent.action_id, AgentErrorEvent.action_id,
    ActionEvent.llm_response_id, LLMMessage.tool_call_id.

This function lives in `core` (not the loop) because volatility is a property of
the event schema, not of the loop that consumes it.
"""

from __future__ import annotations

from .events import (
    ActionEvent,
    AgentErrorEvent,
    Event,
    MessageEvent,
    ObservationEvent,
)

# Envelope-level volatile fields excluded from the generic fallback comparison.
_VOLATILE_FIELDS = {"id", "seq", "timestamp", "meta"}


def event_content_eq(a: Event, b: Event, *, ignore_thought: bool = False) -> bool:
    """[CONTRACT] True if two events are semantically equal ignoring volatile
    fields. Used by stuck detection and idempotency reasoning. Same-type only.

    When `ignore_thought=True` the ActionEvent.thought field is excluded from
    the comparison. Pass this flag ONLY at stuck-detection call-sites — a model
    that paraphrases its reasoning each turn while emitting the identical tool
    call must still be detected as a repeat. Leave idempotency / dedup callers
    on the default so genuinely different reasoning is preserved.
    """
    if type(a) is not type(b) or a.source != b.source:
        return False

    if isinstance(a, ActionEvent) and isinstance(b, ActionEvent):
        return (
            (ignore_thought or a.thought == b.thought)
            and a.tool_call.tool_name == b.tool_call.tool_name
            and a.tool_call.arguments == b.tool_call.arguments
        )
    if isinstance(a, ObservationEvent) and isinstance(b, ObservationEvent):
        return (
            a.tool_result.tool_name == b.tool_result.tool_name
            and a.tool_result.content == b.tool_result.content
            and a.tool_result.success == b.tool_result.success
        )
    if isinstance(a, AgentErrorEvent) and isinstance(b, AgentErrorEvent):
        return a.error == b.error
    if isinstance(a, MessageEvent) and isinstance(b, MessageEvent):
        return a.message.role == b.message.role and a.message.content == b.message.content

    # Generic fallback for the remaining event types (status, error,
    # condensation): compare everything except the envelope-level volatiles.
    return a.model_dump(exclude=_VOLATILE_FIELDS) == b.model_dump(exclude=_VOLATILE_FIELDS)
