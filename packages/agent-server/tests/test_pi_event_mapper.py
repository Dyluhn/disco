"""Tests for the PR I1 Pi event mapper (`pi_event_mapper.map_pi_event`).

Input frames are built to match `packages/pi-kernel/src/events.ts` EXACTLY:
- `message_update`/`message_end` carry a `message` summary
  `{role, text, parts, content}` (events.ts `summarizeMessage`).
- `tool_execution_start`/`_end` carry `{toolCallId, toolName, args/result, ...}`.
- the error frame is the protocol-level `{type:"error", message, fatal}` shape
  the sidecar emits (`runner.ts::error`).

The two load-bearing invariants under test:
- assistant text → a single `MessageEvent` (role/content correct);
- bridged tool start/end → `[]` (proves the mapper does NOT double-append the
  Action/Observation the D2 bridge already owns).
"""

from __future__ import annotations

from disco.agent_server.build_kernel.pi_event_mapper import map_pi_event
from disco.core import AgentErrorEvent, EventSource, MessageEvent

CID = "conv_test"


def _msg_frame(kind: str, *, role: str, text: str) -> dict[str, object]:
    """An events.ts message_update/message_end envelope."""
    return {
        "kind": kind,
        "message": {
            "role": role,
            "text": text,
            "parts": ["text"],
            "content": text,
        },
    }


def test_message_update_maps_to_assistant_message_event() -> None:
    events = map_pi_event(
        _msg_frame("message_update", role="assistant", text="Hello there"),
        conversation_id=CID,
    )
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, MessageEvent)
    assert event.source == EventSource.AGENT
    assert event.message.role == "assistant"
    assert event.message.content == "Hello there"
    # Streaming snapshot is flagged so the wiring can de-dupe/stream.
    assert event.meta["pi_streaming"] is True
    assert event.meta["pi_kind"] == "message_update"


def test_message_end_maps_to_final_assistant_message_event() -> None:
    events = map_pi_event(
        _msg_frame("message_end", role="assistant", text="All done."),
        conversation_id=CID,
    )
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, MessageEvent)
    assert event.message.content == "All done."
    # A final snapshot is NOT flagged streaming.
    assert event.meta["pi_streaming"] is False


def test_non_assistant_message_is_dropped() -> None:
    # A user/tool message_end is not the agent's text — the bridge/user own those.
    assert (
        map_pi_event(
            _msg_frame("message_end", role="user", text="my prompt"),
            conversation_id=CID,
        )
        == []
    )


def test_empty_assistant_text_is_dropped() -> None:
    # An empty streaming snapshot (no text part yet) yields no event.
    assert (
        map_pi_event(
            _msg_frame("message_update", role="assistant", text=""),
            conversation_id=CID,
        )
        == []
    )


def test_error_frame_maps_to_agent_error_event() -> None:
    # The protocol-level error frame the sidecar emits (runner.ts::error).
    events = map_pi_event(
        {"type": "error", "message": "extension failed: boom", "fatal": True},
        conversation_id=CID,
    )
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, AgentErrorEvent)
    assert event.error == "extension failed: boom"
    assert event.source == EventSource.ENVIRONMENT


def test_extension_error_envelope_maps_to_agent_error_event() -> None:
    # Forward-compat: an envelope-borne extension failure.
    events = map_pi_event(
        {"kind": "extension_error", "error": "my-ext threw"},
        conversation_id=CID,
    )
    assert len(events) == 1
    assert isinstance(events[0], AgentErrorEvent)
    assert events[0].error == "my-ext threw"


def test_error_frame_without_message_is_never_blank() -> None:
    events = map_pi_event({"type": "error"}, conversation_id=CID)
    assert len(events) == 1
    assert isinstance(events[0], AgentErrorEvent)
    assert events[0].error  # non-empty fallback


def test_tool_execution_start_returns_empty_no_double_append() -> None:
    # The D2 bridge owns the ActionEvent for bridged custom tools.
    frame = {
        "kind": "tool_execution_start",
        "toolCallId": "tc_1",
        "toolName": "file_write",
        "args": {"path": "index.html", "content": "<html></html>"},
    }
    assert map_pi_event(frame, conversation_id=CID) == []


def test_tool_execution_end_returns_empty_no_double_append() -> None:
    # The D2 bridge owns the ObservationEvent for bridged custom tools.
    frame = {
        "kind": "tool_execution_end",
        "toolCallId": "tc_1",
        "toolName": "file_write",
        "result": {"ok": True},
        "isError": False,
    }
    assert map_pi_event(frame, conversation_id=CID) == []


def test_agent_end_returns_empty_finish_handled_by_wiring() -> None:
    # agent_end is not a single appendable finish event — wiring drives finish.
    frame = {"kind": "agent_end", "willRetry": False, "messageCount": 2, "messages": []}
    assert map_pi_event(frame, conversation_id=CID) == []


def test_unknown_kind_returns_empty() -> None:
    assert map_pi_event({"kind": "queue_update"}, conversation_id=CID) == []
    assert map_pi_event({"kind": "some_future_pi_event"}, conversation_id=CID) == []


def test_malformed_frame_never_raises() -> None:
    # Garbage in → [] out, never an exception (protects the drain loop).
    assert map_pi_event({}, conversation_id=CID) == []
    assert map_pi_event({"kind": 123}, conversation_id=CID) == []  # type: ignore[dict-item]
    assert map_pi_event({"kind": "message_update", "message": "oops"}, conversation_id=CID) == []
