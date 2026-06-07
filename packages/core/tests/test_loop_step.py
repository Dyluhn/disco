"""One-action-per-iteration + execute-and-observe pairing — §10.2, §10.3."""

from __future__ import annotations

from loop_fakes import FakeExecutor, ScriptedAgent, action_step, build_loop, finish_step
from perpleximanus.core import (
    ActionEvent,
    AgentErrorEvent,
    ObservationEvent,
    ToolResult,
)

CID = "conv"


def _dangling_actions(events):
    """ActionEvents with no paired observation/error (the crash-recovery trace)."""
    observed = {
        e.action_id
        for e in events
        if isinstance(e, ObservationEvent | AgentErrorEvent) and e.action_id is not None
    }
    return [e for e in events if isinstance(e, ActionEvent) and e.id not in observed]


# ---- §10.2 one action per iteration -----------------------------------------


async def test_single_action_yields_one_action_and_one_observation():
    agent = ScriptedAgent([action_step(), finish_step()])
    loop, store = build_loop(agent)
    await loop.send_message("go")
    await loop.run()
    events = await store.get_events(CID)
    assert sum(isinstance(e, ActionEvent) for e in events) == 1
    assert sum(isinstance(e, ObservationEvent) for e in events) == 1
    assert _dangling_actions(events) == []  # the action was observed before finishing


# ---- §10.3 execute-and-observe pairing --------------------------------------


async def test_success_yields_exactly_one_observation_with_matching_action_id():
    agent = ScriptedAgent([action_step(), finish_step()])
    loop, store = build_loop(agent)
    await loop.send_message("go")
    await loop.run()
    events = await store.get_events(CID)
    action = next(e for e in events if isinstance(e, ActionEvent))
    obs = [e for e in events if isinstance(e, ObservationEvent)]
    assert len(obs) == 1
    assert obs[0].action_id == action.id


async def test_executor_exception_yields_exactly_one_agent_error():
    agent = ScriptedAgent([action_step(), finish_step()])
    loop, store = build_loop(agent, executor=FakeExecutor(raises=RuntimeError("boom")))
    await loop.send_message("go")
    await loop.run()
    events = await store.get_events(CID)
    action = next(e for e in events if isinstance(e, ActionEvent))
    errs = [e for e in events if isinstance(e, AgentErrorEvent)]
    assert len(errs) == 1
    assert errs[0].action_id == action.id
    assert "boom" in errs[0].error
    assert not any(isinstance(e, ObservationEvent) for e in events)  # never two


async def test_tool_failure_result_yields_agent_error():
    failing = ToolResult(call_id="c", tool_name="shell", success=False, content="", error="exit 1")
    agent = ScriptedAgent([action_step(), finish_step()])
    loop, store = build_loop(agent, executor=FakeExecutor(result=failing))
    await loop.send_message("go")
    await loop.run()
    events = await store.get_events(CID)
    errs = [e for e in events if isinstance(e, AgentErrorEvent)]
    assert len(errs) == 1 and "exit 1" in errs[0].error


async def test_dangling_action_is_detectable_after_crash():
    """Simulate a crash between the ActionEvent and its observation: the proposed
    action is recorded first (§4.1), so on replay it is a detectable dangling
    action with no paired observation."""
    from perpleximanus.core import SqliteEventStore, ToolCall

    store = SqliteEventStore(":memory:")
    await store.append(
        CID, ActionEvent(thought="t", tool_call=ToolCall(tool_name="shell", arguments={}))
    )
    # ... process killed here, before the observation is appended ...
    events = await store.get_events(CID)
    assert len(_dangling_actions(events)) == 1


# ---- error-recovery system reminders ----------------------------------------


async def _reminders(events):
    """Pull just the system-reminder MessageEvents out of the log (the loop emits
    them with source=ENVIRONMENT and a wrapped <system-reminder> body)."""
    from perpleximanus.core import EventSource, MessageEvent

    return [
        e
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and "<system-reminder>" in (e.message.content if e.message else "")
    ]


