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
    WorkspaceMutationEvent,
)
from disco.core.effects import EffectCapability
from disco.core.llm import LLMTransientError, OperatingMode
from disco.core.loop import signals
from disco.core.loop.control import Disp
from disco.core.loop.turn_control import Valve
from loop_fakes import (
    ScriptedAgent,
    action_step,
    assert_blocked_question_landing,
    build_loop,
    finish_step,
)

CID = "conv"


@pytest.mark.asyncio
async def test_post_noop_valve_is_reentrant_safe() -> None:
    class _Loop:
        _invisible_steps = 0

        async def _events(self):
            return []

    valve = Valve(_Loop())
    calls = 0

    async def nested_refusal(_events, _noops):
        nonlocal calls
        calls += 1
        assert await valve.post_noop_valve() is Disp.CONTINUE
        return False

    valve.actionless_valve = nested_refusal

    assert await valve.post_noop_valve() is Disp.CONTINUE
    assert calls == 1


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
    """3 counted no-tool-call steps with an incomplete plan ask the user."""
    agent = ScriptedAgent(
        [
            action_step("submit_plan", {"summary": "p", "steps": [{"title": "1"}]}),
            noop_step("a"),
            noop_step("b"),
            noop_step("c"),
            noop_step("d"),
            finish_step(),
        ]
    )
    loop, store = build_loop(agent)
    loop.mode = OperatingMode.PLANNING
    loop._planning_tools = frozenset(["file_read"])
    await loop.send_message("go")
    await loop.run()
    await loop.approve_plan()

    state = await loop.run()
    assert state.execution_status == ConversationStatus.AWAITING_USER_QUESTION
    events = await store.get_events(CID)
    assert_blocked_question_landing(events, legacy_detail="actionless")
    msgs = [
        e for e in events if isinstance(e, MessageEvent) and e.source == EventSource.ENVIRONMENT
    ]
    assert any(
        "3 consecutive responses without doing any real work"
        in (m.message.content if m.message else "")
        for m in msgs
    )


@pytest.mark.asyncio
async def test_actionless_breaker_reset():
    """2 noops + real action + 2 more noops + real action → breaker never fires."""
    agent = ScriptedAgent(
        [
            action_step("submit_plan", {"summary": "p", "steps": [{"title": "1"}]}),
            noop_step("a"),
            noop_step("b"),
            action_step("shell", {}),  # resets the breaker
            noop_step("c"),
            noop_step("d"),
            action_step("shell", {}),
            finish_step(),
        ]
    )
    loop, store = build_loop(agent)
    loop.mode = OperatingMode.PLANNING
    loop._planning_tools = frozenset(["file_read"])
    await loop.send_message("go")
    await loop.run()
    await loop.approve_plan()

    state = await loop.run()
    # runthru-v2 (#3): plan-step bookkeeping no longer gates finish. The model did
    # productive work (shell), so it lands FINISHED cleanly — no partial_plan bounce.
    assert state.execution_status == ConversationStatus.FINISHED
    events = await store.get_events(CID)
    assert _last_status_detail(events) != "partial_plan"


@pytest.mark.asyncio
async def test_actionless_breaker_inert_when_plan_complete():
    """Breaker must not PAUSE when the plan is already complete — it lands a
    clean FINISHED instead. B5: with every plan step marked done, the actionless
    valve recognizes the build as finished at the cap (the model signaled done
    via noop turns instead of finish()) and terminates FINISHED."""
    agent = ScriptedAgent(
        [
            action_step("submit_plan", {"summary": "p", "steps": [{"title": "1"}]}),
            action_step("shell", {"command": "echo do the work"}),  # real productive work
            action_step("plan_step", {"index": 1, "state": "done"}),
            noop_step("a"),
            noop_step("b"),
            noop_step("c"),
            noop_step("d"),
            noop_step("e"),
            noop_step("f"),  # _max_consecutive_noops = 6
            finish_step(),
        ]
    )
    loop, store = build_loop(agent)
    loop.mode = OperatingMode.PLANNING
    loop._planning_tools = frozenset(["file_read"])
    await loop.send_message("go")
    await loop.run()
    await loop.approve_plan()

    state = await loop.run()
    assert state.execution_status == ConversationStatus.FINISHED
    events = await store.get_events(CID)
    # B5: a complete plan finishes cleanly via the completion path, not the
    # noop backstop — and NEVER PAUSED/actionless.
    assert _last_status_detail(events) == "completed_via_notify"


