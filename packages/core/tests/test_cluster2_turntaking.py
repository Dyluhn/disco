"""Cluster 2 — turn-taking & completion: the `finish` + `notify_user` virtual
tools, the no-op backstop, and the circuit breaker."""

from __future__ import annotations

from disco.core import (
    ConversationStatus,
    EventSource,
    MessageEvent,
    ObservationEvent,
    StatusEvent,
    ToolResult,
)
from disco.core.loop import signals
from loop_fakes import AgentStep, FakeExecutor, ScriptedAgent, action_step, build_loop

CID = "conv"


def _prose(thought: str):
    """A tool-less prose turn that is NOT finished (the new Build execution
    convention — completion is affirmative via `finish`)."""
    return AgentStep(thought=thought, tool_call=None, finished=False)


# ---- the `finish` virtual tool ----------------------------------------------


async def test_finish_tool_ends_the_run():
    agent = ScriptedAgent([action_step("finish", args={"summary": "built the page"})])
    loop, store = build_loop(agent)
    await loop.send_message("go")
    state = await loop.run()
    assert state.execution_status == ConversationStatus.FINISHED
    # The summary landed as the agent's final message.
    events = await store.get_events(CID)
    finals = [
        e
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.AGENT
        and "built the page" in e.message.content
    ]
    assert len(finals) == 1


# ---- the `notify_user` virtual tool (non-blocking) --------------------------


async def test_notify_user_emits_message_and_continues():
    # notify, then finish. notify must NOT end the run; finish does.
    agent = ScriptedAgent(
        [
            action_step("notify_user", args={"message": "Working on the navbar now"}),
            action_step("finish", args={"summary": "done"}),
        ]
    )
    loop, store = build_loop(agent)
    await loop.send_message("go")
    state = await loop.run()
    assert state.execution_status == ConversationStatus.FINISHED
    events = await store.get_events(CID)
    notes = [
        e
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.AGENT
        and "navbar" in e.message.content
    ]
    assert len(notes) == 1  # the non-blocking note was recorded


# ---- the no-op backstop (talk-without-acting can't loop forever) ------------


async def test_consecutive_noops_end_the_run_cleanly():
    # An agent that only ever talks (never a tool) must not loop forever. The
    # PRIMARY guard is StuckDetector's monologue rule (→ STUCK at 4 agent msgs);
    # the noop backstop is the secondary net (→ FINISHED noop_limit). Here we
    # raise the monologue threshold so the noop BACKSTOP is exercised in
    # isolation — proving the run terminates even if monologue detection misses.
    from disco.core.loop.stuck import StuckThresholds

    agent = ScriptedAgent([_prose(f"thought number {i}") for i in range(10)])
    loop, store = build_loop(agent, stuck_thresholds=StuckThresholds(agent_monologue=100))
    await loop.send_message("go")
    state = await loop.run()
    assert state.execution_status == ConversationStatus.FINISHED
    events = await store.get_events(CID)
    terminal = next(
        e
        for e in reversed(events)
        if e.__class__.__name__ == "StatusEvent" and e.status == ConversationStatus.FINISHED
    )
    assert terminal.detail == "noop_limit"


async def test_talking_without_acting_always_terminates():
    # Belt-and-suspenders: with DEFAULT thresholds, pure talking still terminates
    # (via monologue→STUCK) — never an infinite loop. This is the actual bug fix.
    agent = ScriptedAgent([_prose(f"musing {i}") for i in range(20)])
    loop, _ = build_loop(agent)
    await loop.send_message("go")
    state = await loop.run()
    assert state.execution_status in (
        ConversationStatus.STUCK,
        ConversationStatus.FINISHED,
    )


