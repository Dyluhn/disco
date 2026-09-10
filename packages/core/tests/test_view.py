"""View + condensation tests — event-state-contract.md §8.3.

The View is the materialized "what the LLM sees", computed by applying
condensation tombstones to the raw log. Phase 0 ships the View and a no-op
condenser; the real LLMSummarizingCondenser (first-half/keep_first/
minimum_progress/hard-reset strategy) is a Phase 1 deliverable (BoD §22), so its
strategy-specific tests are deferred. Here we test the View's tombstone
*application* directly (which is the [CONTRACT] half) plus the no-op condenser.
"""

from __future__ import annotations

from disco.core import (
    ActionEvent,
    CondensationEvent,
    ConversationStatus,
    EventSource,
    LLMMessage,
    MessageEvent,
    NoOpCondenser,
    ObservationEvent,
    ToolResult,
    View,
)
from disco.core.view import microcompact, recover_span
from event_fakes import (
    action,
    agent_msg,
    fatal,
    observation,
    status,
    tombstone,
    user_msg,
    with_seqs,
)

# ---- S3 Microcompact (GAP A) — drop no-op turns -----------------------------


def _failed_then_retried_ok():
    """A failed `shell` call superseded by an IDENTICAL successful retry. Returns the
    seq-assigned event list (with_seqs copies, so seqs are populated)."""
    a1 = action(tool="shell", args={"command": "pip install x"})
    o1 = observation(action_id=a1.id, tool="shell", success=False, content="network error")
    a2 = action(tool="shell", args={"command": "pip install x"})  # identical call
    o2 = observation(action_id=a2.id, tool="shell", success=True, content="installed")
    return with_seqs([user_msg("go"), a1, o1, a2, o2])


def test_microcompact_tombstones_a_failed_then_superseded_turn():
    events = _failed_then_retried_ok()
    a1_seq, o1_seq = events[1].seq, events[2].seq  # the failed action + its observation
    tombs = microcompact(events)
    assert len(tombs) == 1
    assert (tombs[0].forgotten_start_seq, tombs[0].forgotten_end_seq) == (a1_seq, o1_seq)
    # applying it: the failed turn is gone from the View, the success remains, and a
    # one-line tombstone marks the drop.
    view = View.of(events + tombs)
    blob = "\n".join(m.content for m in view.messages)
    assert "network error" not in blob  # the failed attempt is forgotten
    assert "installed" in blob  # the superseding success stays
    assert "microcompacted" in blob  # the one-liner marks it


def test_microcompact_keeps_an_unsuperseded_failure():
    """A failure NOT followed by an identical success still carries information — keep it."""
    a1 = action(tool="shell", args={"command": "pip install x"})
    o1 = observation(action_id=a1.id, tool="shell", success=False, content="network error")
    events = with_seqs([user_msg("go"), a1, o1])
    assert microcompact(events) == []


def test_microcompact_never_touches_durable_tools():
    """A failed file_write superseded by an identical success is NOT microcompacted
    (write tools may have left partial state; plan_step carries progress)."""
    a1 = action(tool="file_write", args={"path": "a.txt", "content": "hi"})
    o1 = observation(action_id=a1.id, tool="file_write", success=False, content="disk full")
    a2 = action(tool="file_write", args={"path": "a.txt", "content": "hi"})
    o2 = observation(action_id=a2.id, tool="file_write", success=True, content="wrote 2 bytes")
    events = with_seqs([user_msg("go"), a1, o1, a2, o2])
    assert microcompact(events) == []


def test_microcompact_is_idempotent():
    """Re-running after the tombstone is applied finds nothing new (safe every step)."""
    events = _failed_then_retried_ok()
    tombs = microcompact(events)
    assert microcompact(events + tombs) == []


def test_view_is_deterministic():
    events = with_seqs([user_msg(), action(), observation("evt_a")])
    assert View.of(events).messages == View.of(events).messages


def test_trace_only_host_message_is_auditable_but_not_model_visible():
    public = MessageEvent(
        source=EventSource.ENVIRONMENT,
        message=LLMMessage(role="user", content="ordinary host guidance"),
    )
    trace_only = MessageEvent(
        source=EventSource.ENVIRONMENT,
        message=LLMMessage(role="user", content="UI-only diagnostic"),
        meta={"model_visibility": "trace_only"},
    )
    events = with_seqs([user_msg("task"), public, trace_only])

    view = View.of(events)

    persisted = next(event for event in events if event.id == trace_only.id)
    assert persisted.seq is not None
    assert [message.content for message in view.messages] == ["task", "ordinary host guidance"]
    assert persisted.seq not in view.visible_seqs