async def test_failures_do_not_trigger_automatic_reminders():
    """v2 redesign (after the Claude Code research): failures emit AgentErrorEvents
    that the model sees as raw context, and the model decides what to do next.
    The harness no longer injects automatic escalation reminders — that pattern
    caused instruction dilution and was opaque to both the model and the user.
    Recovery is now: visible errors + the model's own reasoning + the user's
    real-time steer/interrupt + the model's voluntary `ask_user` tool when IT
    decides human input is needed."""
    failing = ToolResult(call_id="c", tool_name="shell", success=False, content="", error="bad")
    agent = ScriptedAgent(
        [action_step(), action_step(), action_step(), action_step(), finish_step()]
    )
    loop, store = build_loop(agent, executor=FakeExecutor(result=failing))
    await loop.send_message("go")
    await loop.run()
    reminders = await _reminders(await store.get_events(CID))
    # No escalating reminders are emitted by the harness — even after 4 failures.
    # The trace shows the failures, the model reasons about them on the next
    # turn (or the user interrupts). This is the proven pattern.
    assert reminders == [], (
        "the loop must NOT emit automatic failure-recovery reminders. "
        "Failures are visible context for the model to reason about; the harness "
        "is observability + substrate, not behavior change."
    )


def test_plan_is_incomplete_helper_recognizes_partial_completion():
    """Unit test the plan-completeness gate's truth source. If a plan exists and
    not every step has been marked plan_step(idx, "done"), the helper returns
    (True, [missing_indices]) — which the FINISHED transition uses to fall back
    to STUCK with a reminder naming the gap."""
    from perpleximanus.core import ActionEvent, PlanEvent, ToolCall
    from perpleximanus.core.loop.engine import AgentLoop

    # Plan with 3 steps; only step 1 marked done.
    events = [
        PlanEvent(summary="p", steps=[{"title": "a"}, {"title": "b"}, {"title": "c"}], revision=1),
        ActionEvent(
            thought="step 1 done",
            tool_call=ToolCall(tool_name="plan_step", arguments={"index": 1, "state": "done"}),
        ),
    ]
    incomplete, missing = AgentLoop._plan_is_incomplete(events)
    assert incomplete is True
    assert missing == [2, 3]


def test_plan_is_incomplete_helper_passes_when_all_steps_done():
    """When every step is marked done, the gate clears — FINISHED is allowed."""
    from perpleximanus.core import ActionEvent, PlanEvent, ToolCall
    from perpleximanus.core.loop.engine import AgentLoop

    events = [
        PlanEvent(summary="p", steps=[{"title": "a"}, {"title": "b"}], revision=1),
        ActionEvent(
            thought="1",
            tool_call=ToolCall(tool_name="plan_step", arguments={"index": 1, "state": "done"}),
        ),
        ActionEvent(
            thought="2",
            tool_call=ToolCall(tool_name="plan_step", arguments={"index": 2, "state": "done"}),
        ),
    ]
    incomplete, missing = AgentLoop._plan_is_incomplete(events)
    assert incomplete is False
    assert missing == []


def test_plan_is_incomplete_helper_inert_without_plan():
    """No plan at all → no progress signal to gate on → not incomplete. The gate
    is a no-op for non-plan-first flows (Build runs without an explicit plan)."""
    from perpleximanus.core.loop.engine import AgentLoop

    incomplete, missing = AgentLoop._plan_is_incomplete([])
    assert incomplete is False
    assert missing == []


def test_plan_is_incomplete_helper_uses_latest_revision():
    """A re-plan (revision bump) replaces prior — the gate must check completeness
    against the LATEST plan, not the original."""
    from perpleximanus.core import ActionEvent, PlanEvent, ToolCall
    from perpleximanus.core.loop.engine import AgentLoop

    events = [
        PlanEvent(summary="p1", steps=[{"title": "a"}], revision=1),
        # All steps of plan #1 done.
        ActionEvent(
            thought="1",
            tool_call=ToolCall(tool_name="plan_step", arguments={"index": 1, "state": "done"}),
        ),
        # Re-plan with 2 steps; neither marked done yet.
        PlanEvent(summary="p2", steps=[{"title": "a"}, {"title": "b"}], revision=2),
    ]
    incomplete, missing = AgentLoop._plan_is_incomplete(events)
    # plan #2 is the latest — step 1 of p2 was never explicitly marked (the prior
    # done was for p1's step 1, but it's the same index — the helper treats index
    # as opaque, so p1's done carries forward. The unmarked one is step 2.).
    assert incomplete is True
    assert missing == [2]


# ---- the model-chosen ask_user → AWAITING_USER_DECISION gate ----------------


