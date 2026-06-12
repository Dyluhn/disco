import pytest
from disco.core import (
    ActionEvent,
    ConversationStatus,
    DeliverableEvent,
    EventSource,
    KnowledgeEvent,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    PlanEvent,
    SqliteEventStore,
    StatusEvent,
)
from disco.core.llm import LLMTransientError, OperatingMode
from loop_fakes import ScriptedAgent, action_step, build_loop, finish_step

CID = "conv"


def noop_step(thought="just talking"):
    from disco.core.loop import AgentStep
    return AgentStep(thought=thought, tool_call=None, finished=False)


def _last_status_detail(events):
    """Return the detail of the last StatusEvent in the event log."""
    for e in reversed(events):
        if isinstance(e, StatusEvent):
            return e.detail
    return None


# ---- actionless-step breaker (DEFECT-4) ----------------------------------------


@pytest.mark.asyncio
async def test_actionless_breaker_halts():
    """3 no-tool-call steps with an incomplete plan → PAUSED actionless."""
    agent = ScriptedAgent([
        action_step("submit_plan", {"summary": "p", "steps": [{"title": "1"}]}),
        noop_step("a"),
        noop_step("b"),
        noop_step("c"),
        finish_step(),
    ])
    loop, store = build_loop(agent)
    loop.mode = OperatingMode.PLANNING
    loop._planning_tools = frozenset(["file_read"])
    await loop.send_message("go")
    await loop.run()
    await loop.approve_plan()

    state = await loop.run()
    assert state.execution_status == ConversationStatus.PAUSED
    events = await store.get_events(CID)
    assert _last_status_detail(events) == "actionless"
    msgs = [
        e for e in events
        if isinstance(e, MessageEvent) and e.source == EventSource.ENVIRONMENT
    ]
    assert any(
        "3 consecutive responses without doing any real work"
        in (m.message.content if m.message else "")
        for m in msgs
    )


@pytest.mark.asyncio
async def test_actionless_breaker_reset():
    """2 noops + real action + 2 more noops + real action → breaker never fires."""
    agent = ScriptedAgent([
        action_step("submit_plan", {"summary": "p", "steps": [{"title": "1"}]}),
        noop_step("a"),
        noop_step("b"),
        action_step("shell", {}),   # resets the breaker
        noop_step("c"),
        noop_step("d"),
        action_step("shell", {}),
        finish_step(),
    ])
    loop, store = build_loop(agent)
    loop.mode = OperatingMode.PLANNING
    loop._planning_tools = frozenset(["file_read"])
    await loop.send_message("go")
    await loop.run()
    await loop.approve_plan()

    state = await loop.run()
    # Plan has 1 step not done; auto-continue fires (shell = productive action),
    # eventually landing FINISHED partial_plan.
    assert state.execution_status == ConversationStatus.FINISHED
    events = await store.get_events(CID)
    assert _last_status_detail(events) == "partial_plan"


@pytest.mark.asyncio
async def test_actionless_breaker_inert_when_plan_complete():
    """Breaker must not fire when the plan is already complete."""
    agent = ScriptedAgent([
        action_step("submit_plan", {"summary": "p", "steps": [{"title": "1"}]}),
        action_step("plan_step", {"index": 1, "state": "done"}),
        noop_step("a"),
        noop_step("b"),
        noop_step("c"),
        noop_step("d"),
        noop_step("e"),
        noop_step("f"),   # _max_consecutive_noops = 6
        finish_step(),
    ])
    loop, store = build_loop(agent)
    loop.mode = OperatingMode.PLANNING
    loop._planning_tools = frozenset(["file_read"])
    await loop.send_message("go")
    await loop.run()
    await loop.approve_plan()

    state = await loop.run()
    assert state.execution_status == ConversationStatus.FINISHED
    events = await store.get_events(CID)
    assert _last_status_detail(events) == "noop_limit"


# ---- valve taxonomy: partial-plan landings (DEFECT-4) --------------------------