# ---- valve taxonomy: partial-plan landings (DEFECT-4) --------------------------


@pytest.mark.asyncio
async def test_finish_incomplete_plan_lands_finished_no_bounce():
    """runthru-v2 (#3): a finish with an incomplete plan no longer bounces.
    Plan-step bookkeeping does NOT gate finish — the run lands FINISHED cleanly,
    with no PAUSED/partial_plan taxonomy and no 'finishing was blocked' nudge.
    Real incompleteness is caught by the verify gates, not a bookkeeping proxy."""
    store = SqliteEventStore(":memory:")
    await store.append(
        CID,
        MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content="go")),
    )
    await store.append(CID, PlanEvent(summary="p", steps=[{"title": "1"}], revision=1))
    await store.append(CID, StatusEvent(status=ConversationStatus.RUNNING, detail="plan_approved"))

    agent = ScriptedAgent([finish_step()])  # repeats forever
    loop, _ = build_loop(agent, store=store)
    loop._auto_continue_cap = 3

    state = await loop.run()
    assert state.execution_status == ConversationStatus.FINISHED
    events = await store.get_events(CID)
    assert _last_status_detail(events) != "partial_plan"
    msgs = [
        e for e in events if isinstance(e, MessageEvent) and e.source == EventSource.ENVIRONMENT
    ]
    assert not any(
        "finishing was blocked" in (m.message.content if m.message else "") for m in msgs
    )


@pytest.mark.asyncio
async def test_finish_with_actions_and_incomplete_plan_finishes_cleanly():
    """runthru-v2 (#3): productive work + finish with an unmarked plan → FINISHED
    cleanly (no partial_plan bounce). Bookkeeping never gates finish."""
    store = SqliteEventStore(":memory:")
    await store.append(
        CID,
        MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content="go")),
    )
    await store.append(CID, PlanEvent(summary="p", steps=[{"title": "1"}], revision=1))
    await store.append(CID, StatusEvent(status=ConversationStatus.RUNNING, detail="plan_approved"))

    # Shell action first (productive, counts as actions_since), then finish repeats.
    agent = ScriptedAgent([action_step("shell", {}), finish_step()])
    loop, _ = build_loop(agent, store=store)
    loop._auto_continue_cap = 3

    state = await loop.run()
    assert state.execution_status == ConversationStatus.FINISHED
    events = await store.get_events(CID)
    assert _last_status_detail(events) != "partial_plan"


# ---- knowledge dedup (×179 CSV bloat) ------------------------------------------


@pytest.mark.asyncio
async def test_dedup_remember():
    """Same fact × multiple calls → exactly 1 KnowledgeEvent; dups get 'Already recorded'."""
    agent = ScriptedAgent(
        [
            # Real work first — the fresh-session backstop refuses a zero-work remember.
            action_step("shell", {}),
            action_step("remember", {"fact": "foo", "scope": "bar"}),
            action_step("remember", {"fact": "foo", "scope": "bar"}),  # dup
            action_step("remember", {"fact": "foo  ", "scope": "bar"}),  # dup (trailing space)
            action_step("remember", {"fact": "foo2", "scope": "bar"}),  # new fact
            action_step("remember", {"fact": "foo", "scope": "baz"}),  # new scope
            finish_step(),
        ]
    )
    loop, store = build_loop(agent)
    await loop.send_message("go")
    await loop.run()

    events = await store.get_events(CID)
    knowledges = [e for e in events if isinstance(e, KnowledgeEvent)]
    assert len(knowledges) == 3  # foo/bar, foo2/bar, foo/baz

    obs = [
        e
        for e in events
        if isinstance(e, ObservationEvent) and e.tool_result.tool_name == "remember"
    ]
    assert len(obs) == 2  # the two duplicate calls
    assert "Already recorded" in obs[0].tool_result.content
    assert all(
        event.tool_result.action_profile is not None
        and event.tool_result.action_profile.capabilities
        == frozenset({EffectCapability.RUN_CONTROL})
        for event in obs
    )


# ---- DEFECT-5: LLMTransientError retry + PAUSED, never ERROR -------------------