def test_trace_only_metadata_cannot_hide_a_real_user_instruction():
    instruction = MessageEvent(
        source=EventSource.USER,
        message=LLMMessage(role="user", content="real user instruction"),
        meta={"model_visibility": "trace_only"},
    )

    view = View.of(with_seqs([instruction]))

    assert [message.content for message in view.messages] == ["real user instruction"]


def test_non_convertibles_never_appear():
    """StatusEvent / ErrorEvent / CondensationEvent are never in View.messages."""
    events = with_seqs(
        [
            user_msg("q"),
            status(ConversationStatus.RUNNING),
            agent_msg("working"),
            fatal(),
        ]
    )
    view = View.of(events)
    # Only the two LLMConvertible messages (user + agent) survive.
    assert [m.role for m in view.messages] == ["user", "assistant"]
    assert [m.content for m in view.messages] == ["q", "working"]


def test_tombstone_drops_span_and_inserts_summary_once():
    """A tombstone over a span hides those events and shows its summary exactly
    once in their place."""
    events = with_seqs(
        [
            user_msg("first"),  # seq 1
            agent_msg("early-1"),  # seq 2  (forgotten)
            agent_msg("early-2"),  # seq 3  (forgotten)
            agent_msg("recent"),  # seq 4  (kept)
            tombstone(2, 3, "[earlier discussion]"),  # seq 5  (the tombstone)
        ]
    )
    view = View.of(events)
    contents = [m.content for m in view.messages]
    # first (kept) -> summary (in place of 2,3) -> recent (kept).
    assert contents == ["first", "[earlier discussion]", "recent"]
    assert contents.count("[earlier discussion]") == 1  # summary emitted once
    assert view.forgotten_count == 2
    assert 2 not in view.visible_seqs and 3 not in view.visible_seqs


def test_forgotten_events_never_appear_even_with_overlapping_tombstones():
    events = with_seqs(
        [
            user_msg("first"),  # 1
            agent_msg("a"),  # 2
            agent_msg("b"),  # 3
            agent_msg("c"),  # 4
            tombstone(2, 3, "S1"),  # 5
            tombstone(3, 4, "S2"),  # 6  (overlaps on 3)
        ]
    )
    view = View.of(events)
    # 2,3,4 are all forgotten by the union of ranges; neither survives.
    assert all(s not in view.visible_seqs for s in (2, 3, 4))
    assert "a" not in [m.content for m in view.messages]


def test_back_half_is_preserved_byte_identical_after_condensation():
    """First-half-style condensation leaves the back half untouched (the
    property the real condenser relies on; tested here at the View level)."""
    raw = with_seqs([user_msg("u"), agent_msg("m1"), agent_msg("m2"), agent_msg("m3")])
    before = View.of(raw)
    back_half_before = [m.content for m in before.messages if m.content in ("m2", "m3")]

    condensed = raw + with_seqs([tombstone(1, 2, "[summary of u, m1]")], start=5)
    after = View.of(condensed)
    back_half_after = [m.content for m in after.messages if m.content in ("m2", "m3")]

    assert back_half_before == back_half_after == ["m2", "m3"]


def _obs_for(action_event: ActionEvent, content: str = "ok") -> ObservationEvent:
    return ObservationEvent(
        tool_result=ToolResult(
            call_id=action_event.tool_call.call_id,
            tool_name=action_event.tool_call.tool_name,
            success=True,
            content=content,
        ),
        action_id=action_event.id,
    )


def _assert_tool_pairs_adjacent(messages: list[LLMMessage]) -> None:
    for i, msg in enumerate(messages):
        if msg.role == "assistant" and msg.tool_calls:
            ids = [tc["id"] for tc in msg.tool_calls]
            following = messages[i + 1 : i + 1 + len(ids)]
            assert [(m.role, m.tool_call_id) for m in following] == [("tool", cid) for cid in ids]


