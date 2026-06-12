"""The `serve` virtual tool — the finished-artifact HANDOFF. The agent calls it
to declare a deliverable; the loop intercepts it (never executed) and emits a
DeliverableEvent the UI renders as Open-the-app / Download-the-files. Non-blocking
— the agent serves, verifies, then finishes."""

from __future__ import annotations

from disco.core import (
    ActionEvent,
    ConversationStatus,
    DeliverableEvent,
)
from disco.core.events import EventSource
from disco.core.llm import DefaultLLMRouter, OperatingMode, ProposedToolCall
from disco.core.loop import NeverConfirm, RouterAgent
from llm_fakes import simple_config
from loop_fakes import FakeExecutor, SequenceProvider, build_loop

CID = "conv"


def _agent(scripted):
    provider = SequenceProvider(scripted)
    router = DefaultLLMRouter(simple_config(), {"ollama": provider, "openrouter": provider})
    return RouterAgent(router, conversation_id=CID)


async def test_serve_emits_a_deliverable_event_and_continues():
    agent = _agent(
        [
            # Real work first — the post-resume serve gate refuses a serve
            # with zero non-bookkeeping actions this session.
            {"tool_calls": [ProposedToolCall(tool_name="shell", arguments={"cmd": "build"})]},
            {
                "tool_calls": [
                    ProposedToolCall(
                        tool_name="serve",
                        arguments={"title": "Landing page", "path": "dist", "kind": "app"},
                    )
                ]
            },
            {"tool_calls": [ProposedToolCall(tool_name="finish", arguments={"summary": "done"})]},
        ]
    )
    executor = FakeExecutor()
    loop, store = build_loop(agent, executor=executor, policy=NeverConfirm())

    await loop.send_message("build a landing page")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED
    # serve was INTERCEPTED — the executor never saw it, no ActionEvent for it.
    assert all(c.tool_name != "serve" for c in executor.calls)
    events = await store.get_events(CID)
    assert not any(
        isinstance(e, ActionEvent) and e.tool_call.tool_name == "serve" for e in events
    )
    # The handoff is a DeliverableEvent with the title/path/kind, sourced to AGENT.
    delivs = [e for e in events if isinstance(e, DeliverableEvent)]
    assert len(delivs) == 1
    assert delivs[0].title == "Landing page"
    assert delivs[0].path == "dist"
    assert delivs[0].artifact_kind == "app"
    assert delivs[0].source == EventSource.AGENT


async def test_serve_defaults_kind_to_app_and_validates_enum():
    agent = _agent(
        [
            {"tool_calls": [ProposedToolCall(tool_name="shell", arguments={"cmd": "make"})]},
            {
                "tool_calls": [
                    ProposedToolCall(
                        tool_name="serve",
                        arguments={"title": "Report", "path": "out.pdf", "kind": "bogus"},
                    )
                ]
            },
            {"tool_calls": [ProposedToolCall(tool_name="finish", arguments={"summary": "x"})]},
        ]
    )
    loop, store = build_loop(agent, executor=FakeExecutor(), policy=NeverConfirm())
    await loop.send_message("go")
    await loop.run()
    deliv = next(e for e in await store.get_events(CID) if isinstance(e, DeliverableEvent))
    assert deliv.artifact_kind == "app"  # bogus kind normalized to the default


async def test_serve_missing_path_or_title_is_a_noop():
    agent = _agent(
        [
            {"tool_calls": [ProposedToolCall(tool_name="shell", arguments={"cmd": "ls"})]},
            {"tool_calls": [ProposedToolCall(tool_name="serve", arguments={"title": "x"})]},
            {"tool_calls": [ProposedToolCall(tool_name="finish", arguments={"summary": "x"})]},
        ]
    )
    loop, store = build_loop(agent, executor=FakeExecutor(), policy=NeverConfirm())
    await loop.send_message("go")
    await loop.run()
    assert not any(isinstance(e, DeliverableEvent) for e in await store.get_events(CID))


def test_serve_offered_in_execution_not_planning():
    agent = _agent([{"text": "noop"}])
    loop, _ = build_loop(agent, executor=FakeExecutor(), mode=OperatingMode.LONG_HORIZON)
    assert "serve" in {getattr(t, "name", None) for t in loop._tools_for_step()}
    loop.mode = OperatingMode.PLANNING
    assert "serve" not in {getattr(t, "name", None) for t in loop._tools_for_step()}


def test_deliverable_event_renders_into_llm_context():
    # LLMConvertible: the agent's own context reflects what it handed off.
    ev = DeliverableEvent(title="Sales report", path="report.html", artifact_kind="files")
    msg = ev.to_llm_message()
    assert "report.html" in msg.content and "files" in msg.content
    assert "Sales report" in msg.content
