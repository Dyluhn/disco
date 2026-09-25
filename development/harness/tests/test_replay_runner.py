"""Research event replay with a real gateless capture. Semantic actions, tool
results, checkpoint work and the complete report must match. Transport token
pulses and generated storage identities are tested separately from replay.

Run: uv run pytest development/harness/tests/test_replay_runner.py
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from disco.core import LLMMessage
from disco.core.events import (
    ActionEvent,
    EventAdapter,
    EventSource,
    MessageEvent,
    ObservationEvent,
    ToolCall,
    ToolResult,
)
from harness.replay_runner import (
    diff_sequences,
    is_input,
    is_legacy_plan_fixture,
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


def test_legacy_plan_fixture_is_detected() -> None:
    from disco.core.events import PlanEvent

    assert is_legacy_plan_fixture([PlanEvent(summary="old", steps=[], revision=1)])
    assert not is_legacy_plan_fixture([])


# ---- full deterministic replay (gated on the captured fixture) --------------

_FIXTURE = (
    Path(__file__).resolve().parents[1] / "cassettes" / "loop_paris_standard_low_v3.events.jsonl"
)
_LEGACY_FIXTURE = Path(__file__).resolve().parents[1] / "cassettes" / "loop_demo.events.jsonl"


def _with_reading_checkpoints(recorded_outputs):
    """Apply the captured host-event migration without weakening replay equality."""
    migration = json.loads(
        _LEGACY_FIXTURE.with_name("loop_demo.reading-checkpoints.json").read_text()
    )
    before = {row["before_event_id"]: row["events"] for row in migration["insertions"]}
    expected = []
    for event in recorded_outputs:
        for row in before.pop(event.id, []):
            added = EventAdapter.validate_python(row)
            assert added.kind == "action"
            assert added.thought == "Deep Research: research_checkpoint_commit"
            assert added.tool_call.arguments["stage"] == "writing"
            expected.append(added)
        expected.append(event)
    assert not before, "migration references an event missing from the original capture"
    assert len(expected) - len(recorded_outputs) == 36
    return expected


_CASE = "loop_paris_standard_low_v3"
_DEPTH = "standard_deep"
_PROVENANCE = (
    Path(__file__).resolve().parents[1]
    / "cassettes"
    / ("loop_paris_standard_low_v3.provenance.json")
)


def _sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


# Preserve the accepted skip marker identity; current capture provenance and
# commands are documented in CONTRIBUTING and the versioned fixture record.
@pytest.mark.skipif(
    not _FIXTURE.exists(), reason="needs the captured loop fixture (make capture-loop)"
)
async def test_deterministic_replay_reproduces_recorded_events():
    from disco.core import SqliteEventStore
    from disco.core.events import EventKind
    from harness.cassette import Cassette
    from harness.replay_runner import load_events, replay_conversation
    from harness.runtime import build_replay_runtime

    provenance = json.loads(_PROVENANCE.read_text(encoding="utf-8"))
    assert provenance["case"] == _CASE and provenance["cid"] == _CASE
    assert provenance["depth"] == _DEPTH
    assert provenance["query"] == "What is the capital of France and roughly its population?"
    cassette = Cassette.load(str(_FIXTURE.with_name("loop_paris_standard_low_v3.cassette.jsonl")))
    recorded = load_events(_FIXTURE)
    assert len(recorded) == 293, "versioned current fixture must stay byte-exact (293 events)"
    assert len(cassette) == 129, "versioned current fixture must stay byte-exact (129 interactions)"
    for name, expected in provenance["artifact_sha256"].items():
        actual = _sha256(_FIXTURE.with_name(name))
        assert actual == expected, f"{name} diverged from provenance seal"
    assert not any(event.kind == EventKind.PLAN for event in recorded), (
        "checked-in fixture must exercise the current gateless protocol"
    )

    def _builder(store):
        runtime = build_replay_runtime(cassette, store)
        runtime.deep_research.set_depth(_CASE, _DEPTH)
        return runtime

    outputs = await replay_conversation(
        recorded,
        build_runtime=_builder,
        store=SqliteEventStore(":memory:"),
        cid=_CASE,
        # surface + depth + cid MUST match the capture, or the path (+ cassette key) differs
        surface="deep_research",
    )
    _, recorded_outputs = split_io(recorded)
    # Strict: the current capture already contains its writing-stage checkpoint
    # commits, so NO migration is applied here — any divergence is a regression.
    assert diff_sequences(recorded_outputs, outputs) == []
    cassette.assert_all_consumed()


def test_historical_loop_paris_standard_low_v2_integrity():
    """History-only seals: the allocation change needs a new genuine capture.

    These bytes are not relabeled as responses to the new output budget, and
    this integrity check does not claim v2 replays on the current runtime.
    """
    from harness.cassette import Cassette
    from harness.replay_runner import load_events

    root = _FIXTURE.parent
    seals = {
        "events.jsonl": "d59672670031cff5d43cd896e5dc184b5e48b72ce7a44b281111ac0c70b7858d",
        "cassette.jsonl": "9b49d00a2b2fec9208547fa8be23708f6fee5b274658b442a85b70f618a7fc47",
        "provenance.json": "04fe50ffd526a6c0adb7b1727c4760caf093864c53589b08fa230d7b2c53280c",
    }
    for suffix, expected in seals.items():
        assert _sha256(root / f"loop_paris_standard_low_v2.{suffix}") == expected
    provenance = json.loads((root / "loop_paris_standard_low_v2.provenance.json").read_text())
    assert provenance["case"] == provenance["cid"] == "loop_paris_standard_low_v2"
    assert len(load_events(root / "loop_paris_standard_low_v2.events.jsonl")) == 304
    assert len(Cassette.load(root / "loop_paris_standard_low_v2.cassette.jsonl")) == 149
    for name, expected in provenance["artifact_sha256"].items():
        assert _sha256(root / name) == expected


def test_historical_loop_demo_migration_still_applies():
    """The preserved loop_demo pair + its reading-checkpoint migration stay coherent.

    History-only: no runtime, no replay. Guards the legacy evidence the current
    fixture superseded (see loop_paris_standard_low_v2.provenance.json)."""
    from harness.replay_runner import load_events, split_io

    recorded = load_events(_LEGACY_FIXTURE)
    _, recorded_outputs = split_io(recorded)
    migration = json.loads(
        _LEGACY_FIXTURE.with_name("loop_demo.reading-checkpoints.json").read_text()
    )
    assert len(migration["insertions"]) == 9, "migration must keep its 9 insertion rows"
    assert sum(len(row["events"]) for row in migration["insertions"]) == 36, (
        "migration must insert exactly 36 writing-stage checkpoint commits"
    )
    migrated = _with_reading_checkpoints(recorded_outputs)
    assert len(migrated) - len(recorded_outputs) == 36


def test_research_replay_ignores_transport_pulses_but_keeps_checkpoint_work():
    def trace(name, args):
        return _action(f"Deep Research: {name}", name, args)

    before = trace(
        "research_checkpoint_commit",
        {
            "conversation_id": "old",
            "run_id": "a",
            "checkpoint_id": "digest-a",
            "stage": "review",
            "turns_spent": 4,
            "sources_spent": 12,
        },
    )
    after = trace(
        "research_checkpoint_commit",
        {
            "conversation_id": "new",
            "run_id": "b",
            "checkpoint_id": "digest-b",
            "stage": "review",
            "turns_spent": 4,
            "sources_spent": 12,
        },
    )
    activity = trace("model_activity", {"seconds": 1.5, "tokens_streamed": 50})
    assert diff_sequences([before, activity], [after]) == []
    for changed in ({"stage": "research"}, {"turns_spent": 3}, {"sources_spent": 11}):
        changed_event = trace(
            "research_checkpoint_commit", {**after.tool_call.arguments, **changed}
        )
        assert diff_sequences([before], [changed_event])
    assert diff_sequences([before], [])
    # A proposed model tool call with the same name is still a semantic event.
    assert diff_sequences([_action("run this tool", "model_activity", {})], [])


def test_research_pool_storage_identity_does_not_hide_changed_sources():
    before = _action(
        "Deep Research: research_pool",
        "research_pool",
        {"pool_id": "old", "bytes": 100, "sources": 12},
    )
    after = _action(
        "Deep Research: research_pool",
        "research_pool",
        {"pool_id": "new", "bytes": 104, "sources": 12},
    )
    assert diff_sequences([before], [after]) == []
    changed = _action(
        "Deep Research: research_pool",
        "research_pool",
        {"pool_id": "new", "bytes": 104, "sources": 11},
    )
    assert diff_sequences([before], [changed])
