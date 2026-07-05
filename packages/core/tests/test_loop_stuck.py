"""Stuck detection — agent-loop-contract.md §10.5.

StuckDetector unit tests (table-driven over the four patterns, using
event_content_eq) + loop integration (STUCK then resume on a new message).
"""

from __future__ import annotations

from disco.core import ConversationStatus, EventSource, MessageEvent, StatusEvent
from disco.core.llm import ModelExecutionPolicy
from disco.core.loop import StuckDetector, StuckThresholds, signals
from disco.core.loop.control import Disp
from disco.core.loop.stuck import barren_streak_no_progress, repeated_verify_no_progress
from event_fakes import action, agent_error, agent_msg, observation, user_msg
from loop_fakes import (
    ScriptedAgent,
    action_step,
    assert_blocked_question_landing,
    build_loop,
    finish_step,
)

CID = "conv"


def _pairs_ao(n, thought="same"):
    """n identical action→observation pairs."""
    out = []
    for _ in range(n):
        out += [action(thought=thought), observation(content="ok")]
    return out


# ---- pattern 1: repeated action→observation ---------------------------------


def test_repeated_action_observation_triggers_at_threshold():
    d = StuckDetector(StuckThresholds(repeat_action_observation=3))
    assert d.is_stuck(_pairs_ao(3)) is True
    assert d.is_stuck(_pairs_ao(2)) is False  # below threshold


# ---- pattern 2: repeated action→error ---------------------------------------


def test_repeated_action_error_triggers_at_threshold():
    d = StuckDetector(StuckThresholds(repeat_action_error=3))
    events = []
    for _ in range(3):
        events += [action(thought="retry"), agent_error("same failure")]
    assert d.is_stuck(events) is True


# ---- pattern 2: the stuck detector compares RAW args, not rendered/snipped ones --
#
# Bug 14 (Build Soak repair #7) regression pin. The LLM-context view snips any
# tool arg > _ARG_SNIP_CHARS to a SHORT placeholder marker keyed on the arg's
# LENGTH (`<{n} chars …>`). Two file_replace_lines calls with DISTINCT new_text of
# IDENTICAL length therefore RENDER to the SAME marker — but they are NOT the same
# action. The detector MUST compare the raw ActionEvent.tool_call.arguments (which
# differ), never the rendered/snipped view, or it would falsely flag two distinct
# large edits as a repeat and STUCK a legitimately-progressing run. (The Bug 14 fix
# is the executor-boundary elision guard; this pins the detector's correctness so a
# future "compare rendered args" refactor can't silently reintroduce the false STUCK.)


def _replace_action(new_text: str):  # noqa: ANN202
    return action(
        thought="patching",
        tool="file_replace_lines",
        args={"path": "src/app.js", "start_line": 1, "end_line": 40, "new_text": new_text},
    )


def test_distinct_large_edits_snip_to_same_marker_but_are_not_stuck():
    from disco.core.events import _ARG_SNIP_CHARS, _snip_args

    big = _ARG_SNIP_CHARS + 100
    text_a = "a" * big
    text_b = "b" * big  # distinct content, IDENTICAL length
    # Premise: the rendered/snipped args collapse to the SAME marker (length-keyed).
    marker_a = _snip_args({"new_text": text_a})["new_text"]
    marker_b = _snip_args({"new_text": text_b})["new_text"]
    assert marker_a == marker_b
    # ...yet the raw args differ.
    assert text_a != text_b

    d = StuckDetector(StuckThresholds(repeat_action_error=2))
    events = [
        _replace_action(text_a),
        agent_error("argument contains the elision placeholder"),
        _replace_action(text_b),
        agent_error("argument contains the elision placeholder"),
    ]
    # Two DISTINCT large edits → NOT stuck (the detector reads raw args, not the
    # rendered marker that would make them look identical).
    assert d.is_stuck(events) is False


def test_repeated_identical_large_edit_is_still_stuck():
    """The other half of the contract: the detector MUST still catch a TRULY
    identical repeated action (same raw new_text) — Bug 14 must not weaken it."""
    from disco.core.events import _ARG_SNIP_CHARS

    same = "z" * (_ARG_SNIP_CHARS + 100)
    d = StuckDetector(StuckThresholds(repeat_action_error=2))
    events = [
        _replace_action(same),
        agent_error("argument contains the elision placeholder"),
        _replace_action(same),
        agent_error("argument contains the elision placeholder"),
    ]
    assert d.is_stuck(events) is True


# ---- pattern 2: NONCRITICAL bookkeeping tools are exempt from action→error ----
#
# A malformed `update_plan_progress` (a cosmetic, declarative plan-tracker tool —
# signals._NONCRITICAL_FAILURE_TOOLS) repeatedly failing schema validation is NOT
# "stuck on the task": some models (MiniMax-M3) intermittently emit steps=[""]. It
# must NOT trip the FATAL `repeated_action_error` and kill an otherwise-productive
# build (the dedicated bookkeeping gate is the right backstop for pure spam). Mirrors
# the existing count_recent_failures exclusion (signals.py).


def test_noncritical_update_plan_progress_error_loop_not_stuck():
    d = StuckDetector(StuckThresholds(repeat_action_error=3))
    events = []
    for _ in range(5):  # well past the threshold
        events += [
            action(thought="track", tool="update_plan_progress", args={"steps": [""]}),
            agent_error("steps.0: Input should be a valid object"),
        ]
    assert d.is_stuck(events) is False  # exempt — cosmetic bookkeeping, not task-stuck


def test_mixed_noncritical_and_real_error_still_detects_real_loop():
    # A real execution tool (file_write) erroring identically 3x IS stuck, even when
    # interleaved with exempt update_plan_progress errors — the exemption only drops
    # the cosmetic pairs; the real loop remains detectable.
    d = StuckDetector(StuckThresholds(repeat_action_error=3))
    events = []
    for _ in range(3):
        events += [
            action(thought="track", tool="update_plan_progress", args={"steps": [""]}),
            agent_error("steps.0: Input should be a valid object"),
            action(thought="write", tool="file_write", args={"path": "a.txt", "content": "x"}),
            agent_error("permission denied: read-only filesystem"),
        ]
    assert d.is_stuck(events) is True  # the real file_write error loop still fires


# ---- pattern 3: agent monologue ---------------------------------------------


def test_agent_monologue_triggers_at_threshold():
    d = StuckDetector(StuckThresholds(agent_monologue=4))
    assert d.is_stuck([agent_msg("a"), agent_msg("b"), agent_msg("c"), agent_msg("d")]) is True
    assert d.is_stuck([agent_msg("a"), agent_msg("b")]) is False


# ---- pattern 4: alternating A-B-A-B -----------------------------------------


def test_alternating_actions_trigger_at_threshold():
    d = StuckDetector(StuckThresholds(alternating=3))
    seq = []
    for _ in range(3):
        seq += [action(thought="A"), action(thought="B")]  # A,B repeated 3x
    assert d.is_stuck(seq) is True
    # Identical (not alternating) is pattern 1, not pattern 4:
    assert d._alternating([action(thought="A")] * 6) is False


# ---- reset + window ---------------------------------------------------------