@pytest.mark.asyncio
async def test_valve_auto_continue_zero_actions_pauses():
    """Auto-continue cap hit with zero real actions in the run segment → PAUSED partial_plan."""
    store = SqliteEventStore(":memory:")
    await store.append(
        CID,
        MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content="go")),
    )
    await store.append(CID, PlanEvent(summary="p", steps=[{"title": "1"}], revision=1))
    await store.append(
        CID, StatusEvent(status=ConversationStatus.RUNNING, detail="plan_approved")
    )
    # No non-bookkeeping ActionEvents → actions_since_last_resume == 0.

    agent = ScriptedAgent([finish_step()])   # repeats forever
    loop, _ = build_loop(agent, store=store)
    loop._auto_continue_cap = 3

    state = await loop.run()
    assert state.execution_status == ConversationStatus.PAUSED
    events = await store.get_events(CID)
    assert _last_status_detail(events) == "partial_plan"
    msgs = [
        e for e in events
        if isinstance(e, MessageEvent) and e.source == EventSource.ENVIRONMENT
    ]
    assert any(
        "finishing was blocked" in (m.message.content if m.message else "")
        for m in msgs
    )


@pytest.mark.asyncio
async def test_valve_auto_continue_with_actions_finishes():
    """Auto-continue cap hit WITH real actions in the run segment → FINISHED partial_plan."""
    store = SqliteEventStore(":memory:")
    await store.append(
        CID,
        MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content="go")),
    )
    await store.append(CID, PlanEvent(summary="p", steps=[{"title": "1"}], revision=1))
    await store.append(
        CID, StatusEvent(status=ConversationStatus.RUNNING, detail="plan_approved")
    )

    # Shell action first (productive, counts as actions_since), then finish repeats.
    agent = ScriptedAgent([action_step("shell", {}), finish_step()])
    loop, _ = build_loop(agent, store=store)
    loop._auto_continue_cap = 3

    state = await loop.run()
    assert state.execution_status == ConversationStatus.FINISHED
    events = await store.get_events(CID)
    assert _last_status_detail(events) == "partial_plan"


# ---- knowledge dedup (×179 CSV bloat) ------------------------------------------


@pytest.mark.asyncio
async def test_dedup_remember():
    """Same fact × multiple calls → exactly 1 KnowledgeEvent; dups get 'Already recorded'."""
    agent = ScriptedAgent([
        # Real work first — the fresh-session backstop refuses a zero-work remember.
        action_step("shell", {}),
        action_step("remember", {"fact": "foo", "scope": "bar"}),
        action_step("remember", {"fact": "foo", "scope": "bar"}),       # dup
        action_step("remember", {"fact": "foo  ", "scope": "bar"}),     # dup (trailing space)
        action_step("remember", {"fact": "foo2", "scope": "bar"}),      # new fact
        action_step("remember", {"fact": "foo", "scope": "baz"}),       # new scope
        finish_step(),
    ])
    loop, store = build_loop(agent)
    await loop.send_message("go")
    await loop.run()

    events = await store.get_events(CID)
    knowledges = [e for e in events if isinstance(e, KnowledgeEvent)]
    assert len(knowledges) == 3   # foo/bar, foo2/bar, foo/baz

    obs = [
        e for e in events
        if isinstance(e, ObservationEvent) and e.tool_result.tool_name == "remember"
    ]
    assert len(obs) == 2          # the two duplicate calls
    assert "Already recorded" in obs[0].tool_result.content


# ---- DEFECT-5: LLMTransientError retry + PAUSED, never ERROR -------------------


@pytest.mark.asyncio
async def test_transient_error_retry_succeeds(monkeypatch):
    """2 transient errors then success → step completes, delays == (10.0, 30.0)."""
    import disco.core.loop.engine as engine_module

    delays = []

    async def mock_sleep(d):
        delays.append(d)

    monkeypatch.setattr(engine_module, "_sleep", mock_sleep)

    # Indices 0 and 1 raise; index 2 returns the shell action; index 3 finishes.
    agent = ScriptedAgent([
        LLMTransientError("unavailable"),
        LLMTransientError("unavailable"),
        action_step("shell", {}),
        finish_step(),
    ])
    loop, store = build_loop(agent)
    await loop.send_message("go")
    await loop.run()

    assert delays == [10.0, 30.0]