@pytest.mark.asyncio
async def test_transient_error_retry_succeeds(monkeypatch):
    """2 transient errors then success → step completes, delays == (10.0, 30.0)."""
    import disco.core.loop.driver as driver_module

    delays = []

    async def mock_sleep(d):
        delays.append(d)

    monkeypatch.setattr(driver_module, "_sleep", mock_sleep)

    # Indices 0 and 1 raise; index 2 returns the shell action; index 3 finishes.
    agent = ScriptedAgent(
        [
            LLMTransientError("unavailable"),
            LLMTransientError("unavailable"),
            action_step("shell", {}),
            finish_step(),
        ]
    )
    loop, store = build_loop(agent)
    await loop.send_message("go")
    await loop.run()

    assert delays == [10.0, 30.0]


@pytest.mark.asyncio
async def test_transient_error_persistent_pauses(monkeypatch):
    """Persistent LLMTransientError → explained driver-unavailable, no ErrorEvent."""
    import disco.core.loop.driver as driver_module

    async def mock_sleep(_d):
        pass

    monkeypatch.setattr(driver_module, "_sleep", mock_sleep)

    # Single-item list: ScriptedAgent repeats it on every call (min(i, 0) == 0).
    agent = ScriptedAgent([LLMTransientError("unavailable")])
    loop, store = build_loop(agent)
    await loop.send_message("go")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.AWAITING_USER_QUESTION
    events = await store.get_events(CID)
    assert_blocked_question_landing(events, legacy_detail="driver-unavailable")
    # 1 initial + 3 retries. The INTERACTIVE breaker lander no longer makes a bounded
    # model turn to author the question — it uses the deterministic fallback and emits
    # AWAITING_USER_QUESTION immediately, so the AskPanel surfaces instantly instead of
    # after a model-call-long delay (the "doesn't ask until you refresh/wait" fix).
    assert agent.calls == 4

    errors = [e for e in events if getattr(e, "kind", None) == "error"]
    assert len(errors) == 0


# ---- null-payload deliverable guard --------------------------------------------


@pytest.mark.asyncio
async def test_deliverable_guard():
    """Empty serve payload → no DeliverableEvent; non-empty → one event."""
    agent = ScriptedAgent(
        [
            action_step("shell", {}),  # real work first — serve gate requires it
            action_step("serve", {}),  # empty → skipped
            action_step("serve", {"title": "x", "path": "y"}),  # real deliverable
            finish_step(),
        ]
    )
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
    """Distinct serve calls with no real action in between → asks with actionless
    at the cap (3 DeliverableEvents max), not a 50-event spam run."""
    agent = ScriptedAgent(
        [
            action_step("submit_plan", {"summary": "p", "steps": [{"title": "1"}]}),
            action_step("shell", {}),  # real work first — serve gate requires it
            action_step("serve", {"title": "a", "path": "pa"}),
            action_step("serve", {"title": "b", "path": "pb"}),
            action_step("serve", {"title": "c", "path": "pc"}),
            action_step("serve", {"title": "d", "path": "pd"}),  # must never run
            finish_step(),
        ]
    )
    loop, store = await _approved_plan_loop(agent)

    state = await loop.run()
    assert state.execution_status == ConversationStatus.AWAITING_USER_QUESTION
    events = await store.get_events(CID)
    assert_blocked_question_landing(events, legacy_detail="actionless")
    deliverables = [e for e in events if isinstance(e, DeliverableEvent)]
    assert len(deliverables) == 3


@pytest.mark.asyncio
async def test_duplicate_serve_suppressed_and_trips_valve():
    """Re-serving the same (path, kind) → ONE DeliverableEvent total; the
    invisible-step counter still trips the valve."""
    agent = ScriptedAgent(
        [
            action_step("submit_plan", {"summary": "p", "steps": [{"title": "1"}]}),
            action_step("shell", {}),  # real work first — serve gate requires it
            action_step("serve", {"title": "app", "path": "p"}),
            action_step("serve", {"title": "app", "path": "p"}),  # dup → invisible
            action_step(
                "serve", {"title": "app again", "path": "p"}
            ),  # dup (title spin) → invisible
            finish_step(),
        ]
    )
    loop, store = await _approved_plan_loop(agent)
    # Model the preceding missing-app finish refusal from the live recovery
    # path. The first valid handoff clears that invisible debt; two later
    # duplicates still reach the cap and halt.
    loop._invisible_steps = 1

    state = await loop.run()
    assert state.execution_status == ConversationStatus.AWAITING_USER_QUESTION
    events = await store.get_events(CID)
    assert_blocked_question_landing(events, legacy_detail="actionless")
    deliverables = [e for e in events if isinstance(e, DeliverableEvent)]
    assert len(deliverables) == 1
    duplicate_guidance = [
        e
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and e.meta.get("diagnostic") == "serve_duplicate_ignored"
    ]
    assert len(duplicate_guidance) == 2
    assert all("call `finish`" in e.message.content for e in duplicate_guidance)
    assert any("Handoff recorded" in message.content for message in agent.seen_views[3].messages)
    assert any(
        "duplicate `serve` call was ignored" in message.content
        for message in agent.seen_views[4].messages
    )