def test_user_message_resets_stuck():
    d = StuckDetector(StuckThresholds(repeat_action_observation=3))
    events = _pairs_ao(3) + [user_msg("new instruction")] + _pairs_ao(1)
    assert d.is_stuck(events) is False  # cleared after the user message


def test_events_outside_window_dont_count():
    d = StuckDetector(StuckThresholds(repeat_action_observation=3, scan_window=4))
    # 3 stuck pairs, but only the last `scan_window`(=4) events are inspected by
    # the loop; here we emulate by trimming as the loop's _recent() would.
    events = _pairs_ao(3)  # 6 events
    assert d.is_stuck(events[-4:]) is False  # only 2 pairs visible in the window


def test_probe_spin_trips_on_varying_server_status_output():
    d = StuckDetector(StuckThresholds(probe_spin_calls=12))
    events = []
    for i in range(12):
        probe = action(
            thought=f"poll {i}",
            tool="server_status",
            args={"url": "http://127.0.0.1:5173"},
        )
        events += [
            probe,
            observation(
                action_id=probe.id,
                tool="server_status",
                content=f"server still starting; attempt={i}",
            ),
        ]

    result = d.evaluate(events)

    assert result.is_stuck is True
    assert result.reason == "probe_spin"


def _barren_read_events(*, error: str = "books.json: No such file or directory"):
    events = [user_msg("inspect the project")]
    for i in range(8):
        tool = "file_read" if i in {1, 4, 7} else "think"
        a = action(thought=f"read {i}", tool=tool, args={"path": "books.json"})
        events.append(a)
        if tool == "file_read":
            events.append(agent_error(error, action_id=a.id))
        else:
            events.append(observation(action_id=a.id, tool="think", content="noted"))
    return events


async def test_barren_streak_identical_read_errors_marks_no_progress():
    loop, store = build_loop(ScriptedAgent([]))
    for event in _barren_read_events():
        await store.append(CID, event)

    events = await store.get_events(CID)
    assert barren_streak_no_progress(events) is True

    disp = await loop._valve.gate_no_progress(events)
    assert disp is Disp.CONTINUE
    after = await store.get_events(CID)
    assert any(
        isinstance(e, StatusEvent) and e.detail == "no_progress"
        for e in after
    )


def test_barren_streak_varying_successful_reads_never_fires():
    events = [user_msg("read around")]
    for i in range(8):
        a = action(thought=f"read {i}", tool="file_read", args={"path": f"{i}.txt"})
        events.extend(
            [
                a,
                observation(
                    action_id=a.id,
                    tool="file_read",
                    content=f"unique successful content {i}",
                ),
            ]
        )

    assert barren_streak_no_progress(events) is False


def test_barren_streak_mutating_tool_resets_window():
    events = _barren_read_events()
    mutating = action(
        thought="write once",
        tool="file_write",
        args={"path": "books.json", "content": "[]"},
    )
    # Keep exactly eight recent actions, with a mutating action inside the window.
    events = events[:1] + events[3:] + [mutating, observation(action_id=mutating.id)]

    assert barren_streak_no_progress(events) is False


def test_barren_streak_no_op_write_does_not_reset_window():
    events = [user_msg("finish the edit")]
    for i in range(5):
        a = action(thought=f"think {i}", tool="think", args={})
        events.extend([a, observation(action_id=a.id, tool="think", content="noted")])
    for i in range(3):
        a = action(
            thought=f"write {i}",
            tool="file_write",
            args={"path": "books.json", "content": "[]"},
        )
        events.extend([a, agent_error("no_op_write", action_id=a.id)])

    assert barren_streak_no_progress(events) is True


def test_barren_streak_fresh_read_required_does_not_reset_window():
    events = [user_msg("patch the file")]
    for i in range(5):
        a = action(thought=f"think {i}", tool="think", args={})
        events.extend([a, observation(action_id=a.id, tool="think", content="noted")])
    for i in range(3):
        a = action(
            thought=f"edit {i}",
            tool="file_edit",
            args={"path": "books.json", "old": "a", "new": "b"},
        )
        events.extend([a, agent_error("FRESH_READ_REQUIRED", action_id=a.id)])

    assert barren_streak_no_progress(events) is True


def test_barren_streak_successful_mutation_still_resets_window():
    events = [user_msg("finish the edit")]
    for i in range(7):
        a = action(thought=f"think {i}", tool="think", args={})
        events.extend([a, observation(action_id=a.id, tool="think", content="noted")])
    mutating = action(
        thought="write once",
        tool="file_write",
        args={"path": "books.json", "content": "[]"},
    )
    events.extend([mutating, observation(action_id=mutating.id, tool="file_write")])

    assert barren_streak_no_progress(events) is False


# ---- loop integration: STUCK then resume ------------------------------------


async def test_loop_tries_a_temp_escape_before_going_stuck():
    """Escape-then-halt: the FIRST time the loop detects an action→obs/error rut it
    drops a `stuck_escape` MARKER (a status event) AND injects a C7 escape reminder
    (from the rotating pool, with a per-attempt serialization nonce) — the
    reminder lives INSIDE the existing stuck-escape path so the c97c1b3
    no-nudge-outside-escape invariant is preserved. The model retries the next
    step at a high temperature. It does NOT halt yet. Only if it's STILL stuck
    after that retry does STUCK fire — and the escape buys the model at least
    one extra step it wouldn't have had under immediate-halt."""
    agent = ScriptedAgent([action_step()] * 6 + [finish_step()])
    loop, store = build_loop(agent, stuck_thresholds=StuckThresholds(repeat_action_observation=3))
    await loop.send_message("repeat please")
    state = await loop.run()

    events = await store.get_events(CID)
    # the escape was attempted (a status marker on the log)…
    assert any(isinstance(e, StatusEvent) and e.detail == "stuck_escape" for e in events)
    # …and a C7 escape reminder WAS injected (it is the in-context half of the
    # anti-self-imitation pair; the temperature is the sampling half). The
    # reminder is a <system-reminder> MessageEvent and the FIRST one in the
    # run corresponds to attempt index 0 (the pool rotates from there).
    def _is_escape_reminder(e):
        return (
            isinstance(e, MessageEvent)
            and e.source == EventSource.ENVIRONMENT
            and e.message is not None
            and "<system-reminder>" in (e.message.content or "")
            and "disco:escape-attempt=" in (e.message.content or "")
        )

    escape_reminders = [e for e in events if _is_escape_reminder(e)]
    assert len(escape_reminders) >= 1, (
        f"expected at least one C7 escape reminder in the run, got 0; "
        f"events={[type(e).__name__ for e in events]}"
    )
    # The first escape reminder must be attempt=0 (the count starts at 0
    # before the first marker is emitted).
    assert "disco:escape-attempt=0" in (escape_reminders[0].message.content or ""), (
        f"first escape reminder must use attempt=0, got: {escape_reminders[0].message.content!r}"
    )
    # …and it still ended STUCK because the model kept repeating after the retry.
    assert state.execution_status == ConversationStatus.AWAITING_USER_QUESTION  # terminal-collapse: interactive stuck lands explain+ask