@pytest.mark.asyncio
async def test_transient_error_persistent_pauses(monkeypatch):
    """Persistent LLMTransientError → PAUSED driver-unavailable, 4 attempts, no ErrorEvent."""
    import disco.core.loop.engine as engine_module

    async def mock_sleep(_d):
        pass

    monkeypatch.setattr(engine_module, "_sleep", mock_sleep)

    # Single-item list: ScriptedAgent repeats it on every call (min(i, 0) == 0).
    agent = ScriptedAgent([LLMTransientError("unavailable")])
    loop, store = build_loop(agent)
    await loop.send_message("go")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.PAUSED
    events = await store.get_events(CID)
    assert _last_status_detail(events) == "driver-unavailable"
    assert agent.calls == 4   # 1 initial + 3 retries

    errors = [e for e in events if getattr(e, "kind", None) == "error"]
    assert len(errors) == 0


# ---- null-payload deliverable guard --------------------------------------------


@pytest.mark.asyncio
async def test_deliverable_guard():
    """Empty serve payload → no DeliverableEvent; non-empty → one event."""
    agent = ScriptedAgent([
        action_step("shell", {}),   # real work first — serve gate requires it
        action_step("serve", {}),                           # empty → skipped
        action_step("serve", {"title": "x", "path": "y"}), # real deliverable
        finish_step(),
    ])
    loop, store = build_loop(agent)
    await loop.send_message("go")
    await loop.run()

    events = await store.get_events(CID)
    deliverables = [e for e in events if isinstance(e, DeliverableEvent)]
    assert len(deliverables) == 1
    assert deliverables[0].title == "x"


# ---- intercept bypass holes (Phase-B re-run defect, 2026-06-10) -----------------
#
# The non-blocking intercepts (notify_user / remember / serve) used to end with
# a bare `continue`, skipping ALL actionless bookkeeping — so a model spamming
# them post-resume burned ~50 DeliverableEvents + ~20 prose messages before the
# stuck detector (95 events later) stopped it. Each intercept now feeds the
# shared `_actionless_valve`, and log-invisible steps (empty payloads,
# duplicates) are carried by an instance counter.


async def _approved_plan_loop(agent):
    """Build a loop with an approved 1-step plan (incomplete) — the
    cap-3 actionless breaker is armed in this state."""
    loop, store = build_loop(agent)
    loop.mode = OperatingMode.PLANNING
    loop._planning_tools = frozenset(["file_read"])
    await loop.send_message("go")
    await loop.run()
    await loop.approve_plan()
    return loop, store


@pytest.mark.asyncio
async def test_serve_spam_trips_valve():
    """Distinct serve calls with no real action in between → PAUSED actionless
    at the cap (3 DeliverableEvents max), not a 50-event spam run."""
    agent = ScriptedAgent([
        action_step("submit_plan", {"summary": "p", "steps": [{"title": "1"}]}),
        action_step("shell", {}),   # real work first — serve gate requires it
        action_step("serve", {"title": "a", "path": "pa"}),
        action_step("serve", {"title": "b", "path": "pb"}),
        action_step("serve", {"title": "c", "path": "pc"}),
        action_step("serve", {"title": "d", "path": "pd"}),  # must never run
        finish_step(),
    ])
    loop, store = await _approved_plan_loop(agent)

    state = await loop.run()
    assert state.execution_status == ConversationStatus.PAUSED
    events = await store.get_events(CID)
    assert _last_status_detail(events) == "actionless"
    deliverables = [e for e in events if isinstance(e, DeliverableEvent)]
    assert len(deliverables) == 3


@pytest.mark.asyncio
async def test_duplicate_serve_suppressed_and_trips_valve():
    """Re-serving the same (path, kind) → ONE DeliverableEvent total; the
    invisible-step counter still trips the valve."""
    agent = ScriptedAgent([
        action_step("submit_plan", {"summary": "p", "steps": [{"title": "1"}]}),
        action_step("shell", {}),   # real work first — serve gate requires it
        action_step("serve", {"title": "app", "path": "p"}),
        action_step("serve", {"title": "app", "path": "p"}),       # dup → invisible
        action_step("serve", {"title": "app again", "path": "p"}),  # dup (title spin) → invisible
        finish_step(),
    ])
    loop, store = await _approved_plan_loop(agent)

    state = await loop.run()
    assert state.execution_status == ConversationStatus.PAUSED
    events = await store.get_events(CID)
    assert _last_status_detail(events) == "actionless"
    deliverables = [e for e in events if isinstance(e, DeliverableEvent)]
    assert len(deliverables) == 1