@pytest.mark.asyncio
async def test_same_path_serve_is_recorded_again_for_a_new_run_intent():
    """A previous turn's handoff cannot suppress the current turn's receipt."""
    agent = ScriptedAgent(
        [
            action_step("shell", {}),
            action_step("serve", {"title": "app", "path": "index.html"}),
            finish_step(),
            action_step("shell", {}),
            action_step("serve", {"title": "updated app", "path": "index.html"}),
            finish_step(),
        ]
    )
    loop, store = build_loop(agent)

    await loop.send_message("build the app")
    await store.append(
        CID,
        WorkspaceMutationEvent(
            operation="agent.run-intent.user-turn",
            run_protocol_version=1,
        ),
    )
    assert (await loop.run()).execution_status == ConversationStatus.FINISHED
    await loop.send_message("update the app")
    await store.append(
        CID,
        WorkspaceMutationEvent(
            operation="agent.run-intent.user-turn",
            run_protocol_version=1,
        ),
    )
    assert (await loop.run()).execution_status == ConversationStatus.FINISHED

    events = await store.get_events(CID)
    deliverables = [event for event in events if isinstance(event, DeliverableEvent)]
    assert [event.title for event in deliverables] == ["app", "updated app"]
    assert not any(
        isinstance(event, MessageEvent)
        and event.meta.get("diagnostic") == "serve_duplicate_ignored"
        for event in events
    )


@pytest.mark.asyncio
async def test_empty_serve_spam_trips_valve():
    """Empty-args serve persists NOTHING to the log — the instance counter is
    the only witness, and it must still trip the valve.

    The id is KEPT; the body was strengthened at 2026-08-07b. It used to require
    the string "provide both required string fields" to appear THREE TIMES —
    that is, it pinned byte-identical repetition as correct, the exact defect
    the GROUNDED FEEDBACK twice-rule (constraint 4) names. The property that
    assertion stood for is that every malformed `serve` gets corrective feedback
    and the valve still trips; that is asserted here against the run, plus the
    twice-rule itself: the repeats must NOT be byte-identical, and the later
    ones must acknowledge the count.
    """
    agent = ScriptedAgent(
        [
            action_step("submit_plan", {"summary": "p", "steps": [{"title": "1"}]}),
            action_step("shell", {}),
            action_step("serve", {}),
            action_step("serve", {}),
            action_step("serve", {}),
            finish_step(),
        ]
    )
    loop, store = await _approved_plan_loop(agent)

    state = await loop.run()
    assert state.execution_status == ConversationStatus.AWAITING_USER_QUESTION
    events = await store.get_events(CID)
    assert_blocked_question_landing(events, legacy_detail="actionless")
    assert not any(isinstance(e, DeliverableEvent) for e in events)
    malformed_feedback = [
        e
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and e.meta.get("diagnostic") == "serve_argument_refused"
    ]
    assert len(malformed_feedback) == 3
    bodies = [e.message.content for e in malformed_feedback]
    # every refusal still names the missing contract and the one corrective move
    for body in bodies:
        assert "serve refused:" in body
        assert "Next move:" in body
    # the twice-rule: no two firings are the same bytes, and the repeats say so
    assert len(set(bodies)) == 3
    assert "2 times in this run" in bodies[1]
    assert "3 times in this run" in bodies[2]