# ---- C7 — escape reminder rotation + serialization seed --------------------
#
# The existing escape path bumps temperature; C7 adds (a) a rotating pool of
# reminder phrasings selected by attempt count, and (b) a per-attempt
# serialization nonce embedded in the reminder so consecutive escape attempts
# are NOT byte-identical. Deterministic under a fixed seed (testable). The
# invariant that NO automatic nudge is injected OUTSIDE the existing
# stuck-escape path (c97c1b3) is preserved — every escape reminder is
# emitted FROM the escape branch itself.


def _collect_escape_reminders(events):
    """Pull the C7 escape-reminder MessageEvents out of an event log, in
    emission order. A C7 reminder is identifiable by its embedded
    `disco:escape-attempt=N` nonce in a `<system-reminder>` block.
    """
    out = []
    for e in events:
        if (
            isinstance(e, MessageEvent)
            and e.source == EventSource.ENVIRONMENT
            and e.message is not None
            and "disco:escape-attempt=" in (e.message.content or "")
        ):
            out.append(e)
    return out


async def test_c7_escape_reminders_rotate_deterministically_by_attempt_count():
    """C7: consecutive escape attempts use DIFFERENT reminder text (the
    pool has 3 entries, so attempt 0 → pool[0], attempt 1 → pool[1], ...
    cycling back to pool[0] at attempt 3). The nonces embedded in the
    reminders also differ (attempt=0 vs attempt=1 vs attempt=2), so the
    bytes sent to the model are different across attempts — the
    anti-self-imitation half of the C7 pair.

    Bounded: a fixed small script across THREE user turns, each producing
    one escape (one user turn = one escape, by the existing
    one-reframe-per-turn rule). The pool has 3 entries, so three escapes
    give us pool[0], pool[1], pool[2] — enough to prove rotation without
    the pool wrapping.
    """
    from disco.core.loop import StuckThresholds

    # Each user turn: 3 actions to trigger stuck, then 1 retry action that
    # fails (so the run halts STUCK). 4 actions per turn. Three turns = 12
    # actions, plus a final finish to cleanly drain the queue.
    # terminal-collapse: each blocked landing consumes one scripted turn for the
    # model-authored explanation — supply 3 extra identical actions so turn 3
    # still repeats into the breaker instead of draining to finish_step.
    agent = ScriptedAgent([action_step()] * 15 + [finish_step()])
    loop, store = build_loop(
        agent, stuck_thresholds=StuckThresholds(repeat_action_observation=3)
    )

    # Turn 1: triggers escape 0 (pool[0]).
    await loop.send_message("turn 1: repeat please")
    state = await loop.run()
    # terminal-collapse: interactive stuck lands explain+ask
    assert state.execution_status == ConversationStatus.AWAITING_USER_QUESTION, (
        f"turn 1 should land the blocked ask (escape spent), got {state.execution_status}"
    )

    # Turn 2: triggers escape 1 (pool[1]). The script's next 4 actions fire
    # here (3 to trigger stuck, 1 to retry). The attempt count is global
    # within the conversation, so the second escape gets index 1, not 0.
    await loop.send_message("turn 2: try again")
    state = await loop.run()
    # terminal-collapse: interactive stuck lands explain+ask
    assert state.execution_status == ConversationStatus.AWAITING_USER_QUESTION, (
        f"turn 2 should end STUCK, got {state.execution_status}"
    )

    # Turn 3: triggers escape 2 (pool[2]).
    await loop.send_message("turn 3: one more")
    state = await loop.run()
    # terminal-collapse: interactive stuck lands explain+ask
    assert state.execution_status == ConversationStatus.AWAITING_USER_QUESTION, (
        f"turn 3 should end STUCK, got {state.execution_status}"
    )

    events = await store.get_events(CID)
    reminders = _collect_escape_reminders(events)

    # Three escapes ⇒ three reminders.
    assert len(reminders) == 3, (
        f"expected exactly 3 escape reminders across 3 user turns, "
        f"got {len(reminders)}; events types: {[type(e).__name__ for e in events]}"
    )

    # Determinism under a fixed seed: the attempt indices embedded in the
    # nonces must be 0, 1, 2 in order. This is what `attempt_count % len(POOL)`
    # produces (with the count taken BEFORE each marker is emitted, so the
    # first marker sees count=0 and the second sees count=1).
    expected_attempts = [
        "disco:escape-attempt=0",
        "disco:escape-attempt=1",
        "disco:escape-attempt=2",
    ]
    actual_attempts = []
    for r in reminders:
        content = r.message.content or ""
        if "<!--" in content:
            actual_attempts.append(content.split("<!--")[1].split("-->")[0].strip())
    assert actual_attempts == expected_attempts, (
        f"escape attempts must cycle deterministically: "
        f"expected {expected_attempts}, got {actual_attempts}"
    )

    # Anti-self-imitation: the reminder CONTENT (not just the nonce) must
    # differ between consecutive attempts. We compare the full message
    # content bytes — reminder 0 must NOT be byte-identical to reminder 1,
    # and reminder 1 must NOT be byte-identical to reminder 2.
    contents = [r.message.content for r in reminders]
    for i in range(1, len(contents)):
        assert contents[i] != contents[i - 1], (
            f"escape reminder {i} must differ in bytes from reminder {i-1} "
            f"(anti-self-imitation), but both are:\n{contents[i]!r}"
        )


async def test_c7_non_escape_step_is_unchanged():
    """C7 — non-escape / non-stuck steps must NOT receive an escape reminder
    (the c97c1b3 invariant: no automatic nudge outside the existing
    stuck-escape). A normal, non-stuck run with a finish step at the end
    must produce ZERO escape reminders in the event log.

    Also asserts: a non-escape step is NOT given the high escape temperature
    by the loop's per-step decision (verifiable via the events log: the
    `stuck_escape` status marker is the audit signal that the escape path
    ran; its absence means the path did not run, so neither the temperature
    nor the reminder fired)."""
    # A short, well-behaved run: one action, then finish. Never gets stuck
    # (one observation pair is below the 3-repeat threshold), so the
    # escape path is never entered.
    agent = ScriptedAgent([action_step(), finish_step()])
    loop, store = build_loop(agent)
    await loop.send_message("just do it once")
    state = await loop.run()
    assert state.execution_status == ConversationStatus.FINISHED

    events = await store.get_events(CID)
    reminders = _collect_escape_reminders(events)
    assert reminders == [], (
        f"non-escape run must produce zero C7 escape reminders, got: "
        f"{[r.message.content for r in reminders]}"
    )

    # And no `stuck_escape` marker either (the escape path never ran):
    assert not any(
        isinstance(e, StatusEvent) and e.detail == "stuck_escape" for e in events
    ), "stuck_escape marker must be absent in a non-escape run"

    # And no <system-reminder> MessageEvent at all (the env reminder channel
    # is reserved for c97c1b3-bounded nudges — none should fire here):
    def _is_any_reminder(e):
        return (
            isinstance(e, MessageEvent)
            and e.source == EventSource.ENVIRONMENT
            and e.message is not None
            and "<system-reminder>" in (e.message.content or "")
        )

    assert not any(_is_any_reminder(e) for e in events), (
        f"non-escape run must have zero <system-reminder> env messages, "
        f"got events: {[type(e).__name__ for e in events]}"
    )