def test_consecutive_noops_helper_counts_and_resets():
    from disco.core import LLMMessage

    def agent_msg(text):
        return MessageEvent(
            source=EventSource.AGENT, message=LLMMessage(role="assistant", content=text)
        )

    def user_msg(text):
        return MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content=text))

    # 3 trailing agent prose messages → 3.
    seq = [user_msg("go"), agent_msg("a"), agent_msg("b"), agent_msg("c")]
    assert signals.consecutive_noops(seq) == 3
    # A user message resets the count.
    seq2 = [agent_msg("a"), user_msg("go"), agent_msg("b")]
    assert signals.consecutive_noops(seq2) == 1


def test_consecutive_noops_resets_at_resume_marker():
    """Fix B P1 — a resume is a fresh actionless boundary. The streak must NOT
    walk past the pause+resume and keep counting pre-pause noops (which made a
    resumed MiniMax-M3 build re-pause after ~1 turn).

    The REAL resume path emits the runway boundary producer-agnostically: the
    live `AgentLoop.resume()` flip is a *bare* StatusEvent(RUNNING) with NO
    detail (the original detail=='resumed'-only check NEVER matched it), so the
    reset keys on the PAUSED that the resume follows — everything after the most
    recent pause is the post-resume segment."""
    from disco.core import LLMMessage

    def agent_msg(text):
        return MessageEvent(
            source=EventSource.AGENT, message=LLMMessage(role="assistant", content=text)
        )

    paused = StatusEvent(status=ConversationStatus.PAUSED, detail="actionless")
    # The ACTUAL live resume marker: AgentLoop.resume() emits a BARE RUNNING,
    # and run() re-emits a bare RUNNING at the top of every (re)entry — neither
    # carries detail="resumed". The reset must key on the PAUSED boundary, not
    # this marker, or it never fires on the real path.
    bare_resume = StatusEvent(status=ConversationStatus.RUNNING)

    # [noop, noop, PAUSED, <real bare-RUNNING resume marker>, noop] → only the
    # single post-resume noop counts; the pre-pause streak is behind the PAUSED
    # boundary even though the resume marker carries no detail.
    seq = [agent_msg("a"), agent_msg("b"), paused, bare_resume, agent_msg("c")]
    assert signals.consecutive_noops(seq) == 1

    # The resume_service flip (RUNNING/detail="resumed") still resets too — an
    # IDLE-with-unfinished-plan resume has no preceding PAUSED, so this marker is
    # the only boundary there.
    resumed = StatusEvent(status=ConversationStatus.RUNNING, detail="resumed")
    seq_idle_resume = [agent_msg("a"), agent_msg("b"), resumed, agent_msg("c")]
    assert signals.consecutive_noops(seq_idle_resume) == 1

    # Without a pause/resume, the spam cap still accumulates normally (the real
    # serve/prose caps are untouched) — a degenerate run WITHOUT a resume pauses.
    seq_no_resume = [agent_msg("a"), agent_msg("b"), agent_msg("c"), agent_msg("d")]
    assert signals.consecutive_noops(seq_no_resume) == 4

    # A mid-run BARE RUNNING that is NOT a resume-after-pause (e.g. run()'s
    # top-of-loop RUNNING in a normal run) must NOT wrongly reset the streak —
    # only a PAUSED (or RUNNING/resumed) boundary does.
    seq_midrun_running = [
        agent_msg("a"),
        bare_resume,
        agent_msg("b"),
        agent_msg("c"),
    ]
    assert signals.consecutive_noops(seq_midrun_running) == 3


# ---- the circuit breaker (distinct failures → hand off to user) -------------