@pytest.mark.asyncio
async def test_duplicate_remember_spam_trips_valve():
    """Duplicate remember calls (the dedup ActionEvent pair) count toward the
    actionless streak instead of resetting it."""
    agent = ScriptedAgent(
        [
            action_step("submit_plan", {"summary": "p", "steps": [{"title": "1"}]}),
            # Real work first — the fresh-session backstop refuses a zero-work remember.
            action_step("shell", {}),
            action_step("remember", {"fact": "foo", "scope": "s"}),  # fresh → neutral
            action_step("remember", {"fact": "foo", "scope": "s"}),  # dup → counts
            action_step("remember", {"fact": "foo", "scope": "s"}),  # dup → counts
            action_step("remember", {"fact": "foo", "scope": "s"}),  # dup → trips cap
            finish_step(),
        ]
    )
    loop, store = await _approved_plan_loop(agent)

    state = await loop.run()
    assert state.execution_status == ConversationStatus.AWAITING_USER_QUESTION
    events = await store.get_events(CID)
    assert_blocked_question_landing(events, legacy_detail="actionless")
    knowledges = [e for e in events if isinstance(e, KnowledgeEvent)]
    assert len(knowledges) == 1


@pytest.mark.asyncio
async def test_notify_user_spam_trips_valve():
    """notify_user prose spam (the 'I'm back!' degeneration) → AWAITING_USER
    actionless at the cap — the intercept no longer bypasses the valve."""
    agent = ScriptedAgent(
        [
            action_step("submit_plan", {"summary": "p", "steps": [{"title": "1"}]}),
            action_step("notify_user", {"message": "I'm back after the restart!"}),
            action_step("notify_user", {"message": "Resuming work now!"}),
            action_step("notify_user", {"message": "Picking up where I left off!"}),
            finish_step(),
        ]
    )
    loop, store = await _approved_plan_loop(agent)

    state = await loop.run()
    assert state.execution_status == ConversationStatus.AWAITING_USER_QUESTION
    events = await store.get_events(CID)
    assert_blocked_question_landing(events, legacy_detail="actionless")


@pytest.mark.asyncio
async def test_real_action_resets_intercept_streak():
    """Serves interleaved with real actions never trip the valve — the streak
    (event-derived AND invisible counter) resets on a real ActionEvent."""
    agent = ScriptedAgent(
        [
            action_step("submit_plan", {"summary": "p", "steps": [{"title": "1"}]}),
            action_step("shell", {}),  # real work first — serve gate requires it
            action_step("serve", {"title": "a", "path": "pa"}),
            action_step("serve", {"title": "a", "path": "pa"}),  # dup → invisible +1
            action_step("shell", {}),  # real action → reset
            action_step("serve", {"title": "b", "path": "pb"}),
            action_step("serve", {"title": "c", "path": "pc"}),
            action_step("shell", {}),
            finish_step(),
        ]
    )
    loop, store = await _approved_plan_loop(agent)

    state = await loop.run()
    events = await store.get_events(CID)
    statuses = [e.detail for e in events if isinstance(e, StatusEvent)]
    assert "actionless" not in statuses
    deliverables = [e for e in events if isinstance(e, DeliverableEvent)]
    assert len(deliverables) == 3  # pa, pb, pc — the dup suppressed
    # Lands via the finish path (clean FINISHED), not a valve trip. runthru-v2
    # (#3): no partial_plan bounce — bookkeeping no longer gates finish.
    assert state.execution_status == ConversationStatus.FINISHED
    assert _last_status_detail(events) != "partial_plan"


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
    agent = ScriptedAgent(
        [
            action_step("serve", {"title": "app", "path": "."}),  # zero work → refused
            action_step("shell", {}),  # real work
            action_step("serve", {"title": "app", "path": "index.html"}),  # now allowed
            finish_step(),
        ]
    )
    loop, store = build_loop(agent)
    await loop.send_message("go")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED
    events = await store.get_events(CID)
    deliverables = [e for e in events if isinstance(e, DeliverableEvent)]
    assert len(deliverables) == 1  # only the post-work serve landed
    refusals = [
        e
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and "serve refused" in (e.message.content if e.message else "")
    ]
    assert len(refusals) == 1


@pytest.mark.asyncio
async def test_serve_before_work_spam_trips_valve():
    """Gate-refused serves count as invisible steps — three in a row trips
    the actionless valve, zero DeliverableEvents persisted."""
    agent = ScriptedAgent(
        [
            action_step("submit_plan", {"summary": "p", "steps": [{"title": "1"}]}),
            action_step("serve", {"title": "a", "path": "pa"}),  # refused (no work)
            action_step("serve", {"title": "b", "path": "pb"}),  # refused
            action_step("serve", {"title": "c", "path": "pc"}),  # refused → cap
            finish_step(),
        ]
    )
    loop, store = await _approved_plan_loop(agent)

    state = await loop.run()
    assert state.execution_status == ConversationStatus.AWAITING_USER_QUESTION
    events = await store.get_events(CID)
    assert_blocked_question_landing(events, legacy_detail="actionless")
    assert not any(isinstance(e, DeliverableEvent) for e in events)