async def test_ask_user_intercepts_and_halts_at_decision_gate():
    """When the agent VOLUNTARILY emits an `ask_user` tool call (the model's
    own decision after reasoning, not a harness-funneled reminder), the loop
    intercepts it (the tool is never executed), builds an AlternativesEvent
    from the structured options, and halts at AWAITING_USER_DECISION until
    the user picks one. This is the model-driven escape hatch."""
    from perpleximanus.core import AlternativesEvent, ConversationStatus

    alt_call = action_step(
        tool="ask_user",
        args={
            "summary": "the shell call keeps failing on /tmp/locked",
            "options": [
                {
                    "id": "a",
                    "title": "Try with sudo",
                    "description": "elevate to root",
                    "tool_name": "shell",
                    "arguments": {"command": "sudo rm -rf /tmp/locked"},
                },
                {
                    "id": "b",
                    "title": "Move out of the way",
                    "description": "rename instead of delete",
                    "tool_name": "shell",
                    "arguments": {"command": "mv /tmp/locked /tmp/locked.bak"},
                },
            ],
        },
    )
    agent = ScriptedAgent([alt_call, finish_step()])
    loop, store = build_loop(agent)
    await loop.send_message("clean up tmp")
    await loop.run()
    state = await store.get_state(CID)
    assert state.execution_status == ConversationStatus.AWAITING_USER_DECISION
    # The AlternativesEvent landed with both options + the pending id correlates
    events = await store.get_events(CID)
    alts = [e for e in events if isinstance(e, AlternativesEvent)]
    assert len(alts) == 1
    assert [o.id for o in alts[0].options] == ["a", "b"]
    assert alts[0].options[0].tool_name == "shell"
    assert state.pending_alternatives_id == alts[0].id


async def test_pick_alternative_runs_the_selected_option():
    """User picks option 'a' → the loop synthesizes an ActionEvent from option
    'a's tool_call (shell with sudo) → executes it → returns to RUNNING."""
    from perpleximanus.core import (
        ActionEvent,
        AlternativesEvent,
        ConversationStatus,
        ObservationEvent,
    )

    alt_call = action_step(
        tool="ask_user",
        args={
            "summary": "shell keeps failing",
            "options": [
                {
                    "id": "a",
                    "title": "Sudo it",
                    "description": "elevate",
                    "tool_name": "shell",
                    "arguments": {"command": "sudo true"},
                },
                {
                    "id": "b",
                    "title": "Skip",
                    "description": "do nothing",
                    "tool_name": "shell",
                    "arguments": {"command": "true"},
                },
            ],
        },
    )
    # After the alternatives are emitted + the gate hits, picking will inject a
    # new ActionEvent and the loop resumes. The next agent step (finish_step)
    # cleanly ends the run.
    agent = ScriptedAgent([alt_call, finish_step()])
    loop, store = build_loop(agent)
    await loop.send_message("clean up")
    await loop.run()
    assert (
        await store.get_state(CID)
    ).execution_status == ConversationStatus.AWAITING_USER_DECISION

    await loop.pick_alternative("a")

    events = await store.get_events(CID)
    # The synthesized ActionEvent for option 'a' landed with the picked tool_call
    synthesized = [
        e
        for e in events
        if isinstance(e, ActionEvent)
        and e.tool_call is not None
        and e.tool_call.tool_name == "shell"
        and e.tool_call.arguments.get("command") == "sudo true"
    ]
    assert len(synthesized) == 1
    # And the executor ran it (FakeExecutor returns success by default)
    obs = [
        e
        for e in events
        if isinstance(e, ObservationEvent) and e.action_id == synthesized[0].id
    ]
    assert len(obs) == 1


async def test_pick_alternative_with_unknown_id_does_not_resume():
    """Defensive: a bad pick (stale id, wrong user) doesn't crash — it leaves
    the gate intact + emits a reminder so the user can try again."""
    from perpleximanus.core import ConversationStatus, MessageEvent

    alt_call = action_step(
        tool="ask_user",
        args={
            "summary": "x",
            "options": [
                {
                    "id": "a",
                    "title": "Only option",
                    "description": "x",
                    "tool_name": "shell",
                    "arguments": {"command": "true"},
                }
            ],
        },
    )
    agent = ScriptedAgent([alt_call, finish_step()])
    loop, store = build_loop(agent)
    await loop.send_message("go")
    await loop.run()

    await loop.pick_alternative("nope")
    # Still parked at the gate — no synthesized action for the bogus pick
    state = await store.get_state(CID)
    assert state.execution_status == ConversationStatus.AWAITING_USER_DECISION
    # And the reminder explaining the bad pick landed
    events = await store.get_events(CID)
    bad_pick_reminders = [
        e
        for e in events
        if isinstance(e, MessageEvent)
        and "unknown alternative id" in (e.message.content if e.message else "")
    ]
    assert len(bad_pick_reminders) == 1