def test_c7_stuck_escape_attempt_count_helper():
    """C7: `_stuck_escape_attempt_count` must return 0 when no escape
    has happened, and the count must equal the number of `stuck_escape`
    markers in the event log. This is the rotation key — a wrong count
    would make the pool selection non-deterministic under a fixed seed."""

    # No events: count is 0.
    assert signals.stuck_escape_attempt_count([]) == 0

    # One escape marker: count is 1 (the marker we just emitted).
    from event_fakes import user_msg
    events = [
        user_msg("go"),
        action(thought="a"),
        observation(),
        action(thought="a"),
        observation(),
        action(thought="a"),
        observation(),
        StatusEvent(status=ConversationStatus.RUNNING, detail="stuck_escape"),
    ]
    assert signals.stuck_escape_attempt_count(events) == 1

    # Three markers: count is 3 (rotation wraps modulo len(POOL)).
    events += [
        StatusEvent(status=ConversationStatus.RUNNING, detail="stuck_escape"),
        StatusEvent(status=ConversationStatus.RUNNING, detail="stuck_escape"),
    ]
    assert signals.stuck_escape_attempt_count(events) == 3

    # A user message does NOT reset the global count (the rotation is
    # global within a run, by design — see _stuck_escape_attempt_count
    # docstring).
    events += [user_msg("ok try again")]
    assert signals.stuck_escape_attempt_count(events) == 3


def test_c7_pool_selector_is_deterministic_and_injective_across_attempts():
    """C7: the pool selector must (a) be deterministic under a fixed attempt
    count, (b) differ in bytes between consecutive attempt indices, and
    (c) cycle with period = len(POOL). This locks in the rotation
    contract without driving the full loop."""
    from disco.core.loop.messages import _STUCK_ESCAPE_REMINDER_POOL, _stuck_escape_reminder

    n = len(_STUCK_ESCAPE_REMINDER_POOL)
    # (a) determinism: same attempt → same reminder.
    assert _stuck_escape_reminder(0) == _stuck_escape_reminder(0)
    assert _stuck_escape_reminder(2) == _stuck_escape_reminder(2)

    # (b) bytes differ between consecutive attempts (anti-self-imitation).
    seen = [_stuck_escape_reminder(i) for i in range(n)]
    assert len(set(seen)) == n, (
        f"the pool must have {n} distinct entries, got {len(set(seen))} unique "
        f"contents: {seen!r}"
    )

    # (c) cycling: attempt n must re-use the attempt-0 entry.
    assert _stuck_escape_reminder(n) == _stuck_escape_reminder(0)
    assert _stuck_escape_reminder(n + 1) == _stuck_escape_reminder(1)

    # And each reminder carries its own nonce (the serialization seed
    # varies with attempt count).
    for i in range(n):
        assert f"disco:escape-attempt={i}" in _stuck_escape_reminder(i), (
            f"reminder for attempt={i} must embed disco:escape-attempt={i}, "
            f"got: {_stuck_escape_reminder(i)!r}"
        )


# ---- W-31: the STUCK breaker is NAMED (StuckResult.reason + StatusEvent.detail) --


def test_stuck_result_names_the_breaker_per_pattern():
    """W-31 — `evaluate().reason` NAMES the first stuck pattern that fired so the
    halt can identify WHICH breaker caught the run. Not-stuck ⇒ reason is None;
    the bool stays byte-identical to `is_stuck()`."""
    # pattern 1: repeated action→observation (the W-30/W-31 repeated-reads case).
    d1 = StuckDetector(StuckThresholds(repeat_action_observation=3))
    r1 = d1.evaluate(_pairs_ao(3))
    assert r1.is_stuck is True
    assert r1.reason == "repeated_action_observation"

    # pattern 2: repeated action→error.
    d2 = StuckDetector(StuckThresholds(repeat_action_error=3))
    errs = []
    for _ in range(3):
        errs += [action(thought="retry"), agent_error("same failure")]
    r2 = d2.evaluate(errs)
    assert r2.is_stuck is True
    assert r2.reason == "repeated_action_error"

    # pattern 3: agent monologue.
    d3 = StuckDetector(StuckThresholds(agent_monologue=4))
    r3 = d3.evaluate([agent_msg("a"), agent_msg("b"), agent_msg("c"), agent_msg("d")])
    assert r3.is_stuck is True
    assert r3.reason == "agent_monologue"

    # not stuck ⇒ reason is None.
    r_none = d1.evaluate(_pairs_ao(2))
    assert r_none.is_stuck is False
    assert r_none.reason is None


async def test_stuck_status_event_names_the_breaker():
    """When gate_stuck halts, it explains and asks with the old breaker detail
    retained as landing metadata."""
    agent = ScriptedAgent([action_step()] * 6 + [finish_step()])
    loop, store = build_loop(
        agent, stuck_thresholds=StuckThresholds(repeat_action_observation=3)
    )
    await loop.send_message("repeat please")
    state = await loop.run()
    assert state.execution_status == ConversationStatus.AWAITING_USER_QUESTION

    events = await store.get_events(CID)
    assert_blocked_question_landing(events, legacy_detail="repeated_action_observation")


async def test_loop_goes_stuck_then_resumes_on_new_message():
    # Same action forever → identical action→obs cycles → AWAITING_USER (after the reframe
    # escape is spent: 3 to trigger + ≥1 retry that's still stuck).
    agent = ScriptedAgent([action_step()] * 6 + [finish_step()])
    loop, store = build_loop(agent, stuck_thresholds=StuckThresholds(repeat_action_observation=3))
    await loop.send_message("repeat please")
    state = await loop.run()
    assert state.execution_status == ConversationStatus.AWAITING_USER_QUESTION

    # A new message resets and the loop resumes; the next step finishes.
    await loop.send_message("ok stop, finish")
    resumed = await loop.run()
    assert resumed.execution_status == ConversationStatus.FINISHED
    statuses = [e.status for e in await store.get_events(CID) if isinstance(e, StatusEvent)]
    assert ConversationStatus.AWAITING_USER_QUESTION in statuses
    assert statuses[-1] == ConversationStatus.FINISHED


# ---- bookkeeping streak (issue C) must scale with plan size (T7 / E3) --------


def _seed_approved_plan(store, *, n_steps: int):
    """Pre-populate the store with user msg + a PlanEvent with n_steps steps +
    a `plan_approved` RUNNING marker so a fresh loop enters execution mode
    against an active plan. Returns nothing (mutates store)."""
    from disco.core import EventSource, LLMMessage, MessageEvent, PlanEvent
    from disco.core.events import ConversationStatus

    async def _seed():
        await store.append(
            CID,
            MessageEvent(
                source=EventSource.USER, message=LLMMessage(role="user", content="build it")
            ),
        )
        await store.append(
            CID,
            PlanEvent(
                summary="p",
                steps=[{"title": f"step {i}"} for i in range(1, n_steps + 1)],
                revision=1,
            ),
        )
        await store.append(
            CID,
            StatusEvent(status=ConversationStatus.RUNNING, detail="plan_approved"),
        )

    return _seed


