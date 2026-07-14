"""The `serve` virtual tool — the finished-artifact HANDOFF. The agent calls it
to declare a deliverable; the loop intercepts it (never executed) and emits a
DeliverableEvent the UI renders as Open-the-app / Download-the-files. Non-blocking
— the agent serves, verifies, then finishes."""

from __future__ import annotations

import pytest
from disco.core import (
    ActionEvent,
    ConversationStatus,
    DeliverableEvent,
    MessageEvent,
)
from disco.core.events import EventSource
from disco.core.llm import DefaultLLMRouter, OperatingMode, ProposedToolCall
from disco.core.loop import NeverConfirm, RouterAgent
from llm_fakes import simple_config
from loop_fakes import FakeExecutor, SequenceProvider, build_loop

CID = "conv"


def _environment_messages(events):
    return [
        event
        for event in events
        if isinstance(event, MessageEvent) and event.source == EventSource.ENVIRONMENT
    ]


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
    guidance = [
        event
        for event in _environment_messages(events)
        if event.meta.get("diagnostic") == "serve_handoff_recorded"
    ]
    assert len(guidance) == 1
    assert "Handoff recorded" in guidance[0].message.content
    assert "call `finish`" in guidance[0].message.content


async def test_serve_defaults_kind_to_app_and_rejects_invalid_enum():
    agent = _agent(
        [
            {"tool_calls": [ProposedToolCall(tool_name="shell", arguments={"cmd": "make"})]},
            {
                "tool_calls": [
                    ProposedToolCall(
                        tool_name="serve", arguments={"title": "Report", "path": "out.pdf"}
                    )
                ]
            },
            {
                "tool_calls": [
                    ProposedToolCall(
                        tool_name="serve",
                        arguments={"title": "Bad", "path": "bad.pdf", "kind": "bogus"},
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
    delivs = [event for event in events if isinstance(event, DeliverableEvent)]
    assert len(delivs) == 1
    assert delivs[0].artifact_kind == "app"  # omitted kind uses the documented default
    invalid = [
        event
        for event in _environment_messages(events)
        if "`kind` must be exactly `app` or `files`" in event.message.content
    ]
    assert len(invalid) == 1


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({}, "provide both required string fields"),
        ({"title": "x"}, "`path` is required"),
        ({"path": "out.pdf"}, "`title` is required"),
    ],
)
async def test_serve_malformed_arguments_are_explicit_and_actionable(arguments, expected):
    agent = _agent(
        [
            {"tool_calls": [ProposedToolCall(tool_name="shell", arguments={"cmd": "ls"})]},
            {"tool_calls": [ProposedToolCall(tool_name="serve", arguments=arguments)]},
            {"tool_calls": [ProposedToolCall(tool_name="finish", arguments={"summary": "x"})]},
        ]
    )
    loop, store = build_loop(agent, executor=FakeExecutor(), policy=NeverConfirm())
    await loop.send_message("go")
    state = await loop.run()
    assert state.execution_status == ConversationStatus.FINISHED
    events = await store.get_events(CID)
    assert not any(isinstance(event, DeliverableEvent) for event in events)
    feedback = [
        event
        for event in _environment_messages(events)
        if expected in event.message.content
    ]
    assert len(feedback) == 1


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


# --- F3: sandbox-internal serve URL must not become a (false) reachable URL ----


def test_canonical_deployment_url_drops_sandbox_internal_addresses():
    from disco.core.loop.turn_control import _canonical_deployment_url

    # The exact live-observed sandbox-internal address (collides with the
    # agent-server's own :8000) — never reachable from the host → dropped.
    assert _canonical_deployment_url("http://127.0.0.1:8000/") == ""
    # Other non-externally-reachable hosts are dropped too.
    for bad in (
        "http://localhost:5173/",
        "http://0.0.0.0:8000/",
        "http://[::1]:8000/",
        "http://10.0.0.5:3000/",
        "http://192.168.1.20:8080/",
        "http://172.17.0.2:8000/",  # docker/sandbox bridge net
        "http://169.254.0.1/",  # link-local
        "ftp://example.com/",  # not http(s)
        "not-a-url",
        "",
    ):
        assert _canonical_deployment_url(bad) == "", bad
    # A REAL deploy target / tunnel hostname or a public IP is preserved.
    for good in (
        "https://my-site.trycloudflare.com/",
        "https://cadence.example.com/app",
        "http://8.8.8.8/",  # a genuinely public IP literal
    ):
        assert _canonical_deployment_url(good) == good, good


async def test_serve_drops_sandbox_internal_url_but_keeps_public():
    # Two served apps: the first reports the sandbox-internal http.server address
    # (must be dropped → ""), the second a real tunnel (must be preserved).
    agent = _agent(
        [
            {"tool_calls": [ProposedToolCall(tool_name="shell", arguments={"cmd": "build"})]},
            {
                "tool_calls": [
                    ProposedToolCall(
                        tool_name="serve",
                        arguments={
                            "title": "Local",
                            "path": "dist",
                            "kind": "app",
                            "url": "http://127.0.0.1:8000/",
                        },
                    )
                ]
            },
            {
                "tool_calls": [
                    ProposedToolCall(
                        tool_name="serve",
                        arguments={
                            "title": "Tunnel",
                            "path": "site",
                            "kind": "app",
                            "url": "https://demo.trycloudflare.com/",
                        },
                    )
                ]
            },
            {"tool_calls": [ProposedToolCall(tool_name="finish", arguments={"summary": "done"})]},
        ]
    )
    loop, store = build_loop(agent, executor=FakeExecutor(), policy=NeverConfirm())
    await loop.send_message("build it")
    await loop.run()
    delivs = [e for e in await store.get_events(CID) if isinstance(e, DeliverableEvent)]
    by_title = {d.title: d for d in delivs}
    # Sandbox-internal address is NOT surfaced as a reachable URL (consumers fall
    # back to the host preview-app proxy); the real tunnel URL is preserved.
    assert by_title["Local"].deployment_url == ""
    assert by_title["Tunnel"].deployment_url == "https://demo.trycloudflare.com/"