async def test_propose_plan_update_intercepts_and_halts_at_plan_approval():
    """When the agent's current plan is wrong (a step failed structurally, a
    discovery invalidates the path, etc.), the model can call
    `propose_plan_update` to emit a NEW PlanEvent revision and pause for user
    approval. Slots chronologically into the chat; the existing plan-approval
    UI handles accept/refine/reject. This is the auto-recovery affordance the
    model uses without the user having to poke it."""
    from perpleximanus.core import ConversationStatus, PlanEvent

    update_call = action_step(
        tool="propose_plan_update",
        args={
            "summary": "the original CORS-proxy path doesn't work; using a different API instead",
            "steps": [
                {"title": "Switch to allorigins.win proxy"},
                {"title": "Test the new endpoint"},
                {"title": "Update index.html with the new URL"},
            ],
            "context": "yahoo.com returns 403 via corsproxy.io; allorigins is the working alternative",
        },
        thought="my current plan is wrong; here's a course correction",
    )
    agent = ScriptedAgent([update_call, finish_step()])
    loop, store = build_loop(agent)
    await loop.send_message("build a stock tracker")
    await loop.run()

    state = await store.get_state(CID)
    assert state.execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL
    events = await store.get_events(CID)
    # New PlanEvent landed with the revised steps
    plans = [e for e in events if isinstance(e, PlanEvent)]
    assert len(plans) >= 1
    latest = plans[-1]
    # Summary captures the WHY (model-supplied); context captures the supporting
    # findings — the test asserts the substance landed somewhere.
    assert "different API" in latest.summary
    assert "allorigins" in latest.context
    assert [s.title for s in latest.steps] == [
        "Switch to allorigins.win proxy",
        "Test the new endpoint",
        "Update index.html with the new URL",
    ]
    # And the pending plan id points at the new revision so the UI knows what
    # to gate on
    assert state.pending_plan_id == latest.id


async def test_ask_user_without_options_pauses_as_free_form_question():
    """When the model calls ask_user with no options (just a question), the
    loop emits the question as an assistant MessageEvent and parks at
    AWAITING_USER_DECISION — same gate, just no clickable cards. The user
    replies via send_message or steer."""
    from perpleximanus.core import ConversationStatus, MessageEvent

    free_form = action_step(
        tool="ask_user",
        args={"question": "Should I use sudo for the cleanup, or run as the current user?"},
        thought="I need to decide between two privilege levels",
    )
    agent = ScriptedAgent([free_form, finish_step()])
    loop, store = build_loop(agent)
    await loop.send_message("clean up tmp")
    await loop.run()

    state = await store.get_state(CID)
    assert state.execution_status == ConversationStatus.AWAITING_USER_DECISION
    # The question landed as an assistant message in the timeline
    events = await store.get_events(CID)
    questions = [
        e
        for e in events
        if isinstance(e, MessageEvent)
        and e.source.value == "agent"
        and "Should I use sudo" in (e.message.content if e.message else "")
    ]
    assert len(questions) == 1


# ---- auto-continue (the harness re-runs the loop instead of freezing) -------