async def test_bookkeeping_halt_caps_genuine_spam_on_tiny_plan():
    """E3: a 1-step plan is a TINY plan. Eight `plan_step` calls on it are clearly
    spam (only 1 step exists to mark), so the bookkeeping cap MUST halt the
    run with `bookkeeping_only`. The cap is a guard against a model that just
    shuffles the plan tracker forever; it must still fire on this case."""
    from disco.core import ConversationStatus
    from disco.core import SqliteEventStore as Store
    from disco.core.events import StatusEvent as CoreStatusEvent
    from disco.core.llm import OperatingMode

    store = Store(":memory:")
    await _seed_approved_plan(store, n_steps=1)()
    # Vary the `thought` so the StuckDetector's repeat_action_observation
    # doesn't fire first — we want THIS test to exercise the bookkeeping
    # halt specifically (the StuckDetector has its own coverage).
    steps = [
        action_step("plan_step", {"index": 1, "state": "done"}, thought=f"marking {i}")
        for i in range(8)
    ]
    # Cap the script so a broken loop can't burn the suite.
    agent = ScriptedAgent(steps + [finish_step()])
    loop, _ = build_loop(agent, store=store, mode=OperatingMode.LONG_HORIZON)
    state = await loop.run()

    # bookkeeping halt explained and asked with the right legacy detail
    assert state.execution_status == ConversationStatus.AWAITING_USER_QUESTION
    events = await store.get_events(CID)
    assert_blocked_question_landing(events, legacy_detail="bookkeeping_only")
    # And the model wasn't allowed to keep going — no FINISHED, no plan_step count > 6.
    assert state.execution_status != ConversationStatus.FINISHED


async def test_bookkeeping_halt_does_not_trip_legit_burst_on_long_plan():
    """E3 inverse: an 8-step plan legitimately needs 8 `plan_step` calls to
    mark each step done. Eight such calls in a row is NOT spam — it's the
    plan-tracker being kept honest. The bookkeeping cap MUST NOT halt on this
    case; the run should reach FINISHED once the agent declares done."""
    from disco.core import ConversationStatus
    from disco.core import SqliteEventStore as Store
    from disco.core.events import StatusEvent as CoreStatusEvent
    from disco.core.llm import OperatingMode

    store = Store(":memory:")
    await _seed_approved_plan(store, n_steps=8)()
    # Each plan_step has a different index (1..8) so the actions are
    # semantically distinct — the StuckDetector wouldn't fire anyway, but
    # this matches what a real model would emit.
    steps = [
        action_step(
            "plan_step", {"index": i, "state": "done"}, thought=f"marking step {i}"
        )
        for i in range(1, 9)
    ]
    agent = ScriptedAgent(steps + [finish_step()])
    loop, _ = build_loop(agent, store=store, mode=OperatingMode.LONG_HORIZON)
    state = await loop.run()

    events = await store.get_events(CID)
    stuck_statuses = [
        e
        for e in events
        if isinstance(e, CoreStatusEvent) and e.status == ConversationStatus.STUCK
    ]
    bookkeeping_halts = [e for e in stuck_statuses if e.detail == "bookkeeping_only"]
    assert bookkeeping_halts == [], (
        f"8 plan_step calls on an 8-step plan MUST NOT trip the bookkeeping halt, "
        f"but got {len(bookkeeping_halts)} bookkeeping_only STUCK events"
    )
    # And the run landed cleanly (the plan is complete, the finish was accepted).
    assert state.execution_status == ConversationStatus.FINISHED, (
        f"expected FINISHED, got {state.execution_status}"
    )


# ---- F6 — per-file patch-spiral → full-rewrite directive (gated) ------------
#
# A weak model can patch-spiral one file: try a small edit, fail, try a
# slightly different edit, fail, … — none of those cycles is byte-identical
# (so patterns 1–4 don't fire), but the file is clearly stuck. The detector
# counts failures + total attempts PER FILE and surfaces a `RewriteDirective`
# naming the spiraling file when both reach the threshold AND assist is ON.
# Assist OFF ⇒ `rewrite_directive` is always None and the existing patterns
# return the same stuck bool they always have. The directive is a richer
# signal the engine can later consume via `StuckDetector.evaluate()`; the
# bool facade `is_stuck()` is unchanged so the engine doesn't have to be
# touched for this PR.


def _failed_patch(path: str, n: int, *, vary_thought: bool = True):
    """Build n (ActionEvent, AgentErrorEvent) pairs targeting `path`. Each
    ActionEvent has a distinct id so `event_content_eq` would NOT group them
    (patterns 1–4 stay quiet), but they all hit the same file. A real model
    would vary the patch args per attempt; we vary the thought so a duplicate
    argument doesn't make them collapse."""
    out = []
    for i in range(n):
        a = action(
            thought=("retry " + str(i)) if vary_thought else "retry",
            tool="file_edit",
            args={"path": path, "patch": f"@@ -{i} +{i},1 @@"},
        )
        out.append(a)
        out.append(agent_error("patch failed", action_id=a.id))
    return out


def _successful_patch(path: str, n: int):
    """n (ActionEvent, ObservationEvent success=True) pairs targeting `path`."""
    out = []
    for i in range(n):
        a = action(
            thought="ok " + str(i), tool="file_edit", args={"path": path, "patch": f"ok-{i}"}
        )
        out.append(a)
        out.append(observation(action_id=a.id, content="ok"))
    return out


def _rewrite_directive_markers(events, path: str):
    return [
        e
        for e in events
        if isinstance(e, StatusEvent)
        and e.status == ConversationStatus.RUNNING
        and e.detail == f"rewrite_directive:{path}"
    ]


def _rewrite_directive_reminders(events, path: str):
    return [
        e
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and e.message.role == "user"
        and f"on `{path}`" in e.message.content
        and "Rewrite the ENTIRE file cleanly" in e.message.content
    ]


async def _seed_f6_failed_spiral(store, path: str) -> None:
    await store.append(CID, user_msg("fix it"))
    for event in _failed_patch(path, 3):
        await store.append(CID, event)


def test_f6_per_file_rewrite_directive_fires_on_spiral_assist_on():
    """F6 — assist ON, one file spirals past the threshold ⇒ the rewrite
    directive names that file, with the failure/attempt counts it fired on."""
    d = StuckDetector(
        StuckThresholds(per_file_rewrite_failures=3, per_file_rewrite_min_attempts=3), assist=True
    )
    events = _failed_patch("a/foo.py", 3)
    result = d.evaluate(events)
    assert result.is_stuck is False  # patterns 1–4 don't fire (each attempt is distinct)
    assert result.rewrite_directive is not None, (
        f"expected a RewriteDirective for the spiraling file, got None; "
        f"events={[type(e).__name__ for e in events]}"
    )
    rd = result.rewrite_directive
    assert rd.kind == "full_rewrite"
    assert rd.path == "a/foo.py", f"directive must name the spiraling file, got {rd.path!r}"
    assert rd.failures == 3
    assert rd.attempts == 3


