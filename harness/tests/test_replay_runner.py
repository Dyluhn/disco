"""Event-log replay runner (plan Phase 3). The pure machinery — normalize / split /
diff — is tested on REAL event objects (constructed from the real classes), proving
it strips exactly the volatile fields and catches a real output divergence. The
full deterministic engine re-run is gated on a captured (event-log + cassette) pair
(`_capture_loop_demo.py`); it skips cleanly when that fixture is absent, the same
env-blocked heavy capture as the Phase 2 --replay e2e.

Run: PYTHONPATH=. uv run pytest harness/tests/test_replay_runner.py
"""

from __future__ import annotations

from pathlib import Path

import pytest
from perpleximanus.core import LLMMessage
from perpleximanus.core.events import (
    ActionEvent,
    EventSource,
    MessageEvent,
    ObservationEvent,
    ToolCall,
    ToolResult,
)

from harness.replay_runner import (
    diff_sequences,
    is_input,
    normalize_event,
    split_io,
)


def _user(text: str) -> MessageEvent:
    return MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content=text))


def _action(thought: str, tool: str, args: dict) -> ActionEvent:
    return ActionEvent(
        thought=thought,
        tool_call=ToolCall(tool_name=tool, arguments=args, call_id="call_VOLATILE"),
        llm_response_id="resp_VOLATILE",
    )


def _obs(content: str, action_id: str) -> ObservationEvent:
    return ObservationEvent(
        tool_result=ToolResult(
            call_id="call_VOLATILE", tool_name="shell", success=True, content=content
        ),
        action_id=action_id,
    )


# ---- normalization: strips volatile, keeps semantics ------------------------


def test_normalize_strips_volatile_fields():
    n = normalize_event(_action("do the thing", "shell", {"cmd": "ls"}))
    # volatile gone
    assert "id" not in n and "timestamp" not in n and "seq" not in n
    assert "llm_response_id" not in n
    assert "call_id" not in n["tool_call"]
    # semantics kept
    assert n["thought"] == "do the thing"
    assert n["tool_call"]["tool_name"] == "shell"
    assert n["tool_call"]["arguments"] == {"cmd": "ls"}


def test_two_runs_of_the_same_action_normalize_equal():
    # different ids/timestamps (new instances) → SAME normalized view.
    a = _action("t", "shell", {"cmd": "x"})
    b = _action("t", "shell", {"cmd": "x"})
    assert a.id != b.id  # genuinely different volatile ids
    assert normalize_event(a) == normalize_event(b)


def test_observation_action_id_is_volatile():
    a = _obs("output", action_id="evt_aaa")
    b = _obs("output", action_id="evt_bbb")
    assert normalize_event(a) == normalize_event(b)  # correlation id ignored


# ---- input/output split -----------------------------------------------------


def test_split_io_classifies_user_messages_as_input():
    events = [_user("build me a site"), _action("t", "shell", {"cmd": "ls"}), _obs("ok", "x")]
    assert is_input(events[0]) and not is_input(events[1])
    inputs, outputs = split_io(events)
    assert inputs == [events[0]]
    assert outputs == [events[1], events[2]]


def test_agent_message_is_output_not_input():
    # only USER messages are inputs; an assistant message is engine output.
    asst = MessageEvent(
        source=EventSource.AGENT, message=LLMMessage(role="assistant", content="hi")
    )
    assert not is_input(asst)


# ---- the diff: identical passes, a real regression turns it red -------------


def test_diff_identical_sequences_is_empty():
    rec = [_action("t", "shell", {"cmd": "ls"}), _obs("ok", "evt_1")]
    rep = [_action("t", "shell", {"cmd": "ls"}), _obs("ok", "evt_2")]  # diff ids only
    assert diff_sequences(rec, rep) == []


def test_diff_catches_a_changed_tool_call():
    # THE regression class: a code change makes the engine emit a different action.
    rec = [_action("t", "shell", {"cmd": "ls"})]
    rep = [_action("t", "shell", {"cmd": "rm -rf /"})]  # args diverged
    diffs = diff_sequences(rec, rep)
    assert diffs and "payload diverged" in diffs[0]


def test_diff_catches_a_dropped_event():
    rec = [_action("t", "shell", {"cmd": "ls"}), _obs("ok", "evt_1")]
    rep = [_action("t", "shell", {"cmd": "ls"})]  # the observation went missing
    diffs = diff_sequences(rec, rep)
    assert any("length" in d for d in diffs)


def test_diff_catches_a_kind_change():
    rec = [_action("t", "shell", {"cmd": "ls"})]
    rep = [_obs("ok", "evt_1")]
    diffs = diff_sequences(rec, rep)
    assert diffs and "kind" in diffs[0]


# ---- full deterministic replay (gated on the captured fixture) --------------

_FIXTURE = Path(__file__).resolve().parents[1] / "cassettes" / "loop_demo.events.jsonl"


@pytest.mark.skipif(
    not _FIXTURE.exists(), reason="needs the captured loop fixture (make capture-loop)"
)
async def test_deterministic_replay_reproduces_recorded_events():
    from perpleximanus.core import SqliteEventStore

    from harness.cassette import Cassette
    from harness.replay_runner import load_events, replay_conversation
    from harness.runtime import build_replay_runtime

    cassette = Cassette.load(str(_FIXTURE.with_name("loop_demo.cassette.jsonl")))
    recorded = load_events(_FIXTURE)

    def _builder(store):
        return build_replay_runtime(cassette, store)

    outputs = await replay_conversation(
        recorded,
        build_runtime=_builder,
        store=SqliteEventStore(":memory:"),
        # surface MUST match the capture, or the path (+ cassette key) differs
        surface="deep_research",
    )
    _, recorded_outputs = split_io(recorded)
    assert diff_sequences(recorded_outputs, outputs) == []