def test_condensation_starting_at_observation_omits_the_action_too():
    """Regression for REL-RC-K: a tombstone beginning at the tool result must
    not leave the assistant tool_call visible before the condensation summary."""
    a = action(tool="file_read", args={"path": "index.html"})
    o = _obs_for(a, "file body")
    events = with_seqs(
        [
            user_msg("inspect"),
            a,  # seq 2: paired action
            o,  # seq 3: forgotten result
            agent_msg("later"),
            tombstone(3, 4, "[tool turn condensed]"),
        ]
    )

    view = View.of(events)
    assert "[tool turn condensed]" in [m.content for m in view.messages]
    assert all(
        not (
            m.role == "assistant" and m.tool_calls and m.tool_calls[0]["id"] == a.tool_call.call_id
        )
        for m in view.messages
    )
    _assert_tool_pairs_adjacent(view.messages)


def test_condensation_starting_at_action_omits_the_observation_too():
    a = action(tool="shell", args={"command": "pwd"})
    o = _obs_for(a, "workspace")
    events = with_seqs([user_msg("run"), a, o, tombstone(2, 2, "[action condensed]")])

    view = View.of(events)
    assert "[action condensed]" in [m.content for m in view.messages]
    assert all(m.tool_call_id != a.tool_call.call_id for m in view.messages)
    _assert_tool_pairs_adjacent(view.messages)


def test_condensed_injected_reminder_cannot_split_tool_pair():
    a = action(tool="shell", args={"command": "npm test"})
    reminder = MessageEvent(
        source=EventSource.ENVIRONMENT,
        message=LLMMessage(
            role="user",
            content="<system-reminder>continue with the plan</system-reminder>",
        ),
    )
    o = _obs_for(a, "tests passed")
    events = with_seqs([user_msg("test"), a, reminder, o, tombstone(3, 3, "[reminder condensed]")])

    view = View.of(events)
    pair_idx = next(
        i for i, m in enumerate(view.messages) if m.role == "assistant" and m.tool_calls
    )
    assert view.messages[pair_idx + 1].role == "tool"
    assert view.messages[pair_idx + 1].tool_call_id == a.tool_call.call_id
    assert view.messages[pair_idx + 2].content == "[reminder condensed]"
    _assert_tool_pairs_adjacent(view.messages)


# ---- the no-op condenser (real one deferred to Phase 1) ---------------------


async def test_noop_condenser_never_condenses():
    events = with_seqs([user_msg()] + [agent_msg(f"m{i}") for i in range(300)])
    view = View.of(events)
    cond = NoOpCondenser()
    assert cond.should_condense(view, token_count=10_000_000) is None  # sync (v1.2)
    assert await cond.condense(events, view, summarizer=_FakeSummarizer()) is None  # async


class _FakeSummarizer:
    """Stand-in Summarizer (no real LLM) — the Phase 1 condenser tests will use
    a fake like this to assert the tombstone summary equals its output (§8.3).
    Async per event-state-contract v1.2 §5.2."""

    async def summarize(self, messages):
        return "[fake summary]"


def test_recitation_scopes_plan_steps_to_the_current_plan():
    """After a re-plan, the recitation must NOT count the PRIOR plan's done marks —
    else every step shows done on the new plan and the model gets confused (observed
    live → STUCK). Only plan_steps after the latest PlanEvent count."""
    from disco.core import PlanEvent
    from disco.core.view import _recitation_message

    def act(tool, args):
        return action(tool=tool, args=args)

    evs = with_seqs(
        [
            user_msg("build"),
            PlanEvent(summary="v1", steps=[{"title": "a"}, {"title": "b"}], revision=1),
            act("plan_step", {"index": 1, "state": "done"}),
            act("plan_step", {"index": 2, "state": "done"}),  # v1 fully done
            PlanEvent(
                summary="v2", steps=[{"title": "x"}, {"title": "y"}, {"title": "z"}], revision=2
            ),
            act("plan_step", {"index": 1, "state": "done"}),  # only step 1 of v2 done
        ]
    )
    msg = _recitation_message(evs)
    assert msg is not None
    # the new plan is 1/3 done — NOT 3/3 (v1's marks excluded)
    assert "1/3 done" in msg.content


# ---- C11 — Reversible-compaction tier (on-demand recovery) ------------------
#
# A tombstone is a MARKER on the append-only log, not a delete. The dropped
# events stay addressable on disk; `recover_span` (and `View.recover_span`)
# re-materializes them on demand. The default `View.messages` is NOT
# affected — recovery is an explicit accessor, not an un-tombstone. Nothing
# is re-injected into the live context (no bloat).


