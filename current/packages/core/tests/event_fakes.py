"""Shared event builders for the core spine tests.

These keep each test focused on the behavior under test rather than on event
construction boilerplate. They mirror the shapes in event-state-contract.md §2.
"""

from __future__ import annotations

from disco.core import (
    ActionEvent,
    AgentErrorEvent,
    CondensationEvent,
    ConversationStatus,
    ErrorEvent,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    StatusEvent,
    ToolCall,
    ToolResult,
)


def user_msg(text: str = "hello") -> MessageEvent:
    return MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content=text))


def agent_msg(text: str = "thinking") -> MessageEvent:
    return MessageEvent(
        source=EventSource.AGENT, message=LLMMessage(role="assistant", content=text)
    )


def action(thought: str = "do it", tool: str = "shell", args: dict | None = None) -> ActionEvent:
    return ActionEvent(thought=thought, tool_call=ToolCall(tool_name=tool, arguments=args or {}))


def observation(
    action_id: str = "evt_x", content: str = "ok", tool: str = "shell", success: bool = True
) -> ObservationEvent:
    return ObservationEvent(
        tool_result=ToolResult(call_id="c", tool_name=tool, success=success, content=content),
        action_id=action_id,
    )


def agent_error(err: str = "boom", action_id: str | None = None) -> AgentErrorEvent:
    return AgentErrorEvent(error=err, action_id=action_id)


def status(s: ConversationStatus) -> StatusEvent:
    return StatusEvent(status=s)


def fatal(code: str = "MaxIterationsReached", detail: str = "ceiling hit") -> ErrorEvent:
    return ErrorEvent(code=code, detail=detail)


def tombstone(start: int, end: int, summary: str = "[summary]") -> CondensationEvent:
    return CondensationEvent(forgotten_start_seq=start, forgotten_end_seq=end, summary=summary)


def with_seqs(events: list[Event], start: int = 1) -> list[Event]:
    """Assign contiguous seqs as the store would, without a store (for pure
    reconstruct/View tests)."""
    return [e.model_copy(update={"seq": start + i}) for i, e in enumerate(events)]