async def test_finished_with_incomplete_plan_auto_continues_then_lands_finished():
    """The core 'never freeze' behavior: when the agent declares finished but
    the plan isn't fully marked done, the loop DOES NOT land in STUCK. It
    re-runs itself up to AUTO_CONTINUE_CAP times, injecting a continuation
    prompt each time. If the model still won't progress, the run lands
    FINISHED with a partial-plan detail — the user sees a clean ending and
    can steer if more work is wanted. Critically: the user is NEVER required
    to type something just to keep the loop moving."""
    from perpleximanus.core import (
        ActionEvent,
        ConversationStatus,
        PlanEvent,
        StatusEvent,
        ToolCall,
    )
    from perpleximanus.core import SqliteEventStore as Store
    from perpleximanus.core.llm import OperatingMode

    # Pre-seed: plan with 2 steps, only step 1 done, then RUNNING. The agent
    # script repeatedly emits finish_step (model claims done despite step 2
    # being unmarked).
    from perpleximanus.core import EventSource, LLMMessage, MessageEvent as ME

    store = Store(":memory:")
    await store.append(
        CID,
        ME(source=EventSource.USER, message=LLMMessage(role="user", content="build it")),
    )
    await store.append(
        CID, PlanEvent(summary="p", steps=[{"title": "a"}, {"title": "b"}], revision=1)
    )
    await store.append(
        CID, StatusEvent(status=ConversationStatus.RUNNING, detail="plan_approved")
    )
    # A productive action so the execution-nudge gate is satisfied (the model
    # DID do something — it's just not marking the second step done).
    await store.append(
        CID,
        ActionEvent(
            thought="wrote file",
            tool_call=ToolCall(
                tool_name="shell", arguments={"command": "echo a > /tmp/a"}
            ),
        ),
    )
    await store.append(
        CID,
        ActionEvent(
            thought="step 1 done",
            tool_call=ToolCall(
                tool_name="plan_step", arguments={"index": 1, "state": "done"}
            ),
        ),
    )

    agent = ScriptedAgent([finish_step()])  # repeats finish forever
    loop, _ = build_loop(
        agent,
        store=store,
        mode=OperatingMode.LONG_HORIZON,
    )
    # Configure planning_tools so we're in the plan-first flow.
    loop._planning_tools = frozenset({"submit_plan"})
    await loop.run()

    state = await store.get_state(CID)
    events = await store.get_events(CID)
    # Critical: the run did NOT land in STUCK. The user is not forced to type
    # something to break the freeze — the harness drove the loop forward.
    assert state.execution_status == ConversationStatus.FINISHED
    # The auto-continue StatusEvents fire exactly cap-many times before the
    # FINISHED:partial_plan landing.
    auto_continues = [
        e
        for e in events
        if isinstance(e, StatusEvent)
        and e.detail
        and e.detail.startswith("auto_continue:plan_incomplete")
    ]
    assert len(auto_continues) == loop._auto_continue_cap
    # And the terminal FINISHED carries the honest partial-plan detail.
    terminal = next(
        e
        for e in reversed(events)
        if isinstance(e, StatusEvent) and e.status == ConversationStatus.FINISHED
    )
    assert terminal.detail == "partial_plan"


async def test_auto_continue_budget_resets_on_new_user_message():
    """A fresh user prompt resets the auto-continue counter so each new
    instruction gets its own budget. Prevents one prior stoppage from
    poisoning subsequent runs."""
    from perpleximanus.core import (
        ConversationStatus,
        EventSource,
        LLMMessage,
        MessageEvent,
        StatusEvent,
    )
    from perpleximanus.core.loop.engine import AgentLoop

    # Build a synthetic event log: auto_continue fires twice, then a user
    # message arrives, then auto_continue fires once more.
    events = [
        MessageEvent(
            source=EventSource.USER, message=LLMMessage(role="user", content="hi")
        ),
        StatusEvent(
            status=ConversationStatus.RUNNING, detail="auto_continue:plan_incomplete:1"
        ),
        StatusEvent(
            status=ConversationStatus.RUNNING, detail="auto_continue:plan_incomplete:2"
        ),
        MessageEvent(
            source=EventSource.USER, message=LLMMessage(role="user", content="keep going")
        ),
        StatusEvent(
            status=ConversationStatus.RUNNING, detail="auto_continue:plan_incomplete:1"
        ),
    ]
    # The counter should reflect ONLY events since the last user message.
    assert AgentLoop._auto_continue_attempts(events) == 1


# ---- talk-back: the agent acknowledges before/while it works -----------------


async def test_planning_mode_preserves_acknowledgment_prose():
    """In PLANNING mode, a thought-only step (the agent greeting the user /
    acknowledging the request before it explores) must be RECORDED as an agent
    message, not silently discarded. Previously only the plan-nudge survived,
    so the user heard nothing back before work began."""
    from loop_fakes import AgentStep
    from perpleximanus.core import MessageEvent, ToolCall
    from perpleximanus.core.llm import OperatingMode

    ack = AgentStep(
        thought="Got it — you want a stock ticker. Let me check the APIs first.",
        tool_call=None,
        finished=False,
    )
    # After the ack the model proposes a plan via submit_plan.
    plan = AgentStep(
        thought="here's the plan",
        tool_call=ToolCall(
            tool_name="submit_plan",
            arguments={"summary": "a stock ticker", "steps": [{"title": "scaffold"}]},
        ),
        finished=False,
    )
    agent = ScriptedAgent([ack, plan])
    loop, store = build_loop(
        agent,
        mode=OperatingMode.PLANNING,
    )
    loop._planning_tools = frozenset({"submit_plan"})
    await loop.send_message("build a stock ticker")
    await loop.run()

    events = await store.get_events(CID)
    # The acknowledgment landed as an agent message (the user heard back).
    acks = [
        e
        for e in events
        if isinstance(e, MessageEvent)
        and e.source.value == "agent"
        and "stock ticker" in (e.message.content if e.message else "")
    ]
    assert len(acks) == 1