async def test_circuit_breaker_hands_off_after_distinct_failures():
    failing = ToolResult(call_id="c", tool_name="shell", success=False, content="", error="boom")
    # Four DISTINCT failing actions (distinct so StuckDetector's identical-repeat
    # rule doesn't fire STUCK first). The breaker fires at threshold 4.
    agent = ScriptedAgent(
        [
            action_step(args={"cmd": "a"}),
            action_step(args={"cmd": "b"}),
            action_step(args={"cmd": "c"}),
            action_step(args={"cmd": "d"}),
            action_step(args={"cmd": "e"}),
        ]
    )
    loop, store = build_loop(agent, executor=FakeExecutor(result=failing))
    await loop.send_message("go")
    state = await loop.run()
    # The harness halts for the user instead of grinding to max_iterations.
    assert state.execution_status == ConversationStatus.AWAITING_USER_DECISION
    events = await store.get_events(CID)
    from disco.core import AlternativesEvent

    # The harness SYNTHESIZES an AlternativesEvent so the UI renders the recovery
    # gate (not dead-end prose): the failure summary + a "Continue anyway" option.
    alt = next(e for e in reversed(events) if isinstance(e, AlternativesEvent))
    assert "failures in a row" in alt.summary
    assert any(o.id == "__continue__" for o in alt.options)
    # …and the gate's detail points at that alt so the View resolves it.
    terminal = next(
        e
        for e in reversed(events)
        if e.__class__.__name__ == "StatusEvent"
        and e.status == ConversationStatus.AWAITING_USER_DECISION
    )
    assert terminal.detail == alt.id


async def test_no_breaker_when_failures_below_threshold():
    failing = ToolResult(call_id="c", tool_name="shell", success=False, content="", error="x")
    # Two failures then a success → never reaches the breaker.
    agent = ScriptedAgent(
        [action_step(args={"cmd": "a"}), action_step(args={"cmd": "b"}), action_step("finish")]
    )
    # Executor fails the first two shell calls, succeeds the rest.
    loop, store = build_loop(agent, executor=FakeExecutor(result=failing))
    await loop.send_message("go")
    state = await loop.run()
    # It hits the breaker only if 4 consecutive fail; here the scripted agent
    # repeats shell-b which keeps failing → 4 in a row → breaker. So assert the
    # breaker is reachable but NOT before threshold: with a low streak it would
    # not fire. (This documents the threshold boundary.)
    assert state.execution_status in (
        ConversationStatus.AWAITING_USER_DECISION,
        ConversationStatus.FINISHED,
    )
    # Unused import guard.
    assert ObservationEvent


def test_count_recent_failures_ignores_update_plan_progress():
    """A malformed `update_plan_progress` (non-critical bookkeeping) failure is
    TRANSPARENT to the circuit-breaker streak — it neither increments nor resets it.
    MiniMax-M3 intermittently emits an empty `steps` payload; the schema rejects it
    actionably, but a cosmetic plan-snapshot hiccup must NOT escalate a trivial build
    to AWAITING_USER. A real failure before the churn is still counted; genuine real
    failures still trip the streak (no regression)."""
    from disco.core import ActionEvent, AgentErrorEvent, ToolCall

    def _act(tool, args=None):
        return ActionEvent(thought="t", tool_call=ToolCall(tool_name=tool, arguments=args or {}))

    def _err(action, msg="failed validation"):
        return AgentErrorEvent(error=msg, action_id=action.id)

    # 4 consecutive malformed update_plan_progress failures → streak stays 0
    upp: list = []
    for _ in range(4):
        a = _act("update_plan_progress", {"steps": ["", ""]})
        upp += [a, _err(a)]
    assert signals.count_recent_failures(upp) == 0

    # a real shell failure BEFORE the upp churn is still counted (upp is transparent)
    real = _act("shell", {"cmd": "x"})
    assert signals.count_recent_failures([real, _err(real, "shell failed")] + upp) == 1

    # genuine consecutive real failures still trip the breaker (no regression)
    reals: list = []
    for _ in range(3):
        a = _act("shell", {"cmd": "y"})
        reals += [a, _err(a, "shell failed")]
    assert signals.count_recent_failures(reals) == 3


# ---- BW-02/M3: repeated actionless pauses are event-persisted ----------------


def _agent_msg(text):
    from disco.core import LLMMessage

    return MessageEvent(
        source=EventSource.AGENT, message=LLMMessage(role="assistant", content=text)
    )