def test_f6_per_file_rewrite_directive_does_not_fire_below_threshold_assist_on():
    """F6 — assist ON, only 2 failed patches on a file (below the 3 threshold) ⇒
    the directive does NOT fire. The engine needs a real spiral, not one or
    two unlucky patches."""
    d = StuckDetector(
        StuckThresholds(per_file_rewrite_failures=3, per_file_rewrite_min_attempts=3),
        assist=True,
    )
    events = _failed_patch("a/foo.py", 2)
    result = d.evaluate(events)
    assert result.rewrite_directive is None, (
        f"2 failed patches must NOT cross the 3-failure threshold, "
        f"got directive: {result.rewrite_directive!r}"
    )


def test_f6_per_file_rewrite_directive_isolates_per_file_assist_on():
    """F6 — assist ON, two files touched: A spirals past the threshold while B
    has only one failed patch. The directive names A (not B). The tracker is
    per-file; cross-file pollution is the bug to guard against."""
    d = StuckDetector(
        StuckThresholds(per_file_rewrite_failures=3, per_file_rewrite_min_attempts=3),
        assist=True,
    )
    a_fails = _failed_patch("a/spiral.py", 3)
    b_one = _failed_patch("b/quiet.py", 1)
    events = a_fails + b_one
    result = d.evaluate(events)
    assert result.rewrite_directive is not None
    assert result.rewrite_directive.path == "a/spiral.py", (
        f"directive must name the spiraling file a/spiral.py, "
        f"got {result.rewrite_directive.path!r} (b/quiet.py has only 1 failure)"
    )


def test_f6_per_file_rewrite_directive_off_is_byte_identical():
    """F6 — assist OFF (the default) MUST NOT surface a rewrite directive, no
    matter how bad the spiral looks. The bool facade `is_stuck()` returns the
    same value it would have pre-F6 (the per-file tracker is a no-op when the
    gate is closed)."""
    d_off = StuckDetector(
        StuckThresholds(per_file_rewrite_failures=3, per_file_rewrite_min_attempts=3),
        assist=False,
    )
    d_default = StuckDetector(  # noqa: F841 — default assist is False, the engine's path today
        StuckThresholds(per_file_rewrite_failures=3, per_file_rewrite_min_attempts=3)
    )
    events = _failed_patch("a/foo.py", 3)

    # (1) The bool facade is byte-identical to pre-F6: patterns 1–4 don't fire
    # on distinct attempts, so is_stuck must be False in both modes.
    assert d_off.is_stuck(events) is False
    assert d_default.is_stuck(events) is False

    # (2) The richer `evaluate()` also returns is_stuck=False (same bool), and
    # the new `rewrite_directive` field is None — the gate kept the tracker
    # inert. This is the byte-identical guarantee: no NEW signal appears when
    # assist is OFF, regardless of how many files spiral.
    res_off = d_off.evaluate(events)
    res_default = d_default.evaluate(events)
    assert res_off.is_stuck is False
    assert res_default.is_stuck is False
    assert res_off.rewrite_directive is None, (
        f"assist OFF must NEVER surface a rewrite directive, got {res_off.rewrite_directive!r}"
    )
    assert res_default.rewrite_directive is None, (
        f"default constructor (assist=False) must NEVER surface a rewrite directive, "
        f"got {res_default.rewrite_directive!r}"
    )


def test_f6_per_file_rewrite_directive_successful_patches_dont_fire_assist_on():
    """F6 — assist ON, three SUCCESSFUL patches to a file must NOT fire the
    directive. The tracker only counts FAILURES; a file the model is
    legitimately landing edits on is not spiraling."""
    d = StuckDetector(
        StuckThresholds(per_file_rewrite_failures=3, per_file_rewrite_min_attempts=3),
        assist=True,
    )
    events = _successful_patch("a/landing.py", 3)
    result = d.evaluate(events)
    assert result.rewrite_directive is None, (
        f"3 successful patches must NOT fire the rewrite directive (zero failures), "
        f"got {result.rewrite_directive!r}"
    )


def test_f6_per_file_rewrite_directive_ignores_non_mutating_tools_assist_on():
    """F6 — assist ON, three FAILING `shell` calls (not file-mutating) must NOT
    fire the directive. The tracker only counts mutating tools — a failing
    shell command isn't a patch attempt on a file."""
    d = StuckDetector(
        StuckThresholds(per_file_rewrite_failures=3, per_file_rewrite_min_attempts=3),
        assist=True,
    )
    out = []
    for i in range(3):
        a = action(thought="shell " + str(i), tool="shell", args={"cmd": "false"})
        out.append(a)
        out.append(agent_error("exit 1", action_id=a.id))
    result = d.evaluate(out)
    assert result.rewrite_directive is None, (
        f"failing shell calls must NOT count as patch attempts, "
        f"got directive: {result.rewrite_directive!r}"
    )


def test_f6_per_file_rewrite_directive_threshold_zero_disables_assist_on():
    """F6 — a 0 threshold disables the tracker even when assist is ON. This is
    the escape hatch: an operator who wants the old behavior back can set
    `per_file_rewrite_failures=0` (or `per_file_rewrite_min_attempts=0`) without
    flipping the assist flag globally."""
    d = StuckDetector(
        StuckThresholds(per_file_rewrite_failures=0, per_file_rewrite_min_attempts=0),
        assist=True,
    )
    events = _failed_patch("a/foo.py", 5)
    result = d.evaluate(events)
    assert result.rewrite_directive is None, (
        f"a 0 threshold must disable the tracker, got {result.rewrite_directive!r}"
    )


def test_f6_per_file_rewrite_directive_resets_on_user_message_assist_on():
    """F6 — a user message resets the per-file spiral (the detector trims to
    events after the last user message, same as patterns 1–4). Two failed
    patches before a user message + one after ⇒ still only 1 failure on the
    post-user window, below the 3 threshold, so the directive does NOT fire."""
    d = StuckDetector(
        StuckThresholds(per_file_rewrite_failures=3, per_file_rewrite_min_attempts=3),
        assist=True,
    )
    pre = _failed_patch("a/foo.py", 2)
    post = _failed_patch("a/foo.py", 1)
    events = pre + [user_msg("try a different angle")] + post
    result = d.evaluate(events)
    assert result.rewrite_directive is None, (
        f"a user message must reset the per-file count; only 1 failure on the "
        f"post-user window must NOT fire, got {result.rewrite_directive!r}"
    )


async def test_f6_gate_stuck_emits_rewrite_directive_for_weak_spiral():
    """F6 — weak/assist gate consumes the detector directive before generic stuck."""
    path = "src/app.py"
    thresholds = StuckThresholds(per_file_rewrite_failures=3, per_file_rewrite_min_attempts=3)
    loop, store = build_loop(
        ScriptedAgent([]),
        stuck_thresholds=thresholds,
        model_policy=ModelExecutionPolicy(tier="weak"),
    )
    await _seed_f6_failed_spiral(store, path)

    disp = await loop._valve.gate_stuck(await store.get_events(CID))

    assert disp is Disp.CONTINUE
    events = await store.get_events(CID)
    assert len(_rewrite_directive_markers(events, path)) == 1
    reminders = _rewrite_directive_reminders(events, path)
    assert len(reminders) == 1
    content = reminders[0].message.content
    assert content == (
        f"You have made 3 failed patch attempts on `{path}`. "
        "Stop patching it line-by-line. Rewrite the ENTIRE file cleanly in one "
        "`file_write` call (write the full corrected content), then re-verify."
    )


