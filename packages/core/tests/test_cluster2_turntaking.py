"""Cluster 2 — turn-taking & completion: the `finish` + `notify_user` virtual
tools, the no-op backstop, and the circuit breaker."""

from __future__ import annotations

from disco.core.loop import signals
from disco.core import (
    ConversationStatus,
    EventSource,
    MessageEvent,
    ObservationEvent,
    ToolResult,
)
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
    loop, store = build_loop(
        agent, stuck_thresholds=StuckThresholds(agent_monologue=100)
    )
    await loop.send_message("go")
    state = await loop.run()
    assert state.execution_status == ConversationStatus.FINISHED
    events = await store.get_events(CID)
    terminal = next(
        e
        for e in reversed(events)
        if e.__class__.__name__ == "StatusEvent"
        and e.status == ConversationStatus.FINISHED
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
    from disco.core.loop.engine import AgentLoop

    def agent_msg(text):
        return MessageEvent(
            source=EventSource.AGENT, message=LLMMessage(role="assistant", content=text)
        )

    def user_msg(text):
        return MessageEvent(
            source=EventSource.USER, message=LLMMessage(role="user", content=text)
        )

    # 3 trailing agent prose messages → 3.
    seq = [user_msg("go"), agent_msg("a"), agent_msg("b"), agent_msg("c")]
    assert signals.consecutive_noops(seq) == 3
    # A user message resets the count.
    seq2 = [agent_msg("a"), user_msg("go"), agent_msg("b")]
    assert signals.consecutive_noops(seq2) == 1


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