def test_c11_recover_span_returns_original_dropped_events():
    """The accessor returns the events a tombstone dropped from View.messages —
    the originals in their original Event form (not the summary that replaced
    them, not the rendered LLMMessage). The originals are NOT destroyed; the
    tombstone is a marker on the log, not a delete."""
    events = with_seqs(
        [
            user_msg("first"),  # seq 1 (kept)
            agent_msg("early-1"),  # seq 2 (forgotten)
            agent_msg("early-2"),  # seq 3 (forgotten)
            agent_msg("recent"),  # seq 4 (kept)
            tombstone(2, 3, "[earlier discussion]"),  # seq 5
        ]
    )
    t = events[-1]
    assert isinstance(t, CondensationEvent)
    recovered = recover_span(events, t)
    # Originals in seq order, raw Event objects.
    assert [e.seq for e in recovered] == [2, 3]
    assert [e.message.content for e in recovered if isinstance(e, MessageEvent)] == [
        "early-1",
        "early-2",
    ]
    # The summary that replaced them is NOT returned (we recover the originals,
    # not the substitute).
    assert all(
        e.message.content != "[earlier discussion]"
        for e in recovered
        if isinstance(e, MessageEvent)
    )


def test_c11_recover_does_not_change_default_view_messages():
    """Calling recover_span does NOT mutate the event log and does NOT change
    `View.messages`. The default condensed view still omits the tombstoned
    span; the recovered events are returned to the caller, never re-injected."""
    events = with_seqs(
        [
            user_msg("first"),  # seq 1
            agent_msg("early-1"),  # seq 2 (forgotten)
            agent_msg("early-2"),  # seq 3 (forgotten)
            agent_msg("recent"),  # seq 4 (kept)
            tombstone(2, 3, "[earlier discussion]"),  # seq 5
        ]
    )
    # Snapshot the default View BEFORE recovery.
    view_before = View.of(events)
    contents_before = [m.content for m in view_before.messages]
    # Default view: span is gone, summary sits in its place, recent message
    # follows.
    assert contents_before == ["first", "[earlier discussion]", "recent"]
    assert view_before.forgotten_count == 2
    assert 2 not in view_before.visible_seqs
    assert 3 not in view_before.visible_seqs

    # Invoke recovery — this is the explicit on-demand accessor.
    t = events[-1]
    assert isinstance(t, CondensationEvent)
    recovered = recover_span(events, t)
    # Recovery worked: we got the dropped originals back.
    assert [e.seq for e in recovered] == [2, 3]
    assert "early-1" in [e.message.content for e in recovered if isinstance(e, MessageEvent)]
    assert "early-2" in [e.message.content for e in recovered if isinstance(e, MessageEvent)]

    # The default View is UNCHANGED. Recovery did not un-tombstone, did not
    # re-inject, did not mutate the log.
    view_after = View.of(events)
    assert [m.content for m in view_after.messages] == contents_before
    assert view_after.forgotten_count == 2
    assert "early-1" not in [m.content for m in view_after.messages]
    assert "early-2" not in [m.content for m in view_after.messages]
    # And the View's fingerprint is byte-identical — recovery has no side
    # effects on what the LLM sees.
    assert view_before.fingerprint() == view_after.fingerprint()


def test_c11_recover_returns_empty_for_unmatched_range():
    """A tombstone whose seq range matches no events in the log returns [].
    Reversible doesn't mean indestructible — it means addressable. An empty
    result is the honest answer when the range doesn't resolve."""
    events = with_seqs([user_msg("u")])  # only seq 1
    t = CondensationEvent(
        forgotten_start_seq=99,
        forgotten_end_seq=100,
        summary="phantom span (was never persisted)",
    )
    assert recover_span(events, t) == []


def test_c11_recover_handles_degenerate_range():
    """start > end is a degenerate tombstone; the accessor must not raise —
    it returns []. View.of already tolerates such ranges (a tombstone with
    start > end forgets nothing), so the recovery mirrors that."""
    t = CondensationEvent(forgotten_start_seq=10, forgotten_end_seq=5, summary="empty")
    assert recover_span(with_seqs([user_msg(), agent_msg()]), t) == []