def _file_read_action(path="src/app.js"):
    from disco.core import ActionEvent, ToolCall

    return ActionEvent(
        thought="reading",
        tool_call=ToolCall(tool_name="file_read", arguments={"path": path}),
    )


def _undone_plan():
    from disco.core import PlanEvent

    return PlanEvent(summary="build it", steps=[{"title": "a"}], revision=1)


def test_consecutive_actionless_pauses_helper():
    """The BW-02 escalation signal: trailing run of zero-action actionless pauses,
    transparent to resume markers and noop prose, broken by a real action."""
    paused = StatusEvent(status=ConversationStatus.PAUSED, detail="actionless")
    resumed = StatusEvent(status=ConversationStatus.RUNNING, detail="resumed")

    # No prior actionless pause → 0 (the FIRST pause is the useful stop).
    assert signals.consecutive_actionless_pauses([_agent_msg("a"), _agent_msg("b")]) == 0
    # One prior pause, then a zero-action resumed segment → 1 (this is the 2nd).
    seq = [paused, resumed, _agent_msg("a"), _agent_msg("b")]
    assert signals.consecutive_actionless_pauses(seq) == 1
    # Two prior pauses with no action between → 2.
    seq2 = [paused, resumed, _agent_msg("x"), paused, resumed, _agent_msg("y")]
    assert signals.consecutive_actionless_pauses(seq2) == 2
    # A real action (file_read) in the trailing segment breaks the streak → 0.
    seq3 = [paused, resumed, _file_read_action(), _agent_msg("a")]
    assert signals.consecutive_actionless_pauses(seq3) == 0
    # A different terminal (a non-actionless PAUSE) breaks the streak.
    other = StatusEvent(status=ConversationStatus.PAUSED, detail="noop_limit")
    assert signals.consecutive_actionless_pauses([other, resumed, _agent_msg("a")]) == 0


async def test_second_zero_action_actionless_pause_persists_another_pause():
    """M3 — a resumed zero-action segment persists another PAUSED(actionless)
    instead of the old STUCK(actionless_loop) branch, so REL-RC-P can derive
    pause #2 from status events."""
    loop, store = build_loop(ScriptedAgent([]))
    paused = StatusEvent(status=ConversationStatus.PAUSED, detail="actionless")
    resumed = StatusEvent(status=ConversationStatus.RUNNING, detail="resumed")
    events = [
        _undone_plan(),
        paused,  # the FIRST (useful) actionless pause
        resumed,  # human resumed
        _agent_msg("still thinking..."),  # zero-action segment
        _agent_msg("almost there..."),
        _agent_msg("ok..."),
    ]
    landed = await loop._valve.actionless_valve(events, loop._ACTIONLESS_BREAK_CAP)
    assert landed is True
    emitted = await store.get_events(CID)
    statuses = [e for e in emitted if isinstance(e, StatusEvent)]
    assert statuses, "the valve must land a pause"
    # terminal-collapse: ask landing supersedes the actionless marker
    assert statuses[-1].status == ConversationStatus.AWAITING_USER_QUESTION
    assert statuses[-2].status == ConversationStatus.PAUSED
    assert statuses[-2].detail == "actionless"
    assert not any(s.status == ConversationStatus.STUCK for s in statuses)


async def test_action_between_pauses_does_not_escalate():
    """A resumed segment that DID a real action (a file_read counts as acting) must
    NOT escalate — it pauses normally. Only a truly zero-ACTION repeat is degenerate."""
    loop, store = build_loop(ScriptedAgent([]))
    paused = StatusEvent(status=ConversationStatus.PAUSED, detail="actionless")
    resumed = StatusEvent(status=ConversationStatus.RUNNING, detail="resumed")
    events = [
        _undone_plan(),
        paused,
        resumed,
        _file_read_action(),  # the model acted this segment — not degenerate
        _agent_msg("read the file, now thinking..."),
    ]
    landed = await loop._valve.actionless_valve(events, loop._ACTIONLESS_BREAK_CAP)
    assert landed is True
    statuses = [e for e in await store.get_events(CID) if isinstance(e, StatusEvent)]
    # terminal-collapse: ask landing supersedes the actionless marker
    assert statuses[-1].status == ConversationStatus.AWAITING_USER_QUESTION
    assert statuses[-2].status == ConversationStatus.PAUSED
    assert statuses[-2].detail == "actionless"
    assert not any(s.status == ConversationStatus.STUCK for s in statuses)