@pytest.mark.asyncio
async def test_empty_serve_spam_trips_valve():
    """Empty-args serve persists NOTHING to the log — the instance counter is
    the only witness, and it must still trip the valve."""
    agent = ScriptedAgent([
        action_step("submit_plan", {"summary": "p", "steps": [{"title": "1"}]}),
        action_step("serve", {}),
        action_step("serve", {}),
        action_step("serve", {}),
        finish_step(),
    ])
    loop, store = await _approved_plan_loop(agent)

    state = await loop.run()
    assert state.execution_status == ConversationStatus.PAUSED
    events = await store.get_events(CID)
    assert _last_status_detail(events) == "actionless"
    assert not any(isinstance(e, DeliverableEvent) for e in events)


@pytest.mark.asyncio
async def test_duplicate_remember_spam_trips_valve():
    """Duplicate remember calls (the dedup ActionEvent pair) count toward the
    actionless streak instead of resetting it."""
    agent = ScriptedAgent([
        action_step("submit_plan", {"summary": "p", "steps": [{"title": "1"}]}),
        # Real work first — the fresh-session backstop refuses a zero-work remember.
        action_step("shell", {}),
        action_step("remember", {"fact": "foo", "scope": "s"}),   # fresh → neutral
        action_step("remember", {"fact": "foo", "scope": "s"}),   # dup → counts
        action_step("remember", {"fact": "foo", "scope": "s"}),   # dup → counts
        action_step("remember", {"fact": "foo", "scope": "s"}),   # dup → trips cap
        finish_step(),
    ])
    loop, store = await _approved_plan_loop(agent)

    state = await loop.run()
    assert state.execution_status == ConversationStatus.PAUSED
    events = await store.get_events(CID)
    assert _last_status_detail(events) == "actionless"
    knowledges = [e for e in events if isinstance(e, KnowledgeEvent)]
    assert len(knowledges) == 1


@pytest.mark.asyncio
async def test_notify_user_spam_trips_valve():
    """notify_user prose spam (the 'I'm back!' degeneration) → PAUSED
    actionless at the cap — the intercept no longer bypasses the valve."""
    agent = ScriptedAgent([
        action_step("submit_plan", {"summary": "p", "steps": [{"title": "1"}]}),
        action_step("notify_user", {"message": "I'm back after the restart!"}),
        action_step("notify_user", {"message": "Resuming work now!"}),
        action_step("notify_user", {"message": "Picking up where I left off!"}),
        finish_step(),
    ])
    loop, store = await _approved_plan_loop(agent)

    state = await loop.run()
    assert state.execution_status == ConversationStatus.PAUSED
    events = await store.get_events(CID)
    assert _last_status_detail(events) == "actionless"


@pytest.mark.asyncio
async def test_real_action_resets_intercept_streak():
    """Serves interleaved with real actions never trip the valve — the streak
    (event-derived AND invisible counter) resets on a real ActionEvent."""
    agent = ScriptedAgent([
        action_step("submit_plan", {"summary": "p", "steps": [{"title": "1"}]}),
        action_step("shell", {}),   # real work first — serve gate requires it
        action_step("serve", {"title": "a", "path": "pa"}),
        action_step("serve", {"title": "a", "path": "pa"}),  # dup → invisible +1
        action_step("shell", {}),                            # real action → reset
        action_step("serve", {"title": "b", "path": "pb"}),
        action_step("serve", {"title": "c", "path": "pc"}),
        action_step("shell", {}),
        finish_step(),
    ])
    loop, store = await _approved_plan_loop(agent)

    state = await loop.run()
    events = await store.get_events(CID)
    statuses = [e.detail for e in events if isinstance(e, StatusEvent)]
    assert "actionless" not in statuses
    deliverables = [e for e in events if isinstance(e, DeliverableEvent)]
    assert len(deliverables) == 3  # pa, pb, pc — the dup suppressed
    # Lands via the finish path (partial_plan taxonomy), not a valve trip.
    assert state.execution_status in (
        ConversationStatus.FINISHED,
        ConversationStatus.PAUSED,
    )
    assert _last_status_detail(events) == "partial_plan"