def test_actions_since_last_resume_resets_at_marker():
    """The gate's counter ignores pre-resume work: a resume marker zeroes it,
    and bookkeeping tools never count."""
    from disco.core import ToolCall

    shell = ActionEvent(thought="t", tool_call=ToolCall(tool_name="shell", arguments={}))
    plan = ActionEvent(thought="t", tool_call=ToolCall(tool_name="submit_plan", arguments={}))
    resumed = StatusEvent(status=ConversationStatus.RUNNING, detail="resumed")
    # The verify-on-finish probe is the GATE's action, not the agent's (re-run #6).
    probe = ActionEvent(
        thought="Verifying completion: pytest -q",
        tool_call=ToolCall(tool_name="shell", arguments={}),
        meta={"verify_probe": True},
    )

    assert signals.actions_since_last_resume([shell, shell]) == 2
    assert signals.actions_since_last_resume([shell, resumed]) == 0
    assert signals.actions_since_last_resume([shell, resumed, plan]) == 0
    assert signals.actions_since_last_resume([shell, resumed, shell]) == 1
    assert signals.actions_since_last_resume([resumed, probe]) == 0
    assert signals.actions_since_last_resume([resumed, probe, shell]) == 1

    # codex P1 / fb60fc8 twin: AgentLoop.resume() emits a BARE RUNNING (no
    # detail), AND a PAUSED is itself a resume boundary. Pre-pause work must NOT
    # leak past it — otherwise the BW-02 STUCK escalation's `== 0` gate stays
    # False forever after a resume that followed real work.
    paused = StatusEvent(status=ConversationStatus.PAUSED, detail="actionless")
    bare_running = StatusEvent(status=ConversationStatus.RUNNING)
    # prior file_read, then PAUSED, then a bare-RUNNING resume, then nothing:
    # zero actions since the real resume boundary (NOT 1 counting the pre-pause shell).
    assert signals.actions_since_last_resume([shell, paused, bare_running]) == 0
    assert signals.actions_since_last_resume([shell, paused]) == 0
    # a real action AFTER the bare-RUNNING resume DOES count.
    assert signals.actions_since_last_resume([shell, paused, bare_running, shell]) == 1
    # a normal-run bare RUNNING with NO preceding PAUSED is neutral (does not reset).
    assert signals.actions_since_last_resume([shell, bare_running, shell]) == 2


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

    lean = {getattr(t, "name", None) for t in loop._tools_for_step(suppress_meta_tools=True)}
    assert not (_META_VIRTUALS & lean)
    assert {"finish", "shell"} <= lean


@pytest.mark.asyncio
async def test_meta_tools_withheld_until_first_real_action():
    """The run loop offers a lean tool set on the session's first turn and the
    full set once a real action has landed."""
    agent = ScriptedAgent(
        [
            action_step("shell", {}),  # first turn: lean set offered
            action_step("shell", {}),  # second turn: full set offered
            finish_step(),
        ]
    )
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
    agent = ScriptedAgent(
        [
            action_step("ask_user", {"question": "should I rebuild?"}),  # refused
            action_step("shell", {}),  # real work
            action_step("ask_user", {"question": "sudo or not?"}),  # gated normally
            finish_step(),
        ]
    )
    loop, store = build_loop(agent)
    await loop.send_message("go")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.AWAITING_USER_QUESTION
    events = await store.get_events(CID)
    refusals = [
        e
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and "ask_user refused" in (e.message.content if e.message else "")
    ]
    assert len(refusals) == 1
    questions = [
        e
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.AGENT
        and "sudo or not?" in (e.message.content if e.message else "")
    ]
    assert len(questions) == 1  # only the post-work question reached the gate
    assert not any(
        isinstance(e, MessageEvent)
        and e.source == EventSource.AGENT
        and "should I rebuild?" in (e.message.content if e.message else "")
        for e in events
    )