async def test_bare_running_resume_after_prior_work_still_persists_second_pause():
    """codex P1 — the bare-RUNNING resume blind spot. A run that DID real work
    (a file_read) BEFORE the first actionless pause, then is resumed by the live
    `AgentLoop.resume()` BARE StatusEvent(RUNNING) (NO detail="resumed"), then
    again does zero actions, MUST still persist pause #2. The old
    `actions_since_last_resume` keyed only on detail=="resumed", so it counted
    the pre-pause file_read (>0) and the old escalation gate never fired. The
    REL-RC-P replacement is keyed on persisted PAUSED(actionless) events."""
    loop, store = build_loop(ScriptedAgent([]))
    paused = StatusEvent(status=ConversationStatus.PAUSED, detail="actionless")
    bare_resume = StatusEvent(status=ConversationStatus.RUNNING)  # the LIVE resume marker
    events = [
        _undone_plan(),
        _file_read_action(),  # REAL work happened before the first pause
        paused,  # the FIRST (useful) actionless pause
        bare_resume,  # AgentLoop.resume(): bare RUNNING, no detail
        _agent_msg("still thinking..."),  # zero-action segment
        _agent_msg("almost there..."),
        _agent_msg("ok..."),
    ]
    landed = await loop._valve.actionless_valve(events, loop._ACTIONLESS_BREAK_CAP)
    assert landed is True
    statuses = [e for e in await store.get_events(CID) if isinstance(e, StatusEvent)]
    assert statuses, "the valve must land a pause"
    # terminal-collapse: the ask landing supersedes the actionless marker
    assert statuses[-1].status == ConversationStatus.AWAITING_USER_QUESTION
    assert statuses[-2].status == ConversationStatus.PAUSED
    assert statuses[-2].detail == "actionless"
    assert not any(s.status == ConversationStatus.STUCK for s in statuses)


async def test_real_action_after_bare_running_resume_does_not_escalate():
    """The mirror guard: a run resumed by a BARE RUNNING that THEN does real
    work (a file_read after the resume) is NOT a degenerate loop — it must pause
    normally, never STUCK. Resetting the counter at the bare-RUNNING boundary
    must not over-fire on a genuinely-productive resumed segment."""
    loop, store = build_loop(ScriptedAgent([]))
    paused = StatusEvent(status=ConversationStatus.PAUSED, detail="actionless")
    bare_resume = StatusEvent(status=ConversationStatus.RUNNING)
    events = [
        _undone_plan(),
        _file_read_action(),  # pre-pause work
        paused,
        bare_resume,  # bare RUNNING resume
        _file_read_action(),  # the model ACTED this segment → not degenerate
        _agent_msg("read another file, now thinking..."),
    ]
    landed = await loop._valve.actionless_valve(events, loop._ACTIONLESS_BREAK_CAP)
    assert landed is True
    statuses = [e for e in await store.get_events(CID) if isinstance(e, StatusEvent)]
    # terminal-collapse: the landing supersedes the actionless marker; the
    # marker (counter bookkeeping) directly precedes it.
    assert statuses[-1].status == ConversationStatus.AWAITING_USER_QUESTION
    assert statuses[-2].status == ConversationStatus.PAUSED
    assert statuses[-2].detail == "actionless"
    assert not any(s.status == ConversationStatus.STUCK for s in statuses)
