"""The `remember` virtual tool — durable, condensation-proof memory.

When the agent calls `remember`, the loop intercepts it (never executed by the
executor) and emits a PINNED KnowledgeEvent. Pinned = exempt from condensation,
so the fact survives a long run and is re-injected into context every step. The
call is non-blocking: the run continues, exactly like notify_user.
"""

from __future__ import annotations

from llm_fakes import simple_config
from loop_fakes import FakeExecutor, SequenceProvider, build_loop
from perpleximanus.core import (
    ActionEvent,
    ConversationStatus,
    KnowledgeEvent,
)
from perpleximanus.core.events import EventSource
from perpleximanus.core.llm import DefaultLLMRouter, OperatingMode, ProposedToolCall
from perpleximanus.core.loop import NeverConfirm, RouterAgent
from perpleximanus.core.view import View

CID = "conv"


def _router(scripted):
    provider = SequenceProvider(scripted)
    router = DefaultLLMRouter(simple_config(), {"ollama": provider, "openrouter": provider})
    return RouterAgent(router, conversation_id=CID), provider


async def test_remember_emits_a_pinned_knowledge_event_and_continues():
    # Call 1: remember a fact. Call 2: finish. The remember call must NOT end the
    # run and must NOT execute against the executor — it becomes a KnowledgeEvent.
    agent, _ = _router(
        [
            {
                "tool_calls": [
                    ProposedToolCall(
                        tool_name="remember",
                        arguments={"fact": "build cmd is `npm run build`", "scope": "build"},
                    )
                ]
            },
            {"tool_calls": [ProposedToolCall(tool_name="finish", arguments={"summary": "done"})]},
        ]
    )
    executor = FakeExecutor()
    loop, store = build_loop(agent, executor=executor, policy=NeverConfirm())

    await loop.send_message("build the app")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED
    # The remember call was INTERCEPTED — the executor never saw it.
    assert executor.calls == []
    # ...and no ActionEvent was recorded for `remember` (it's not an action).
    events = await store.get_events(CID)
    assert not any(
        isinstance(e, ActionEvent) and e.tool_call.tool_name == "remember" for e in events
    )
    # A KnowledgeEvent carries the fact + scope, sourced to the AGENT.
    knows = [e for e in events if isinstance(e, KnowledgeEvent)]
    assert len(knows) == 1
    assert knows[0].snippet == "build cmd is `npm run build`"
    assert knows[0].scope == "build"
    assert knows[0].source == EventSource.AGENT


async def test_remembered_fact_survives_condensation_via_pinning():
    # The View pins KnowledgeEvents, so the remembered fact is never "forgotten"
    # even when the surrounding transcript is condensed away.
    agent, _ = _router(
        [
            {
                "tool_calls": [
                    ProposedToolCall(
                        tool_name="remember", arguments={"fact": "API base is /v2"}
                    )
                ]
            },
            {"tool_calls": [ProposedToolCall(tool_name="finish", arguments={"summary": "x"})]},
        ]
    )
    loop, store = build_loop(agent, executor=FakeExecutor(), policy=NeverConfirm())
    await loop.send_message("go")
    await loop.run()

    events = await store.get_events(CID)
    know = next(e for e in events if isinstance(e, KnowledgeEvent))
    # Materialize the View and confirm the pinned fact is present in the messages.
    view = View.of(events)
    assert any("API base is /v2" in (m.content or "") for m in view.messages)
    # And the pin set names this event's seq (the condensation-exempt guarantee).
    from perpleximanus.core.view import _pinned_seqs

    assert know.seq in _pinned_seqs(events)


async def test_blank_fact_is_a_noop():
    agent, _ = _router(
        [
            {"tool_calls": [ProposedToolCall(tool_name="remember", arguments={"fact": "   "})]},
            {"tool_calls": [ProposedToolCall(tool_name="finish", arguments={"summary": "x"})]},
        ]
    )
    loop, store = build_loop(agent, executor=FakeExecutor(), policy=NeverConfirm())
    await loop.send_message("go")
    await loop.run()
    events = await store.get_events(CID)
    assert not any(isinstance(e, KnowledgeEvent) for e in events)


def test_remember_offered_in_execution_not_planning():
    # The tool is in the execution-mode tool list (a build affordance) but NOT in
    # the read-only planning surface.
    agent, _ = _router([{"text": "noop"}])
    loop, _ = build_loop(agent, executor=FakeExecutor(), mode=OperatingMode.LONG_HORIZON)
    names = {getattr(t, "name", None) for t in loop._tools_for_step()}
    assert "remember" in names

    loop.mode = OperatingMode.PLANNING
    names_planning = {getattr(t, "name", None) for t in loop._tools_for_step()}
    assert "remember" not in names_planning
