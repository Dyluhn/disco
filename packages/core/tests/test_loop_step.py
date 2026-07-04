"""One-action-per-iteration + execute-and-observe pairing — §10.2, §10.3."""

from __future__ import annotations

from disco.core import (
    ActionEvent,
    AgentErrorEvent,
    ObservationEvent,
    ToolResult,
)
from disco.core.loop import signals
from loop_fakes import (
    FakeExecutor,
    ScriptedAgent,
    action_step,
    assert_blocked_question_landing,
    build_loop,
    finish_step,
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
    from disco.core import SqliteEventStore, ToolCall

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
    from disco.core import EventSource, MessageEvent

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
    decides human input is needed.

    C7 (build-surface-recovery-ux) — EXCEPTION for the existing stuck-escape
    path: when the StuckDetector detects 3 identical action→error repeats it
    fires the ONE-reframe escape (a status marker + a rotating C7 reminder +
    a high temperature), exactly ONCE per user turn, BEFORE the run halts
    STUCK. The C7 reminder lives INSIDE that escape branch — it does NOT
    violate the c97c1b3 "no automatic nudge outside the existing
    stuck-escape" invariant. So this test now distinguishes between
    C7-bounded escape reminders (allowed) and any OTHER automatic
    failure-recovery reminders (still forbidden).
    """
    failing = ToolResult(call_id="c", tool_name="shell", success=False, content="", error="bad")
    agent = ScriptedAgent(
        [action_step(), action_step(), action_step(), action_step(), finish_step()]
    )
    loop, store = build_loop(agent, executor=FakeExecutor(result=failing))
    await loop.send_message("go")
    await loop.run()
    events = await store.get_events(CID)
    all_reminders = await _reminders(events)
    # Filter to reminders that are NOT one of the TWO sanctioned host
    # reminders: (1) the c97c1b3-bounded C7 escape reminder, and (2) the
    # terminal-collapse blocked-landing prompt ("You are blocked because…"),
    # which is host-initiated, breaker-bounded, and BY DESIGN the mechanism
    # that converts a dead-end into an explain+ask landing. Any other
    # <system-reminder> is a regression of the no-automatic-nudge invariant.
    non_escape_reminders = [
        e for e in all_reminders
        if "disco:escape-attempt=" not in (e.message.content or "")
        and "You are blocked because" not in (e.message.content or "")
    ]
    assert non_escape_reminders == [], (
        "the loop must NOT emit automatic failure-recovery reminders "
        "OUTSIDE the c97c1b3-bounded stuck-escape path. The C7 escape "
        "reminder is the one allowed exception (it is emitted from inside "
        "the existing stuck-escape branch with a rotating pool + a "
        "per-attempt serialization nonce). Got non-escape reminders: "
        f"{[e.message.content for e in non_escape_reminders]}"
    )
    # And the C7 escape path MUST have fired (3 identical failures is at the
    # StuckDetector's `repeat_action_error` threshold). This is the
    # positive half of the assertion: the escape reminder IS present
    # (the old assertion was too strict; the new design explicitly adds it).
    escape_reminders = [
        e for e in all_reminders
        if "disco:escape-attempt=" in (e.message.content or "")
    ]
    assert len(escape_reminders) >= 1, (
        "expected the c97c1b3-bounded C7 escape reminder to fire after "
        f"{len([e for e in events if isinstance(e, AgentErrorEvent)])} "
        "consecutive failures, got 0"
    )


def test_plan_is_incomplete_helper_recognizes_partial_completion():
    """Unit test the plan-completeness gate's truth source. If a plan exists and
    not every step has been marked plan_step(idx, "done"), the helper returns
    (True, [missing_indices]) — which the FINISHED transition uses to fall back
    to STUCK with a reminder naming the gap."""
    from disco.core import ActionEvent, PlanEvent, ToolCall

    # Plan with 3 steps; only step 1 marked done.
    events = [
        PlanEvent(summary="p", steps=[{"title": "a"}, {"title": "b"}, {"title": "c"}], revision=1),
        ActionEvent(
            thought="step 1 done",
            tool_call=ToolCall(tool_name="plan_step", arguments={"index": 1, "state": "done"}),
        ),
    ]
    incomplete, missing = signals.plan_is_incomplete(events)
    assert incomplete is True
    assert missing == [2, 3]


def test_plan_is_incomplete_helper_passes_when_all_steps_done():
    """When every step is marked done, the gate clears — FINISHED is allowed."""
    from disco.core import ActionEvent, PlanEvent, ToolCall

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
    incomplete, missing = signals.plan_is_incomplete(events)
    assert incomplete is False
    assert missing == []


def test_plan_is_incomplete_helper_inert_without_plan():
    """No plan at all → no progress signal to gate on → not incomplete. The gate
    is a no-op for non-plan-first flows (Build runs without an explicit plan)."""

    incomplete, missing = signals.plan_is_incomplete([])
    assert incomplete is False
    assert missing == []


def test_plan_is_incomplete_helper_uses_latest_revision():
    """A re-plan (revision bump) replaces prior — the gate must check completeness
    against the LATEST plan, not the original."""
    from disco.core import ActionEvent, PlanEvent, ToolCall

    # Sequenced events (production always assigns seqs) so the re-plan boundary is
    # unambiguous — the unified reader scopes per-step state to AFTER the latest plan.
    events = [
        PlanEvent(summary="p1", steps=[{"title": "a"}], revision=1, seq=1),
        # All steps of plan #1 done.
        ActionEvent(
            thought="1",
            tool_call=ToolCall(tool_name="plan_step", arguments={"index": 1, "state": "done"}),
            seq=2,
        ),
        # Re-plan with 2 steps; neither marked done yet.
        PlanEvent(summary="p2", steps=[{"title": "a"}, {"title": "b"}], revision=2, seq=3),
    ]
    incomplete, missing = signals.plan_is_incomplete(events)
    # plan #2 is the latest. A re-plan starts a FRESH checklist: p1's done mark (before
    # p2's seq) must NOT carry forward, so BOTH of p2's steps are unmarked. (This is the
    # corrected behavior — the prior seqless fixture let p1's mark leak into p2.)
    assert incomplete is True
    assert missing == [1, 2]


# ---- the model-chosen ask_user → AWAITING_USER_DECISION gate ----------------


async def test_ask_user_intercepts_and_halts_at_decision_gate():
    """When the agent VOLUNTARILY emits an `ask_user` tool call (the model's
    own decision after reasoning, not a harness-funneled reminder), the loop
    intercepts it (the tool is never executed), builds an AlternativesEvent
    from the structured options, and halts at AWAITING_USER_DECISION until
    the user picks one. This is the model-driven escape hatch."""
    from disco.core import AlternativesEvent, ConversationStatus

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
    # Real work first — the fresh-session backstop refuses a zero-work ask_user.
    agent = ScriptedAgent([action_step("shell", {}), alt_call, finish_step()])
    loop, store = build_loop(agent)
    await loop.send_message("clean up tmp")
    await loop.run()
    state = await store.get_state(CID)
    assert state.execution_status == ConversationStatus.AWAITING_USER_DECISION
    # The AlternativesEvent landed with both options + the pending id correlates
    events = await store.get_events(CID)
    alts = [e for e in events if isinstance(e, AlternativesEvent)]
    assert len(alts) == 1
    # The agent's two options + the always-appended "Continue anyway" bypass (D2).
    assert [o.id for o in alts[0].options] == ["a", "b", "__continue__"]
    assert alts[0].options[-1].title == "Continue anyway"
    assert alts[0].options[0].tool_name == "shell"
    assert state.pending_alternatives_id == alts[0].id


async def test_pick_alternative_runs_the_selected_option():
    """User picks option 'a' → the loop synthesizes an ActionEvent from option
    'a's tool_call (shell with sudo) → executes it → returns to RUNNING."""
    from disco.core import (
        ActionEvent,
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
    # Real work first — the fresh-session backstop refuses a zero-work ask_user.
    agent = ScriptedAgent([action_step("shell", {}), alt_call, finish_step()])
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


class _SudoHighAnalyzer:
    """Scope-free risk double: HIGH iff the action's shell command is `sudo true`
    (the picked option), LOW otherwise. Lets the warm-up real-work step through
    (so the run reaches the ask_user gate) while flagging the picked alternative,
    so the test isolates the pick path's gating — using the SAME injected-analyzer
    seam the normal tool-call path uses."""

    def assess(self, action):
        from disco.core import SecurityRisk

        tc = action.tool_call
        cmd = (tc.arguments.get("command") if tc else "") or ""
        return SecurityRisk.HIGH if "sudo true" in cmd else SecurityRisk.LOW


def _risky_alt_agent():
    """A scripted agent that does real work, then offers an ask_user gate with a
    RUNNABLE option 'a' (shell `sudo true`) and a benign option 'b'."""
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
    # Real work first — the fresh-session backstop refuses a zero-work ask_user.
    return ScriptedAgent([action_step("shell", {}), alt_call, finish_step()])


async def _drive_to_risky_pick_gate(policy, analyzer):
    """Run to the AWAITING_USER_DECISION gate, then pick the HIGH-risk option 'a'.
    Returns (loop, store)."""
    loop, store = build_loop(_risky_alt_agent(), policy=policy, analyzer=analyzer)
    await loop.send_message("clean up")
    await loop.run()
    from disco.core import ConversationStatus

    assert (
        await store.get_state(CID)
    ).execution_status == ConversationStatus.AWAITING_USER_DECISION
    await loop.pick_alternative("a")
    return loop, store


async def test_pick_high_risk_alternative_is_gated_not_executed() -> None:
    """SECURITY (close BlastRadiusConfirm bypass): picking an alternative whose
    tool_name is a confirm-required (HIGH-risk) tool must route through the SAME
    risk-confirm gate a direct call would hit — emitting WAITING_FOR_CONFIRMATION
    and NOT executing the tool until the user confirms.

    Pre-fix this FAILS: pick_alternative executed the synthesized action directly
    via _execute_and_observe, bypassing _gate_risk_confirm entirely."""
    from disco.core import ActionEvent, ConversationStatus, ObservationEvent
    from disco.core.loop import ConfirmRisky

    loop, store = await _drive_to_risky_pick_gate(
        ConfirmRisky(), _SudoHighAnalyzer()
    )

    state = await store.get_state(CID)
    # Parked on the confirm gate — NOT resumed to RUNNING, NOT finished.
    assert state.execution_status == ConversationStatus.WAITING_FOR_CONFIRMATION

    events = await store.get_events(CID)
    # The synthesized (PROPOSED) action for option 'a' was recorded...
    synthesized = [
        e
        for e in events
        if isinstance(e, ActionEvent)
        and e.tool_call is not None
        and e.tool_call.tool_name == "shell"
        and e.tool_call.arguments.get("command") == "sudo true"
    ]
    assert len(synthesized) == 1
    # ...and the confirm gate points at it.
    assert state.pending_action_id == synthesized[0].id
    # CRITICAL: it did NOT execute — no observation, executor never ran `sudo true`.
    assert [
        e
        for e in events
        if isinstance(e, ObservationEvent) and e.action_id == synthesized[0].id
    ] == []
    assert all(
        c.arguments.get("command") != "sudo true" for c in loop.executor.calls
    )


async def test_pick_high_risk_alternative_confirm_then_executes() -> None:
    """After the gated pick, confirm() executes EXACTLY the pending option —
    identical to confirming a direct risky tool call."""
    from disco.core import ObservationEvent
    from disco.core.loop import ConfirmRisky

    loop, store = await _drive_to_risky_pick_gate(
        ConfirmRisky(), _SudoHighAnalyzer()
    )
    pending_id = (await store.get_state(CID)).pending_action_id

    await loop.confirm()

    events = await store.get_events(CID)
    # The pending option now executed (observation paired to the proposed action).
    assert [
        e for e in events if isinstance(e, ObservationEvent) and e.action_id == pending_id
    ]
    assert any(
        c.arguments.get("command") == "sudo true" for c in loop.executor.calls
    )


async def test_pick_high_risk_alternative_reject_does_not_execute() -> None:
    """Declining the gated pick records the denial and does NOT execute the
    option — same as rejecting a direct risky tool call."""
    from disco.core import AgentErrorEvent, ConversationStatus, ObservationEvent
    from disco.core.loop import ConfirmRisky

    loop, store = await _drive_to_risky_pick_gate(
        ConfirmRisky(), _SudoHighAnalyzer()
    )
    pending_id = (await store.get_state(CID)).pending_action_id

    await loop.reject("not approving sudo")

    events = await store.get_events(CID)
    # Denial recorded against the proposed action; it never executed.
    assert [
        e
        for e in events
        if isinstance(e, AgentErrorEvent) and e.action_id == pending_id
    ]
    assert [
        e for e in events if isinstance(e, ObservationEvent) and e.action_id == pending_id
    ] == []
    assert all(
        c.arguments.get("command") != "sudo true" for c in loop.executor.calls
    )
    assert (await store.get_state(CID)).execution_status == ConversationStatus.RUNNING


async def test_pick_low_risk_alternative_executes_on_pick_no_regression() -> None:
    """No regression: under the SAME confirm-on-HIGH policy, a LOW-risk picked
    alternative is NOT gated — it executes immediately on pick, preserving the
    existing pick UX for non-risky tools."""
    from disco.core import ActionEvent, ConversationStatus, ObservationEvent, SecurityRisk
    from disco.core.loop import ConfirmRisky
    from loop_fakes import FakeAnalyzer

    loop, store = await _drive_to_risky_pick_gate(
        ConfirmRisky(), FakeAnalyzer(SecurityRisk.LOW)
    )

    # LOW < HIGH threshold → no gate; the option ran straight away.
    state = await store.get_state(CID)
    assert state.execution_status != ConversationStatus.WAITING_FOR_CONFIRMATION
    events = await store.get_events(CID)
    synthesized = [
        e
        for e in events
        if isinstance(e, ActionEvent)
        and e.tool_call is not None
        and e.tool_call.arguments.get("command") == "sudo true"
    ]
    assert len(synthesized) == 1
    assert [
        e
        for e in events
        if isinstance(e, ObservationEvent) and e.action_id == synthesized[0].id
    ]
    assert any(
        c.arguments.get("command") == "sudo true" for c in loop.executor.calls
    )


async def test_pick_hard_denied_alternative_is_refused_not_executed() -> None:
    """SECURITY (close `rm -rf /` bypass): picking an alternative whose tool is
    HARD-DENIED must be REFUSED before the confirm gate — exactly like a direct
    call of a catastrophic command. The command must NOT execute (no observation,
    executor never ran it), a REFUSED error must be emitted so the agent adapts,
    and the proposed action must still be recorded for audit.

    Pre-fix this FAILS: pick_alternative only threaded _gate_risk_confirm, so a
    hard-denied picked command executed directly via _execute_and_observe."""
    from disco.core import ActionEvent, AgentErrorEvent, ConversationStatus, ObservationEvent

    alt_call = action_step(
        tool="ask_user",
        args={
            "summary": "the build dir is wedged",
            "options": [
                {
                    "id": "a",
                    "title": "Nuke everything",
                    "description": "wipe the root",
                    "tool_name": "shell",
                    "arguments": {"command": "rm -rf /"},
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
    # Real work first (fresh-session backstop); after the refusal the agent's next
    # scripted step cleanly finishes. Hard-deny is signature-based (signals.
    # hard_deny_reason), independent of the injected analyzer/policy — so the
    # default build_loop wiring is the same one the normal-path hard-deny tests use.
    agent = ScriptedAgent([action_step("shell", {}), alt_call, finish_step()])
    loop, store = build_loop(agent)
    await loop.send_message("clean up")
    await loop.run()
    assert (
        await store.get_state(CID)
    ).execution_status == ConversationStatus.AWAITING_USER_DECISION

    await loop.pick_alternative("a")

    events = await store.get_events(CID)
    # The proposed action is recorded for audit...
    synthesized = [
        e
        for e in events
        if isinstance(e, ActionEvent)
        and e.tool_call is not None
        and e.tool_call.arguments.get("command") == "rm -rf /"
    ]
    assert len(synthesized) == 1
    # ...but it NEVER executed (no observation, executor never saw `rm -rf /`).
    assert [
        e
        for e in events
        if isinstance(e, ObservationEvent) and e.action_id == synthesized[0].id
    ] == []
    assert all(c.arguments.get("command") != "rm -rf /" for c in loop.executor.calls)
    # A REFUSED/hard-denied error was emitted so the agent sees it and adapts.
    refusals = [
        e
        for e in events
        if isinstance(e, AgentErrorEvent)
        and "REFUSED" in e.error
        and "hard-denied" in e.error
    ]
    assert len(refusals) == 1
    # The loop resumed (never parked at the confirm gate) and finished.
    assert (await store.get_state(CID)).execution_status == ConversationStatus.FINISHED


async def test_pick_label_only_alternative_replies_with_the_label_no_tool() -> None:
    """BW-03 end-to-end: an option with NO tool_name is the user's ANSWER, not a
    runnable action. Picking it injects the option's label as a USER reply and
    resumes the conversation — it must NOT synthesize an empty-tool ActionEvent
    (which would error / do nothing) and must NOT raise."""
    from disco.core import (
        ActionEvent,
        AgentErrorEvent,
        AlternativesEvent,
        ConversationStatus,
        MessageEvent,
    )

    alt_call = action_step(
        tool="ask_user",
        args={
            "summary": "which direction?",
            "options": [
                # LABEL-ONLY: a plain-language path with no tool_name.
                {"id": "a", "title": "Use approach A", "description": "the simpler path"},
                {"id": "b", "title": "Use approach B", "description": "the thorough path"},
            ],
        },
    )
    # Real work first (fresh-session backstop), then the ask_user gate, then the
    # model's next step after the pick cleanly finishes the run.
    agent = ScriptedAgent([action_step("shell", {}), alt_call, finish_step()])
    loop, store = build_loop(agent)
    await loop.send_message("decide the approach")
    await loop.run()
    assert (
        await store.get_state(CID)
    ).execution_status == ConversationStatus.AWAITING_USER_DECISION
    # The gate kept the label-only options (no tool_name) — the BW-03 plans.py fix.
    events = await store.get_events(CID)
    alts = [e for e in events if isinstance(e, AlternativesEvent)]
    assert alts and alts[-1].options[0].tool_name == ""

    n_actions_before = len([e for e in events if isinstance(e, ActionEvent)])

    await loop.pick_alternative("a")

    events = await store.get_events(CID)
    # The pick became a USER reply carrying the option's label (the answer),
    # NOT a tool execution.
    user_replies = [
        e
        for e in events
        if isinstance(e, MessageEvent)
        and e.message is not None
        and "Use approach A" in e.message.content
    ]
    assert len(user_replies) == 1
    # NO empty-tool ActionEvent was ever synthesized for the label-only pick.
    empty_tool_actions = [
        e
        for e in events
        if isinstance(e, ActionEvent)
        and e.tool_call is not None
        and not (e.tool_call.tool_name or "").strip()
    ]
    assert empty_tool_actions == []
    # No error surfaced from the pick.
    assert [e for e in events if isinstance(e, AgentErrorEvent)] == []
    # The conversation CONTINUED with that answer (the scripted finish step ran).
    assert (await store.get_state(CID)).execution_status == ConversationStatus.FINISHED
    # The only new ActionEvents after the pick are the agent's own next steps —
    # none synthesized from the label-only option.
    n_actions_after = len([e for e in events if isinstance(e, ActionEvent)])
    # finish_step may or may not emit an action; the invariant is no EMPTY-tool one.
    assert n_actions_after >= n_actions_before


async def test_pick_alternative_with_unknown_id_does_not_resume():
    """Defensive: a bad pick (stale id, wrong user) doesn't crash — it leaves
    the gate intact + emits a reminder so the user can try again."""
    from disco.core import ConversationStatus, MessageEvent

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
    # Real work first — the fresh-session backstop refuses a zero-work ask_user.
    agent = ScriptedAgent([action_step("shell", {}), alt_call, finish_step()])
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
    from disco.core import ConversationStatus, PlanEvent

    update_call = action_step(
        tool="propose_plan_update",
        args={
            "summary": "the original CORS-proxy path doesn't work; using a different API instead",
            "steps": [
                {"title": "Switch to allorigins.win proxy"},
                {"title": "Test the new endpoint"},
                {"title": "Update index.html with the new URL"},
            ],
            "context": (
                "yahoo.com returns 403 via corsproxy.io; allorigins is the working alt"
            ),
        },
        thought="my current plan is wrong; here's a course correction",
    )
    # Real work first — the fresh-session backstop refuses a zero-work re-plan.
    agent = ScriptedAgent([action_step("shell", {}), update_call, finish_step()])
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
    loop emits the question as an assistant MessageEvent and parks at the
    two-way Ask-gate (AWAITING_USER_QUESTION) — a free-form question, not the
    pick-a-card AWAITING_USER_DECISION gate. The user replies via send_message
    or steer. `pending_question_id` resolves to the question message so the UI
    can render it in the AskPanel."""
    from disco.core import ConversationStatus, MessageEvent

    free_form = action_step(
        tool="ask_user",
        args={"question": "Should I use sudo for the cleanup, or run as the current user?"},
        thought="I need to decide between two privilege levels",
    )
    # Real work first — the fresh-session backstop refuses a zero-work ask_user.
    agent = ScriptedAgent([action_step("shell", {}), free_form, finish_step()])
    loop, store = build_loop(agent)
    await loop.send_message("clean up tmp")
    await loop.run()

    state = await store.get_state(CID)
    assert state.execution_status == ConversationStatus.AWAITING_USER_QUESTION
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
    # the gate's pending id points at the question message (not a sentinel)
    assert state.pending_question_id == questions[0].id


# ---- auto-continue (the harness re-runs the loop instead of freezing) -------


async def test_finished_with_unmarked_plan_lands_finished_cleanly():
    """runthru-v2 (#3): plan-step bookkeeping no longer gates finish. When the
    agent declares finished AFTER doing real work, the loop lands FINISHED
    immediately — even if plan steps were never marked done. EVERY model tier
    under-reports per-step progress, so the old auto-continue-on-incomplete-plan
    bounce (≈3× rework, then FINISHED:partial_plan) was removed; completion is
    gated by the real verify gates (execution-nudge / DoD), not bookkeeping.
    Critically: still NO STUCK freeze AND now no bookkeeping bounce — the model
    did productive work (a shell action), so the execution gate is satisfied."""
    # Pre-seed: plan with 2 steps, only step 1 done, then RUNNING. The agent
    # script repeatedly emits finish_step (model claims done despite step 2
    # being unmarked).
    from disco.core import (
        ActionEvent,
        ConversationStatus,
        EventSource,
        LLMMessage,
        ObservationEvent,
        PlanEvent,
        StatusEvent,
        ToolCall,
        ToolResult,
    )
    from disco.core import MessageEvent as ME
    from disco.core import SqliteEventStore as Store
    from disco.core.llm import OperatingMode

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
    shell_action = ActionEvent(
        thought="wrote file",
        tool_call=ToolCall(tool_name="shell", arguments={"command": "echo a > /tmp/a"}),
    )
    await store.append(CID, shell_action)
    await store.append(
        CID,
        ObservationEvent(
            tool_result=ToolResult(
                call_id=shell_action.tool_call.call_id,
                tool_name="shell",
                success=True,
                content="ok",
            ),
            action_id=shell_action.id,
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
    # NO auto-continue bounce — finish is no longer gated on plan_step marks.
    auto_continues = [
        e
        for e in events
        if isinstance(e, StatusEvent)
        and e.detail
        and e.detail.startswith("auto_continue:plan_incomplete")
    ]
    assert auto_continues == []
    # Clean FINISHED — NOT the old "partial_plan" bookkeeping taxonomy.
    terminal = next(
        e
        for e in reversed(events)
        if isinstance(e, StatusEvent) and e.status == ConversationStatus.FINISHED
    )
    assert terminal.detail != "partial_plan"


async def test_auto_continue_budget_resets_on_new_user_message():
    """A fresh user prompt resets the auto-continue counter so each new
    instruction gets its own budget. Prevents one prior stoppage from
    poisoning subsequent runs."""
    from disco.core import (
        ConversationStatus,
        EventSource,
        LLMMessage,
        MessageEvent,
        StatusEvent,
    )

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
    assert signals.auto_continue_attempts(events) == 1


# ---- talk-back: the agent acknowledges before/while it works -----------------


async def test_planning_mode_preserves_acknowledgment_prose():
    """In PLANNING mode, a thought-only step (the agent greeting the user /
    acknowledging the request before it explores) must be RECORDED as an agent
    message, not silently discarded. Previously only the plan-nudge survived,
    so the user heard nothing back before work began."""
    from disco.core import MessageEvent, ToolCall
    from disco.core.llm import OperatingMode
    from loop_fakes import AgentStep

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


async def test_planning_mode_can_ask_user_before_proposing_a_plan():
    """An under-specified task most needs clarification BEFORE a plan is
    committed. In PLANNING mode the model can call `ask_user` (free-form) to get a
    detail only the user knows; the loop halts at AWAITING_USER_QUESTION instead of
    forcing a guessed plan. (Regression for the live finding: the planner WANTED to
    ask for the brand color but was forced to submit_plan.)"""
    from disco.core import ConversationStatus, MessageEvent, ToolCall
    from disco.core.llm import OperatingMode
    from loop_fakes import AgentStep

    ask = AgentStep(
        thought="The task names an exact brand color I don't have — I should ask.",
        tool_call=ToolCall(
            tool_name="ask_user",
            arguments={"question": "What is your brand's exact primary color (hex)?"},
        ),
        finished=False,
    )
    agent = ScriptedAgent([ask, finish_step()])
    loop, store = build_loop(agent, mode=OperatingMode.PLANNING)
    loop._planning_tools = frozenset({"submit_plan"})

    # ask_user is offered to the planner now (not just in execution).
    tool_names = {getattr(t, "name", None) for t in loop._tools_for_step()}
    assert "ask_user" in tool_names

    await loop.send_message("Build a landing page using my brand's exact primary color")
    await loop.run()

    state = await store.get_state(CID)
    # the planner asked → halted at the free-form Ask-gate (NOT a forced plan)
    assert state.execution_status == ConversationStatus.AWAITING_USER_QUESTION
    events = await store.get_events(CID)
    questions = [
        e
        for e in events
        if isinstance(e, MessageEvent)
        and e.source.value == "agent"
        and "primary color" in (e.message.content if e.message else "")
    ]
    assert len(questions) == 1
    assert state.pending_question_id == questions[0].id
    # and no plan was committed on a guess
    from disco.core import PlanEvent

    assert not any(isinstance(e, PlanEvent) for e in events)


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
    from disco.core import ActionEvent, ConversationStatus, PlanEvent, StatusEvent, ToolCall

    def act(tool, args=None):
        return ActionEvent(thought="x", tool_call=ToolCall(tool_name=tool, arguments=args or {}))

    events = _ev_seq(
        [
            PlanEvent(
                summary="p",
                steps=[{"title": "a"}, {"title": "b"}, {"title": "c"}],
                revision=1,
            ),
            StatusEvent(status=ConversationStatus.RUNNING, detail="plan_approved"),
            act("file_write", {"path": "1"}),
            act("file_write", {"path": "2"}),
            act("shell", {"command": "echo"}),
            # 3 productive actions >= 3 steps; zero steps marked done.
        ]
    )
    assert signals.plan_step_lag_signal(events) is True


def test_plan_step_lag_signal_silent_when_tracker_keeps_up():
    """When at least half the steps are marked done, the tracker is keeping up
    — no nudge."""
    from disco.core import ActionEvent, ConversationStatus, PlanEvent, StatusEvent, ToolCall

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
    assert signals.plan_step_lag_signal(events) is False


def test_plan_step_lag_signal_fires_once_per_episode():
    """After a lag nudge fires, it must not re-fire until the agent checks off
    another step (otherwise it would nag every iteration)."""
    from disco.core import (
        ActionEvent,
        ConversationStatus,
        EventSource,
        LLMMessage,
        MessageEvent,
        PlanEvent,
        StatusEvent,
        ToolCall,
    )

    def act(tool, args=None):
        return ActionEvent(thought="x", tool_call=ToolCall(tool_name=tool, arguments=args or {}))

    nudge = MessageEvent(
        source=EventSource.ENVIRONMENT,
        message=LLMMessage(
            role="user",
            content=(
                "<system-reminder>\nGentle note: the plan-step tracker is behind.\n"
                "</system-reminder>"
            ),
        ),
    )
    events = _ev_seq(
        [
            PlanEvent(
                summary="p",
                steps=[{"title": "a"}, {"title": "b"}, {"title": "c"}],
                revision=1,
            ),
            StatusEvent(status=ConversationStatus.RUNNING, detail="plan_approved"),
            act("file_write", {"path": "1"}),
            act("file_write", {"path": "2"}),
            act("shell", {"command": "echo"}),
            nudge,  # the soft nudge already fired
            act("file_write", {"path": "3"}),  # more work, still no check-off
        ]
    )
    # The nudge is more recent than the last plan_step (there is none) → silent.
    assert signals.plan_step_lag_signal(events) is False


def test_productive_gate_rejects_read_only_then_finish():
    """The build-finish gate must require a STATE-CHANGING action since plan
    approval — reading the files and declaring done delivers nothing (caught live:
    a build iteration 'finished' after only file_reads with zero edits)."""
    from disco.core import ActionEvent, ObservationEvent, StatusEvent, ToolCall, ToolResult
    from disco.core.events import ConversationStatus

    def _seqd(evs):
        return [e.model_copy(update={"seq": i}) for i, e in enumerate(evs, 1)]

    approved = StatusEvent(status=ConversationStatus.RUNNING, detail="plan_approved")
    rd = ActionEvent(
        thought="read it", tool_call=ToolCall(tool_name="file_read", arguments={"path": "i.html"})
    )
    wr = ActionEvent(
        thought="edit it",
        tool_call=ToolCall(tool_name="file_write", arguments={"path": "i.html", "content": "x"}),
    )
    wr_obs = ObservationEvent(
        tool_result=ToolResult(
            call_id=wr.tool_call.call_id,
            tool_name="file_write",
            success=True,
            content="ok",
        ),
        action_id=wr.id,
    )

    # runthru-v2 (#3): the declarative progress tool is pure bookkeeping — calling
    # it after approval must NOT look like real work (else approve→update_plan_progress
    # →finish would land FINISHED with zero workspace mutation, bypassing the gate).
    upp = ActionEvent(
        thought="report progress",
        tool_call=ToolCall(
            tool_name="update_plan_progress",
            arguments={"steps": [{"index": 1, "state": "done"}]},
        ),
    )

    # only reads after approval → NOT productive (finish would be refused)
    assert signals.productive_action_since_approval(_seqd([approved, rd, rd])) is False
    # progress-snapshot-only after approval → NOT productive (the #3 regression)
    assert signals.productive_action_since_approval(_seqd([approved, upp, upp])) is False
    # a write after approval → productive (finish allowed)
    assert signals.productive_action_since_approval(_seqd([approved, rd, wr, wr_obs])) is True
    # a write still counts even if a progress snapshot follows it
    assert signals.productive_action_since_approval(_seqd([approved, wr, wr_obs, upp])) is True
    # no plan-approval marker → gate inert (don't block)
    assert signals.productive_action_since_approval(_seqd([rd])) is True


async def test_execution_nudge_without_action_lands_instead_of_livelocking():
    """REGRESSION (confirm/reject livelock, Defect B): when the plan is COMPLETE
    (every step marked done) yet NO productive action happened since approval, a
    model that keeps declaring `finish` must NOT spin forever on the execution
    gate. The plan-completeness auto-continue ladder is inert here (nothing is
    missing). W5 added an explicit cap (_EXECUTION_NUDGE_CAP = 3): after 3 nudges
    the gate terminalizes STUCK:approve_plan_no_execution directly and halts —
    a plan approved but never executed is a terminal FAILURE per §11.2, NOT a
    false FINISHED.

    Inverse proof: the script caps at an `LLMError` sentinel after 20 finish
    attempts, so a REVERTED backstop (bare `continue`) terminates into a
    model_error ERROR state. The FIXED path lands STUCK in ~4 turns (3 nudges +
    1 cap terminal), well before the sentinel cap."""
    from disco.core import ActionEvent as AE
    from disco.core import (
        ConversationStatus,
        EventSource,
        LLMMessage,
        PlanEvent,
        StatusEvent,
        ToolCall,
    )
    from disco.core import MessageEvent as ME
    from disco.core import SqliteEventStore as Store
    from disco.core.llm import OperatingMode
    from disco.core.llm.errors import LLMError

    store = Store(":memory:")
    await store.append(
        CID,
        ME(source=EventSource.USER, message=LLMMessage(role="user", content="build it")),
    )
    await store.append(
        CID, PlanEvent(summary="p", steps=[{"title": "only step"}], revision=1)
    )
    await store.append(
        CID, StatusEvent(status=ConversationStatus.RUNNING, detail="plan_approved")
    )
    # The plan is fully marked done (so _plan_is_incomplete is False and the
    # auto-continue ladder never engages) — but plan_step is non-productive, so
    # NOTHING productive has happened since approval. This is the exact livelock
    # shape: the publish action was swallowed upstream (Defect A) and the only
    # post-approval action is the bookkeeping mark.
    await store.append(
        CID,
        AE(
            thought="step 1 done",
            tool_call=ToolCall(
                tool_name="plan_step", arguments={"index": 1, "state": "done"}
            ),
        ),
    )

    # finish forever, then a hard LLMError cap so a broken loop can't hang.
    from disco.core.loop.finish import _EXECUTION_NUDGE_CAP

    agent = ScriptedAgent([finish_step()] * 20 + [LLMError("spin-cap reached")])
    loop, _ = build_loop(agent, store=store, mode=OperatingMode.LONG_HORIZON)
    loop._planning_tools = frozenset({"submit_plan"})
    await loop.run()

    state = await store.get_state(CID)
    events = await store.get_events(CID)
    # Landed cleanly via the W5 execution-nudge cap — NOT spun to the LLMError cap.
    # The cap explains/asks with approve_plan_no_execution directly (bypassing the
    # noop valve) so the run lands in bounded turns — a false FINISHED here would
    # be the APPROVE_PLAN_NO_EXECUTION bug.
    assert state.execution_status == ConversationStatus.AWAITING_USER_QUESTION
    assert_blocked_question_landing(events, legacy_detail="approve_plan_no_execution")
    # The execution gate actually fired (the path under test ran)...
    assert loop._execution_nudges >= _EXECUTION_NUDGE_CAP
    # ...and it terminated promptly — well short of the 20-finish LLMError cap.
    assert agent.calls < 15