# ---- post-resume serve gate (Phase-B re-run #3 defect, 2026-06-10) --------------
#
# Re-run #3 showed a NEW degeneration shape: the model's FIRST post-resume turn
# was serve(path=".") on an empty restored workspace — a handoff with zero work
# behind it. The gate refuses any serve with no real (non-bookkeeping) action
# since the last resume (or since start), with actionable feedback.


@pytest.mark.asyncio
async def test_serve_before_any_work_refused():
    """serve as the first move → refused with the actionable ENVIRONMENT
    message and NO DeliverableEvent; after one real action it goes through."""
    agent = ScriptedAgent([
        action_step("serve", {"title": "app", "path": "."}),  # zero work → refused
        action_step("shell", {}),                              # real work
        action_step("serve", {"title": "app", "path": "."}),  # now allowed
        finish_step(),
    ])
    loop, store = build_loop(agent)
    await loop.send_message("go")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED
    events = await store.get_events(CID)
    deliverables = [e for e in events if isinstance(e, DeliverableEvent)]
    assert len(deliverables) == 1   # only the post-work serve landed
    refusals = [
        e for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and "serve refused" in (e.message.content if e.message else "")
    ]
    assert len(refusals) == 1


@pytest.mark.asyncio
async def test_serve_before_work_spam_trips_valve():
    """Gate-refused serves count as invisible steps — three in a row trips
    the actionless valve, zero DeliverableEvents persisted."""
    agent = ScriptedAgent([
        action_step("submit_plan", {"summary": "p", "steps": [{"title": "1"}]}),
        action_step("serve", {"title": "a", "path": "pa"}),  # refused (no work)
        action_step("serve", {"title": "b", "path": "pb"}),  # refused
        action_step("serve", {"title": "c", "path": "pc"}),  # refused → cap
        finish_step(),
    ])
    loop, store = await _approved_plan_loop(agent)

    state = await loop.run()
    assert state.execution_status == ConversationStatus.PAUSED
    events = await store.get_events(CID)
    assert _last_status_detail(events) == "actionless"
    assert not any(isinstance(e, DeliverableEvent) for e in events)


def test_actions_since_last_resume_resets_at_marker():
    """The gate's counter ignores pre-resume work: a resume marker zeroes it,
    and bookkeeping tools never count."""
    from disco.core import ToolCall
    from disco.core.loop.engine import AgentLoop

    shell = ActionEvent(thought="t", tool_call=ToolCall(tool_name="shell", arguments={}))
    plan = ActionEvent(thought="t", tool_call=ToolCall(tool_name="submit_plan", arguments={}))
    resumed = StatusEvent(status=ConversationStatus.RUNNING, detail="resumed")
    # The verify-on-finish probe is the GATE's action, not the agent's (re-run #6).
    probe = ActionEvent(
        thought="Verifying completion: pytest -q",
        tool_call=ToolCall(tool_name="shell", arguments={}),
        meta={"verify_probe": True},
    )

    assert AgentLoop._actions_since_last_resume([shell, shell]) == 2
    assert AgentLoop._actions_since_last_resume([shell, resumed]) == 0
    assert AgentLoop._actions_since_last_resume([shell, resumed, plan]) == 0
    assert AgentLoop._actions_since_last_resume([shell, resumed, shell]) == 1
    assert AgentLoop._actions_since_last_resume([resumed, probe]) == 0
    assert AgentLoop._actions_since_last_resume([resumed, probe, shell]) == 1


# ---- meta-tool suppression until first real action (Phase-B re-run #4) ----------
#
# Re-run #4 showed the model burning its post-resume turns on remember spam —
# the THIRD distinct meta-tool escape (prose, serve, remember). Refusal gates
# converge one tool at a time; shaping the offered tool set doesn't: until the
# session's first real action, notify_user / remember / serve are WITHHELD.