async def test_f6_gate_stuck_dedupes_rewrite_directive_until_successful_edit():
    """F6 — repeat gate passes do not re-emit while the same marker is active."""
    path = "src/app.py"
    thresholds = StuckThresholds(per_file_rewrite_failures=3, per_file_rewrite_min_attempts=3)
    loop, store = build_loop(
        ScriptedAgent([]),
        stuck_thresholds=thresholds,
        model_policy=ModelExecutionPolicy(tier="weak"),
    )
    await _seed_f6_failed_spiral(store, path)

    first = await loop._valve.gate_stuck(await store.get_events(CID))
    second = await loop._valve.gate_stuck(await store.get_events(CID))

    assert first is Disp.CONTINUE
    assert second is Disp.FALLTHROUGH
    events = await store.get_events(CID)
    assert len(_rewrite_directive_markers(events, path)) == 1
    assert len(_rewrite_directive_reminders(events, path)) == 1


async def test_f6_gate_stuck_standard_tier_suppresses_rewrite_directive():
    """F6 — standard tier keeps the directive path closed and falls through."""
    path = "src/app.py"
    thresholds = StuckThresholds(per_file_rewrite_failures=3, per_file_rewrite_min_attempts=3)
    loop, store = build_loop(
        ScriptedAgent([]),
        stuck_thresholds=thresholds,
        model_policy=ModelExecutionPolicy.standard(),
    )
    await _seed_f6_failed_spiral(store, path)

    disp = await loop._valve.gate_stuck(await store.get_events(CID))

    assert disp is Disp.FALLTHROUGH
    events = await store.get_events(CID)
    assert _rewrite_directive_markers(events, path) == []
    assert _rewrite_directive_reminders(events, path) == []


# ---- WALK-19 — semantic no-progress detector (failure-independent) ----------
#
# The black-screen-game: a capable model writes a DIFFERENT edit each turn (so
# patterns 1-4 never fire), each edit "succeeds" (writes apply, dev server 200 —
# so the failure-keyed circuit breaker never trips), and it grinds to
# max_iterations. The semantic signal: the same probe/verify OUTCOME recurs
# across many VARIED edits = no real progress.


def _edit(i: int):
    """A distinct (varied) file edit — patterns 1-4 stay quiet on these."""
    return action(thought=f"edit {i}", tool="file_edit", args={"path": "App.tsx", "patch": f"v{i}"})


def _probe(content: str = "HTTP 200, no console errors"):
    """A probe action (server_status) + its observation carrying the outcome."""
    a = action(thought="check the app", tool="server_status", args={})
    o = observation(action_id=a.id, content=content, tool="server_status")
    return [a, o]


def test_no_progress_trips_on_varied_edits_same_symptom():
    """4 DISTINCT edits, each followed by the SAME probe outcome ⇒ trip."""
    events = [user_msg("build the app")]
    for i in range(4):
        events.append(_edit(i))
        events += _probe()  # identical outcome every time
    assert repeated_verify_no_progress(events) is True


def test_no_progress_does_not_trip_when_outcome_changes():
    """Genuine progress: the final edit CHANGES the probe outcome ⇒ no trip
    (the trailing constant-outcome run is just the new, single observation)."""
    events = [user_msg("build the app")]
    for i in range(3):
        events.append(_edit(i))
        events += _probe("HTTP 200, blank page")
    events.append(_edit(3))
    events += _probe("HTTP 200, heading now visible")  # outcome finally changed
    assert repeated_verify_no_progress(events) is False


def test_no_progress_below_distinct_edit_threshold_does_not_trip():
    """Only 3 distinct edits against a stable outcome (< the default 4) ⇒ no trip."""
    events = [user_msg("go")]
    for i in range(3):
        events.append(_edit(i))
        events += _probe()
    assert repeated_verify_no_progress(events) is False


def test_no_progress_requires_two_probes():
    """Many varied edits but only ONE probe observation ⇒ no trip (a single
    outcome is not a RECURRING symptom)."""
    events = [user_msg("go"), _edit(0), _edit(1), _edit(2), _edit(3)]
    events += _probe()
    assert repeated_verify_no_progress(events) is False


def test_no_progress_identical_edits_are_not_distinct():
    """Byte-identical edits are pattern-1's job, NOT this detector's — they must
    NOT be double-counted as distinct varied edits, so an identical-edit loop
    against a stable outcome does NOT trip here."""
    events = [user_msg("go")]
    for _ in range(4):
        events.append(action(thought="same", tool="file_edit", args={"path": "a", "patch": "x"}))
        events += _probe()
    assert repeated_verify_no_progress(events) is False


def test_no_progress_resets_on_user_message():
    """A new USER instruction is a new goal — the pre-message symptom does not
    count toward the post-message window."""
    events = [user_msg("go")]
    for i in range(4):
        events.append(_edit(i))
        events += _probe()
    events.append(user_msg("new direction"))
    events.append(_edit(99))
    events += _probe()
    assert repeated_verify_no_progress(events) is False


async def test_no_progress_gate_nudges_then_halts():
    """Loop integration via the real Valve gate + store: first trip emits a
    corrective nudge (CONTINUE); a second trip after the model made MORE varied
    edits with the SAME symptom explains and asks (instead of grinding to the ceiling)."""
    loop, store = build_loop(ScriptedAgent([finish_step()]))
    await store.append(CID, user_msg("build the app"))
    for i in range(4):
        await store.append(CID, _edit(i))
        for e in _probe():
            await store.append(CID, e)

    events = await store.get_events(CID)
    disp = await loop._valve.gate_no_progress(events)
    assert disp is Disp.CONTINUE  # first trip → nudge, not halt
    events = await store.get_events(CID)
    assert any(
        isinstance(e, StatusEvent) and e.detail == "no_progress" for e in events
    ), "first trip must drop a no_progress marker"
    assert any(
        isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and "outcome has NOT changed" in (e.message.content or "")
        for e in events
    ), "first trip must inject the corrective reminder"

    # The model acts again (more varied edits) but the symptom is unchanged.
    await store.append(CID, _edit(5))
    for e in _probe():
        await store.append(CID, e)
    events = await store.get_events(CID)
    disp = await loop._valve.gate_no_progress(events)
    assert disp is Disp.HALT
    events = await store.get_events(CID)
    assert_blocked_question_landing(events, legacy_detail="no_progress")


async def test_no_progress_gate_silent_on_genuine_progress():
    """The gate must NOT fire when the outcome is changing (real progress)."""
    loop, store = build_loop(ScriptedAgent([finish_step()]))
    await store.append(CID, user_msg("build it"))
    for i in range(4):
        await store.append(CID, _edit(i))
        for e in _probe(f"render #{i}"):  # outcome changes every edit
            await store.append(CID, e)
    events = await store.get_events(CID)
    disp = await loop._valve.gate_no_progress(events)
    assert disp is Disp.FALLTHROUGH
    assert not any(
        isinstance(e, StatusEvent) and e.detail == "no_progress"
        for e in await store.get_events(CID)
    )


