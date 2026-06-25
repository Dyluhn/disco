"""Tests for the PR I1 Pi event mapper (`pi_event_mapper.map_pi_event`).

Input frames are the REAL OUTER protocol frames `PiProcess.events()` yields:
- an `agent_event` is wrapped `{"type":"agent_event","event":{kind,…}}`
  (protocol.ts `AgentEventMessage`); the inner envelope carries a `message`
  summary `{role, text, parts, content}` (events.ts `summarizeMessage`) for
  message_update/message_end, and `{toolCallId, toolName, …}` for tool frames.
- the error frame is the protocol-level `{type:"error", message, fatal}` shape
  the sidecar emits (`runner.ts::error`) — it is NOT wrapped.

The load-bearing invariants under test:
- the mapper unwraps the OUTER agent_event frame (Batch-3 feeds these);
- only the FINAL assistant message (`message_end`) → a single `MessageEvent`;
  a streaming `message_update` partial → `[]` (no duplicate stored messages);
- bridged tool start/end and agent_end → `[]` (no double-append / wiring owns it).
"""

from __future__ import annotations

from disco.agent_server.build_kernel.pi_event_mapper import map_pi_event
from disco.core import AgentErrorEvent, EventSource, MessageEvent

CID = "conv_test"


def _agent_event(envelope: dict[str, object]) -> dict[str, object]:
    """Wrap an inner `{kind,…}` envelope in the REAL outer protocol frame that
    `PiProcess.events()` yields (protocol.ts `AgentEventMessage`)."""
    return {"type": "agent_event", "event": envelope}


def _msg_envelope(kind: str, *, role: str, text: str) -> dict[str, object]:
    """An events.ts message_update/message_end inner envelope."""
    return {
        "kind": kind,
        "message": {
            "role": role,
            "text": text,
            "parts": ["text"],
            "content": text,
        },
    }


def _msg_frame(kind: str, *, role: str, text: str) -> dict[str, object]:
    """The full OUTER agent_event frame for a message envelope."""
    return _agent_event(_msg_envelope(kind, role=role, text=text))


# ---- assistant messages: only message_end persists ---------------------------


def test_message_update_is_dropped_not_persisted() -> None:
    # Streaming partials must NOT each become a stored MessageEvent — the
    # Batch-3 wiring surfaces them live; only message_end is persisted truth.
    assert (
        map_pi_event(
            _msg_frame("message_update", role="assistant", text="Hello the"),
            conversation_id=CID,
        )
        == []
    )


def test_message_end_maps_to_single_final_assistant_message_event() -> None:
    events = map_pi_event(
        _msg_frame("message_end", role="assistant", text="All done."),
        conversation_id=CID,
    )
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, MessageEvent)
    assert event.source == EventSource.AGENT
    assert event.message.role == "assistant"
    assert event.message.content == "All done."
    # The final snapshot is the persisted truth, not a streaming partial.
    assert event.meta["pi_streaming"] is False
    assert event.meta["pi_kind"] == "message_end"


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
    # A final snapshot with no text (e.g. a tool-only turn) yields no event.
    assert (
        map_pi_event(
            _msg_frame("message_end", role="assistant", text=""),
            conversation_id=CID,
        )
        == []
    )


# ---- back-compat: a bare inner envelope is still accepted ---------------------


def test_bare_inner_message_end_envelope_still_maps() -> None:
    # Back-compat: a bare {kind,…} envelope (no agent_event wrapper) still maps.
    events = map_pi_event(
        _msg_envelope("message_end", role="assistant", text="bare ok"),
        conversation_id=CID,
    )
    assert len(events) == 1
    assert isinstance(events[0], MessageEvent)
    assert events[0].message.content == "bare ok"


# ---- error frames ------------------------------------------------------------


def test_error_frame_maps_to_agent_error_event() -> None:
    # The protocol-level error frame the sidecar emits (runner.ts::error) is NOT
    # wrapped in agent_event — it is mapped directly.
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
    # Forward-compat: an envelope-borne extension failure (wrapped agent_event).
    events = map_pi_event(
        _agent_event({"kind": "extension_error", "error": "my-ext threw"}),
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


# ---- bridged tools + agent_end: no appended event ----------------------------


def test_tool_execution_start_returns_empty_no_double_append() -> None:
    # The D2 bridge owns the ActionEvent for bridged custom tools.
    frame = _agent_event(
        {
            "kind": "tool_execution_start",
            "toolCallId": "tc_1",
            "toolName": "file_write",
            "args": {"path": "index.html", "content": "<html></html>"},
        }
    )
    assert map_pi_event(frame, conversation_id=CID) == []


def test_tool_execution_end_returns_empty_no_double_append() -> None:
    # The D2 bridge owns the ObservationEvent for bridged custom tools.
    frame = _agent_event(
        {
            "kind": "tool_execution_end",
            "toolCallId": "tc_1",
            "toolName": "file_write",
            "result": {"ok": True},
            "isError": False,
        }
    )
    assert map_pi_event(frame, conversation_id=CID) == []


def test_agent_end_returns_empty_finish_handled_by_wiring() -> None:
    # agent_end is not a single appendable finish event — wiring drives finish.
    frame = _agent_event(
        {"kind": "agent_end", "willRetry": False, "messageCount": 2, "messages": []}
    )
    assert map_pi_event(frame, conversation_id=CID) == []


# ---- unknown / malformed -----------------------------------------------------


def test_unknown_kind_returns_empty() -> None:
    assert map_pi_event(_agent_event({"kind": "queue_update"}), conversation_id=CID) == []
    assert (
        map_pi_event(_agent_event({"kind": "some_future_pi_event"}), conversation_id=CID)
        == []
    )


def test_unknown_protocol_type_returns_empty() -> None:
    # A protocol frame that is neither agent_event nor error → [].
    assert map_pi_event({"type": "heartbeat", "ts": 123}, conversation_id=CID) == []
    assert map_pi_event({"type": "exit", "code": 0}, conversation_id=CID) == []


def test_agent_event_with_non_mapping_event_returns_empty() -> None:
    # A malformed outer frame whose `event` is not a mapping → [], never raises.
    assert map_pi_event({"type": "agent_event", "event": "oops"}, conversation_id=CID) == []
    assert map_pi_event({"type": "agent_event"}, conversation_id=CID) == []


def test_malformed_frame_never_raises() -> None:
    # Garbage in → [] out, never an exception (protects the drain loop).
    assert map_pi_event({}, conversation_id=CID) == []
    assert map_pi_event({"kind": 123}, conversation_id=CID) == []  # type: ignore[dict-item]
    assert (
        map_pi_event(
            _agent_event({"kind": "message_end", "message": "oops"}),
            conversation_id=CID,
        )
        == []
    )