# ---- the soft plan-step nudge (the "auditor") --------------------------------


def _ev_seq(events):
    """Assign sequential seqs to a hand-built event list (the store does this in
    production; the helper is pure and reads seq)."""
    out = []
    for i, e in enumerate(events, start=1):
        out.append(e.model_copy(update={"seq": i}))
    return out


def test_plan_step_lag_signal_fires_when_work_outpaces_tracker():
    """Auditor: lots of productive actions since approval, < half the steps
    marked done, and no prior lag nudge → soft nudge warranted."""
    from perpleximanus.core import ActionEvent, PlanEvent, StatusEvent, ToolCall
    from perpleximanus.core import ConversationStatus
    from perpleximanus.core.loop.engine import AgentLoop

    def act(tool, args=None):
        return ActionEvent(thought="x", tool_call=ToolCall(tool_name=tool, arguments=args or {}))

    events = _ev_seq(
        [
            PlanEvent(summary="p", steps=[{"title": "a"}, {"title": "b"}, {"title": "c"}], revision=1),
            StatusEvent(status=ConversationStatus.RUNNING, detail="plan_approved"),
            act("file_write", {"path": "1"}),
            act("file_write", {"path": "2"}),
            act("shell", {"command": "echo"}),
            # 3 productive actions >= 3 steps; zero steps marked done.
        ]
    )
    assert AgentLoop._plan_step_lag_signal(events) is True


def test_plan_step_lag_signal_silent_when_tracker_keeps_up():
    """When at least half the steps are marked done, the tracker is keeping up
    — no nudge."""
    from perpleximanus.core import ActionEvent, PlanEvent, StatusEvent, ToolCall
    from perpleximanus.core import ConversationStatus
    from perpleximanus.core.loop.engine import AgentLoop

    def act(tool, args=None):
        return ActionEvent(thought="x", tool_call=ToolCall(tool_name=tool, arguments=args or {}))

    events = _ev_seq(
        [
            PlanEvent(summary="p", steps=[{"title": "a"}, {"title": "b"}], revision=1),
            StatusEvent(status=ConversationStatus.RUNNING, detail="plan_approved"),
            act("file_write", {"path": "1"}),
            act("plan_step", {"index": 1, "state": "done"}),
            act("file_write", {"path": "2"}),
        ]
    )
    # 2 productive >= 2 steps, but 1 of 2 steps done (>= half) → no nudge.
    assert AgentLoop._plan_step_lag_signal(events) is False


def test_plan_step_lag_signal_fires_once_per_episode():
    """After a lag nudge fires, it must not re-fire until the agent checks off
    another step (otherwise it would nag every iteration)."""
    from perpleximanus.core import (
        ActionEvent,
        EventSource,
        LLMMessage,
        MessageEvent,
        PlanEvent,
        StatusEvent,
        ToolCall,
    )
    from perpleximanus.core import ConversationStatus
    from perpleximanus.core.loop.engine import AgentLoop

    def act(tool, args=None):
        return ActionEvent(thought="x", tool_call=ToolCall(tool_name=tool, arguments=args or {}))

    nudge = MessageEvent(
        source=EventSource.ENVIRONMENT,
        message=LLMMessage(
            role="user",
            content="<system-reminder>\nGentle note: the plan-step tracker is behind.\n</system-reminder>",
        ),
    )
    events = _ev_seq(
        [
            PlanEvent(summary="p", steps=[{"title": "a"}, {"title": "b"}, {"title": "c"}], revision=1),
            StatusEvent(status=ConversationStatus.RUNNING, detail="plan_approved"),
            act("file_write", {"path": "1"}),
            act("file_write", {"path": "2"}),
            act("shell", {"command": "echo"}),
            nudge,  # the soft nudge already fired
            act("file_write", {"path": "3"}),  # more work, still no check-off
        ]
    )
    # The nudge is more recent than the last plan_step (there is none) → silent.
    assert AgentLoop._plan_step_lag_signal(events) is False