def test_f6_per_file_rewrite_directive_failed_observation_also_counts_assist_on():
    """F6 — a non-success ObservationEvent on a file-mutating tool ALSO counts
    as a failure (the tool returned success=False). The detector covers both
    shapes: a tool that raised (paired AgentErrorEvent) AND a tool that
    returned a failure result (paired ObservationEvent with success=False)."""
    d = StuckDetector(
        StuckThresholds(per_file_rewrite_failures=3, per_file_rewrite_min_attempts=3),
        assist=True,
    )
    out = []
    for i in range(3):
        a = action(
            thought="write " + str(i),
            tool="file_write",
            args={"path": "a/x.py", "content": "v" + str(i)},
        )
        out.append(a)
        out.append(observation(action_id=a.id, content="ERROR: write refused", success=False))
    result = d.evaluate(out)
    assert result.rewrite_directive is not None
    assert result.rewrite_directive.path == "a/x.py"
    assert result.rewrite_directive.failures == 3
    assert result.rewrite_directive.attempts == 3


# ---- Order A wiring test: weak model_policy threads assist=True into StuckDetector ----
#
# Previously `AgentLoop.__init__` always constructed `StuckDetector(thresholds)`
# WITHOUT passing `assist`, so the detector defaulted to assist=False even when
# the loop's `_assist` flag was later flipped to True. The result: the F6
# patch-spiral directive was BROKEN-CLOSED — it never fired from a real loop run.
#
# After Order A the constructor calls `StuckDetector(thresholds, assist=model_policy.assist)`,
# so a weak `model_policy` (assist=True) propagates into the detector's _assist gate.
# This test verifies that end-to-end wiring: the loop's stuck detector must fire
# the F6 directive when the loop was built with a weak model_policy.


def test_f6_loop_wiring_weak_model_policy_enables_rewrite_directive():
    """Order A acceptance: a weak model_policy passed to AgentLoop must make the
    loop's StuckDetector emit the F6 rewrite_directive for a patch-spiraling file.

    This was BROKEN before Order A because AgentLoop always passed assist=False
    (the default) to StuckDetector regardless of its own _assist value. The fix:
    `StuckDetector(stuck_thresholds, assist=model_policy.assist)`.
    """
    from disco.core import NoOpCondenser, SqliteEventStore
    from disco.core.llm import OperatingMode
    from disco.core.loop import NeverConfirm
    from disco.core.loop.engine import AgentLoop
    from loop_fakes import FakeAnalyzer, FakeExecutor, FakeSummarizer

    _TIGHT = StuckThresholds(per_file_rewrite_failures=2, per_file_rewrite_min_attempts=2)

    loop = AgentLoop(
        "conv",
        SqliteEventStore(":memory:"),
        ScriptedAgent([]),  # never stepped — we inspect _stuck directly
        FakeExecutor(),
        None,
        FakeAnalyzer(),
        NeverConfirm(),
        NoOpCondenser(),
        FakeSummarizer(),
        mode=OperatingMode.LONG_HORIZON,
        model_policy=ModelExecutionPolicy(tier="weak"),
        stuck_thresholds=_TIGHT,
    )

    # Build a spiral: 2 failed patches on the same file (threshold = 2,2).
    events = _failed_patch("src/app.py", 2)
    result = loop._stuck.evaluate(events)

    assert result.rewrite_directive is not None, (
        "StuckDetector inside AgentLoop(model_policy=ModelExecutionPolicy(tier='weak')) "
        "must fire the F6 rewrite_directive — the loop wiring was broken before Order A "
        "(assist defaulted to False regardless of _assist). "
        f"events={[type(e).__name__ for e in events]}"
    )
    assert result.rewrite_directive.path == "src/app.py"


def test_f6_loop_wiring_standard_model_policy_suppresses_rewrite_directive():
    """Order A acceptance (inverse): a standard model_policy must NOT make the
    StuckDetector emit an F6 directive — the gate must stay closed for capable models."""
    from disco.core import NoOpCondenser, SqliteEventStore
    from disco.core.llm import OperatingMode
    from disco.core.loop import NeverConfirm
    from disco.core.loop.engine import AgentLoop
    from loop_fakes import FakeAnalyzer, FakeExecutor, FakeSummarizer

    _TIGHT = StuckThresholds(per_file_rewrite_failures=2, per_file_rewrite_min_attempts=2)

    loop = AgentLoop(
        "conv",
        SqliteEventStore(":memory:"),
        ScriptedAgent([]),
        FakeExecutor(),
        None,
        FakeAnalyzer(),
        NeverConfirm(),
        NoOpCondenser(),
        FakeSummarizer(),
        mode=OperatingMode.LONG_HORIZON,
        model_policy=ModelExecutionPolicy.standard(),
        stuck_thresholds=_TIGHT,
    )

    events = _failed_patch("src/app.py", 2)
    result = loop._stuck.evaluate(events)

    assert result.rewrite_directive is None, (
        "StuckDetector inside AgentLoop(model_policy=ModelExecutionPolicy.standard()) "
        "must NOT fire the F6 directive — the gate is closed for capable models. "
        f"Got: {result.rewrite_directive!r}"
    )


def test_plan_done_and_verified_discriminator():
    """dt3 autopsy: unchanged verify outcome is DONE, not stuck, when every plan
    step is done and the last verify PASSED — the gate must hint finish instead
    of halting STUCK."""
    from disco.core.events import (
        EventSource,
        ObservationEvent,
        PlanEvent,
        PlanStep,
        StatusEvent,
        ToolResult,
    )
    from disco.core.loop.turn_control import _plan_done_and_verified

    plan = PlanEvent(
        source=EventSource.AGENT,
        summary="s",
        steps=[PlanStep(title="a"), PlanStep(title="b")],
    )
    def obs(content: str) -> ObservationEvent:
        return ObservationEvent(
            source=EventSource.ENVIRONMENT,
            action_id="a1",
            tool_result=ToolResult(
                call_id="c1", tool_name="verify_web_app", success=True, content=content
            ),
        )
    from disco.core.events import ActionEvent, ToolCall

    done = [
        ActionEvent(
            source=EventSource.AGENT,
            thought="",
            tool_call=ToolCall(
                tool_name="update_plan_progress",
                arguments={"steps": [
                    {"index": 1, "state": "done"},
                    {"index": 2, "state": "done"},
                ]},
                call_id="p1",
            ),
        )
    ]
    # all steps done + PASS → True
    assert _plan_done_and_verified([plan, *done, obs("VERIFY_WEB_APP: PASS (pass)")])
    # FAIL verify → False
    assert not _plan_done_and_verified([plan, *done, obs("VERIFY_WEB_APP: FAIL (broken)")])
    # steps not done → False
    assert not _plan_done_and_verified([plan, obs("VERIFY_WEB_APP: PASS (pass)")])