@pytest.mark.asyncio
async def test_failed_verify_probe_does_not_unlock_meta_tools():
    """A failed model-supplied finish check is evidence, not build authority.

    The host records its probe without treating it as agent work and finishes
    on the same turn.  In particular, it must not reopen the builder with the
    meta tools unlocked by its own probe.
    """
    from disco.core import ToolResult
    from loop_fakes import FakeExecutor

    agent = ScriptedAgent(
        [
            action_step("finish", {"summary": "done", "verify": "pytest -q"}),
            action_step("remember", {"fact": "csv columns"}),  # turn 2: still lean
            action_step("shell", {}),  # real work at last
            finish_step(),
        ]
    )
    failing = ToolResult(
        call_id="c", tool_name="shell", success=False, content="2 failed", error="exit 1"
    )
    loop, store = build_loop(agent, executor=FakeExecutor(result=failing))
    await loop.send_message("go")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED
    assert len(agent.seen_tools) == 1
    events = await store.get_events(CID)
    assert any(
        isinstance(e, StatusEvent) and e.detail == "finish_verify_advisory_failed"
        for e in events
    )
    assert any(
        isinstance(e, ActionEvent)
        and e.tool_call is not None
        and e.tool_call.tool_name == "shell"
        and e.meta.get("verify_probe")
        for e in events
    )
    assert not any(isinstance(e, KnowledgeEvent) for e in events)
    assert not any(
        isinstance(e, ActionEvent)
        and e.tool_call is not None
        and e.tool_call.tool_name == "shell"
        and not e.meta.get("verify_probe")
        for e in events
    )


# ---- Fix 3: in-loop exit invariant (no path carries RUNNING out of run()) ------
# Defense-in-depth atop the runtime backstop (9c90d7e). engine.py funnels every
# drive-loop exit through one boundary: on a NORMAL return, if the reconstructed
# status is still RUNNING, run() terminalizes to STUCK in-turn with the same
# canonical detail the supervisor uses — instead of sitting RUNNING forever.

_STUCK_DETAIL = "loop ended without reaching a terminal state"


@pytest.mark.asyncio
async def test_exit_invariant_terminalizes_running_return():
    """A drive loop that returns while status is still RUNNING (a dropped /
    no-event step on a HALT path) must explain and ask in-turn with the canonical
    detail in metadata — the conversation never carries RUNNING out of run()."""
    agent = ScriptedAgent([finish_step()])
    loop, store = build_loop(agent)
    await loop.send_message("go")

    # Simulate the wedge: run() emits RUNNING just before calling _run_drive;
    # the loop yields a return WITHOUT any terminal/parked emit (status stays
    # RUNNING). The instance-attr shadow is an unbound async fn (no self).
    async def wedged_drive():
        return await loop.get_state()

    loop._run_drive = wedged_drive  # type: ignore[method-assign]

    state = await loop.run()

    assert state.execution_status == ConversationStatus.AWAITING_USER_QUESTION
    events = await store.get_events(CID)
    assert_blocked_question_landing(events, legacy_detail=_STUCK_DETAIL)


@pytest.mark.asyncio
async def test_exit_invariant_no_spurious_stuck_on_finished():
    """The invariant must NOT fire on a clean FINISHED exit — no spurious STUCK."""
    agent = ScriptedAgent([action_step(), finish_step()])
    loop, store = build_loop(agent)
    await loop.send_message("do the task")

    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED
    events = await store.get_events(CID)
    assert not any(isinstance(e, StatusEvent) and e.detail == _STUCK_DETAIL for e in events)


@pytest.mark.asyncio
async def test_exit_invariant_no_spurious_stuck_on_parked():
    """The invariant must NOT add its own block on a legitimate actionless park."""
    agent = ScriptedAgent(
        [
            action_step("submit_plan", {"summary": "p", "steps": [{"title": "1"}]}),
            noop_step("a"),
            noop_step("b"),
            noop_step("c"),
            finish_step(),
        ]
    )
    loop, store = build_loop(agent)
    loop.mode = OperatingMode.PLANNING
    loop._planning_tools = frozenset(["file_read"])
    await loop.send_message("go")
    await loop.run()
    await loop.approve_plan()

    state = await loop.run()

    assert state.execution_status == ConversationStatus.AWAITING_USER_QUESTION
    events = await store.get_events(CID)
    assert not any(isinstance(e, StatusEvent) and e.detail == _STUCK_DETAIL for e in events)