_META_VIRTUALS = {"serve", "remember", "notify_user", "ask_user", "propose_plan_update"}


def test_tools_for_step_suppresses_meta_tools():
    """suppress_meta_tools drops every virtual except finish — the first turn
    of a session must be a real tool call (finish stays, gated by verify)."""
    agent = ScriptedAgent([finish_step()])
    loop, _ = build_loop(agent)

    full = {getattr(t, "name", None) for t in loop._tools_for_step()}
    assert _META_VIRTUALS <= full

    lean = {getattr(t, "name", None) for t in
            loop._tools_for_step(suppress_meta_tools=True)}
    assert not (_META_VIRTUALS & lean)
    assert {"finish", "shell"} <= lean


@pytest.mark.asyncio
async def test_meta_tools_withheld_until_first_real_action():
    """The run loop offers a lean tool set on the session's first turn and the
    full set once a real action has landed."""
    agent = ScriptedAgent([
        action_step("shell", {}),   # first turn: lean set offered
        action_step("shell", {}),   # second turn: full set offered
        finish_step(),
    ])
    loop, _ = build_loop(agent)
    await loop.send_message("go")
    await loop.run()

    first, second = agent.seen_tools[0], agent.seen_tools[1]
    assert not (_META_VIRTUALS & set(first))
    assert "finish" in first and "shell" in first
    assert _META_VIRTUALS <= set(second)


@pytest.mark.asyncio
async def test_ask_user_before_any_work_refused():
    """ask_user as the session's first move (re-run #5's hallucinated-call
    shape) → refused with actionable feedback, NO question gate; after one
    real action the Ask-gate works normally."""
    agent = ScriptedAgent([
        action_step("ask_user", {"question": "should I rebuild?"}),  # refused
        action_step("shell", {}),                                     # real work
        action_step("ask_user", {"question": "sudo or not?"}),        # gated normally
        finish_step(),
    ])
    loop, store = build_loop(agent)
    await loop.send_message("go")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.AWAITING_USER_QUESTION
    events = await store.get_events(CID)
    refusals = [
        e for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and "ask_user refused" in (e.message.content if e.message else "")
    ]
    assert len(refusals) == 1
    questions = [
        e for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.AGENT
        and "sudo or not?" in (e.message.content if e.message else "")
    ]
    assert len(questions) == 1   # only the post-work question reached the gate
    assert not any(
        isinstance(e, MessageEvent)
        and e.source == EventSource.AGENT
        and "should I rebuild?" in (e.message.content if e.message else "")
        for e in events
    )


@pytest.mark.asyncio
async def test_failed_verify_probe_does_not_unlock_meta_tools():
    """Phase-B re-run #6 leak: first move = finish with a FAILING verify. The
    gate's probe runs as a shell ActionEvent — which must NOT count as the
    session's first real action, or the refused finish unlocks the withheld
    meta tools and the model can remember-spam (exactly what happened live)."""
    from disco.core import ToolResult
    from loop_fakes import FakeExecutor
    agent = ScriptedAgent([
        action_step("finish", {"summary": "done", "verify": "pytest -q"}),
        action_step("remember", {"fact": "csv columns"}),  # turn 2: still lean
        action_step("shell", {}),                          # real work at last
        finish_step(),
    ])
    failing = ToolResult(
        call_id="c", tool_name="shell", success=False, content="2 failed", error="exit 1"
    )
    loop, store = build_loop(agent, executor=FakeExecutor(result=failing))
    await loop.send_message("go")
    await loop.run()

    # Turn 2's offered tool set is STILL lean — the probe didn't flip it.
    assert not (_META_VIRTUALS & set(agent.seen_tools[1]))
    # And the hallucinated remember was intercepted, not executed: no
    # KnowledgeEvent landed before the real shell action.
    events = await store.get_events(CID)
    shell_seq = next(
        e.seq for e in events
        if isinstance(e, ActionEvent)
        and e.tool_call is not None
        and e.tool_call.tool_name == "shell"
        and not e.meta.get("verify_probe")
    )
    assert not any(
        isinstance(e, KnowledgeEvent) and e.seq < shell_seq for e in events
    )
