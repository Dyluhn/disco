import pytest
from loop_fakes import ScriptedAgent, action_step, build_loop, finish_step
from perpleximanus.core import (
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
from perpleximanus.core.llm import LLMTransientError, OperatingMode

CID = "conv"


def noop_step(thought="just talking"):
    from perpleximanus.core.loop import AgentStep
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
        "3 consecutive responses without any tool call" in (m.message.content if m.message else "")
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
    import perpleximanus.core.loop.engine as engine_module

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
    import perpleximanus.core.loop.engine as engine_module

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
