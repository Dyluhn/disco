"""The `serve` virtual tool — the finished-artifact HANDOFF. The agent calls it
to declare a deliverable; the loop intercepts it (never executed) and emits a
DeliverableEvent the UI renders as Open-the-app / Download-the-files. Non-blocking
— the agent serves, verifies, then finishes."""

from __future__ import annotations

import pytest
from disco.core import (
    ActionEvent,
    BuildPlatformAdmissionEvent,
    ConversationStatus,
    DeliverableEvent,
    MessageEvent,
)
from disco.core.events import EventSource
from disco.core.llm import DefaultLLMRouter, OperatingMode, ProposedToolCall
from disco.core.loop import NeverConfirm, RouterAgent
from disco.core.verification import (
    HostVerificationClaim,
    VerificationClaimKind,
    default_structured_web_claims,
)
from llm_fakes import simple_config
from loop_fakes import FakeExecutor, SequenceProvider, build_loop

CID = "conv"
_DIGEST = "sha256:" + "a" * 64
_RUN_ID = "run:sha256:" + "b" * 64


def _platform_admission(*, claims) -> BuildPlatformAdmissionEvent:
    return BuildPlatformAdmissionEvent(
        route="platform",
        profile_id="disco.freeform_web@1",
        run_intent_id="intent-test",
        composition_authority="build_platform_core",
        composition_digest=_DIGEST,
        run_identity=_RUN_ID,
        verification_claims=tuple(claims),
    )


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
    assert not any(isinstance(e, ActionEvent) and e.tool_call.tool_name == "serve" for e in events)
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
        # 2026-08-07b: the refusal now NAMES the rejected kind rather than
        # restating the rule in canned bytes (constraint 4). Id KEPT.
        if "is neither `app` nor `files`" in event.message.content
    ]
    assert len(invalid) == 1
    assert "`kind` was" in invalid[0].message.content


async def test_governed_browser_target_refuses_files_then_accepts_app_handoff():
    agent = _agent(
        [
            {"tool_calls": [ProposedToolCall(tool_name="shell", arguments={"cmd": "build"})]},
            {
                "tool_calls": [
                    ProposedToolCall(
                        tool_name="serve",
                        arguments={"title": "Site", "path": "index.html", "kind": "files"},
                    )
                ]
            },
            {
                "tool_calls": [
                    ProposedToolCall(
                        tool_name="serve",
                        arguments={"title": "Site", "path": "index.html", "kind": "app"},
                    )
                ]
            },
            {"tool_calls": [ProposedToolCall(tool_name="finish", arguments={"summary": "done"})]},
        ]
    )
    loop, store = build_loop(agent, executor=FakeExecutor(), policy=NeverConfirm())
    await loop._emit(_platform_admission(claims=default_structured_web_claims()))

    await loop.send_message("build a browser application")
    await loop.run()

    events = await store.get_events(CID)
    deliverables = [event for event in events if isinstance(event, DeliverableEvent)]
    assert [(event.artifact_kind, event.path) for event in deliverables] == [("app", "index.html")]
    refusals = [
        event
        for event in _environment_messages(events)
        if event.meta.get("diagnostic") == "serve_target_shape_refused"
    ]
    assert len(refusals) == 1
    assert "target-owned delivery contract" in refusals[0].message.content


async def test_shape_refusal_names_the_admitted_kind_and_escalates_on_repeat():
    """F41 (counted wave-1 FAIL, `diag_script_run` seed 7, ACTIONLESS_THRASH).

    The refusal used to say the handoff "does not match" and to use "the
    requested interactive app or artifact-files shape" — never WHICH one, though
    the gate had just computed it. An agent whose deliverable is a CLI script
    could only re-send the same shape, byte-identically, 13 times, until the
    loop-breaker graded the retries as thrash.

    Two facts are asserted, and BOTH fail on the pre-repair bytes: the first
    refusal names the admitted kind, and a second refusal is not byte-identical
    to the first.
    """

    files_serve = {
        "tool_calls": [
            ProposedToolCall(
                tool_name="serve",
                arguments={"title": "Primes", "path": "primes.py", "kind": "files"},
            )
        ]
    }
    agent = _agent(
        [
            {"tool_calls": [ProposedToolCall(tool_name="shell", arguments={"cmd": "python3 x"})]},
            files_serve,
            files_serve,
            {"tool_calls": [ProposedToolCall(tool_name="finish", arguments={"summary": "done"})]},
        ]
    )
    loop, store = build_loop(agent, executor=FakeExecutor(), policy=NeverConfirm())
    await loop._emit(_platform_admission(claims=default_structured_web_claims()))

    await loop.send_message("write primes.py, run it, and confirm the output")
    await loop.run()

    events = await store.get_events(CID)
    refusals = [
        event
        for event in _environment_messages(events)
        if event.meta.get("diagnostic") == "serve_target_shape_refused"
    ]
    assert len(refusals) == 2
    contents = [event.message.content for event in refusals]

    # The fact that makes a corrective move possible: which kind IS admitted.
    assert 'kind="app"' in contents[0]
    assert 'kind="files"' in contents[0]
    assert refusals[0].meta["expected_kind"] == "app"
    assert refusals[0].meta["offered_kind"] == "files"

    # An identical second refusal is what produced the thrash; it must escalate.
    assert contents[1] != contents[0]
    assert "STOP" in contents[1]
    assert refusals[1].meta["shape_refusals"] == 2


async def test_governed_nonbrowser_target_keeps_files_handoff() -> None:
    target_claim = HostVerificationClaim(
        claim_id="target.report",
        kind=VerificationClaimKind.TARGET_SPECIFIC,
        expected="valid report artifact",
        source_authority="target:report@1",
    )
    agent = _agent(
        [
            {"tool_calls": [ProposedToolCall(tool_name="shell", arguments={"cmd": "build"})]},
            {
                "tool_calls": [
                    ProposedToolCall(
                        tool_name="serve",
                        arguments={"title": "Report", "path": "report.txt", "kind": "files"},
                    )
                ]
            },
            {"tool_calls": [ProposedToolCall(tool_name="finish", arguments={"summary": "done"})]},
        ]
    )
    loop, store = build_loop(agent, executor=FakeExecutor(), policy=NeverConfirm())
    await loop._emit(_platform_admission(claims=(target_claim,)))

    await loop.send_message("build a report")
    await loop.run()

    events = await store.get_events(CID)
    assert any(
        isinstance(event, DeliverableEvent)
        and event.artifact_kind == "files"
        and event.path == "report.txt"
        for event in events
    )
    assert not any(
        event.meta.get("diagnostic") == "serve_target_shape_refused"
        for event in _environment_messages(events)
    )


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        # 2026-08-07b: the expected strings now name the DEFECT each refusal
        # must state, not the byte-identical canned sentence the surface used to
        # emit (constraint 4). The parametrize IDS are pinned to their historical
        # values so this strengthening deletes ZERO test ids from the inventory.
        ({}, "the call carried no arguments"),
        ({"title": "x"}, "`path` was empty or missing"),
        ({"path": "out.pdf"}, "`title` was empty or missing"),
    ],
    ids=[
        "arguments0-provide both required string fields",
        "arguments1-`path` is required",
        "arguments2-`title` is required",
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
        event for event in _environment_messages(events) if expected in event.message.content
    ]
    assert len(feedback) == 1
    # The refusal must be a projection of the call, not canned text: it names
    # what was actually received and exactly ONE corrective move.
    body = feedback[0].message.content
    assert "You sent:" in body
    assert "Next move:" in body
    assert feedback[0].meta.get("diagnostic") == "serve_argument_refused"


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