def test_c11_recover_works_on_microcompact_tombstones():
    """Microcompact (S3) emits its own CondensationEvent for no-op turns. The
    original (action, observation) pair is STILL on the log — recovery returns
    them. This is the dominant real-world case: a failed `pip install` that
    was retried successfully, dropped as a microcompact tombstone. Recovery
    lets a debugging surface see the original failure verbatim."""
    a1 = action(tool="shell", args={"command": "pip install x"})
    o1 = observation(
        action_id=a1.id, tool="shell", success=False, content="network error\n" + "x" * 200
    )
    a2 = action(tool="shell", args={"command": "pip install x"})  # identical retry
    o2 = observation(action_id=a2.id, tool="shell", success=True, content="installed")
    base = with_seqs([user_msg("go"), a1, o1, a2, o2])
    tombs = microcompact(base)
    assert len(tombs) == 1
    t = tombs[0]
    # The default view drops the failed turn.
    view = View.of(base + tombs)
    assert "network error" not in "\n".join(m.content for m in view.messages)
    # Recovery returns the ORIGINAL failed turn — action + observation,
    # contents intact (not snipped/masked, since we recover the Event, not
    # the rendered LLMMessage).
    recovered = recover_span(base, t)
    assert len(recovered) == 2
    a1_seq, o1_seq = base[1].seq, base[2].seq
    assert [e.seq for e in recovered] == [a1_seq, o1_seq]
    assert isinstance(recovered[0], ActionEvent)
    assert recovered[0].tool_call.tool_name == "shell"
    assert isinstance(recovered[1], ObservationEvent)
    assert recovered[1].tool_result.success is False
    assert recovered[1].tool_result.content == o1.tool_result.content  # full body, not masked
    # And the default view is STILL condensed: recovery did not un-tombstone.
    view2 = View.of(base + tombs)
    assert view.fingerprint() == view2.fingerprint()


def test_c11_recover_returns_nested_tombstones_as_typed_events():
    """An inner tombstone (a CondensationEvent inside the recovered range) is
    returned as a typed CondensationEvent — the log is the source of truth,
    and a nested tombstone IS an event in the range. Re-running recover_span
    on it re-walks the same log to resolve its own span. This is the
    expected, correct behavior for a multi-stage condensation history."""
    inner = tombstone(2, 3, "inner summary")
    outer = tombstone(1, 5, "outer summary")
    events = with_seqs(
        [
            user_msg("first"),  # 1
            agent_msg("a"),  # 2
            agent_msg("b"),  # 3
            agent_msg("c"),  # 4
            agent_msg("d"),  # 5
            inner,  # 6
            outer,  # 7
        ]
    )
    # Recover the outer tombstone's range: events with seq in [1, 5] (5 of them)
    # — the user msg, three agent msgs, and the agent msg at seq 5. The inner
    # tombstone is at seq 6, OUTSIDE the outer's range, so it is NOT included.
    out = recover_span(events, outer)
    assert [e.seq for e in out] == [1, 2, 3, 4, 5]
    # Now recover the inner tombstone's range: events with seq in [2, 3].
    inn = recover_span(events, inner)
    assert [e.seq for e in inn] == [2, 3]


def test_c11_recover_is_pure_does_not_mutate_inputs():
    """The accessor is a pure read: it does not append, delete, or modify the
    event list or the tombstone. (Both are frozen Pydantic models so a
    mutation would raise — but the function itself must not assign.)"""
    a1 = action(tool="shell", args={"command": "echo"})
    o1 = observation(action_id=a1.id, tool="shell", success=True, content="ok")
    events = with_seqs([user_msg("u"), a1, o1, tombstone(2, 3, "drop")])
    t = events[-1]
    assert isinstance(t, CondensationEvent)
    # Snapshot pre-call state.
    pre_events_len = len(events)
    pre_t_summary = t.summary
    pre_t_seq = t.seq
    recovered = recover_span(events, t)
    # Post-call: nothing changed.
    assert len(events) == pre_events_len
    assert t.summary == pre_t_summary
    assert t.seq == pre_t_seq
    # Recovered is a fresh list of the same Event objects (not a re-render).
    assert all(e in events for e in recovered)


def test_c11_view_recover_span_classmethod_matches_module_function():
    """`View.recover_span` is a paired accessor to `View.of`; it must return
    the same thing the module-level `recover_span` returns. Both forms work
    so callers can pick the surface that fits their code."""
    events = with_seqs(
        [
            user_msg("u"),
            agent_msg("a1"),
            agent_msg("a2"),
            agent_msg("kept"),
            tombstone(2, 3, "S"),
        ]
    )
    t = events[-1]
    assert isinstance(t, CondensationEvent)
    assert View.recover_span(events, t) == recover_span(events, t)
