"""REL-1e host verifier authoritative gate."""

from __future__ import annotations

import pytest
from disco.core import (
    ActionEvent,
    ConversationStatus,
    DeliverableEvent,
    MessageEvent,
    NoOpCondenser,
    ObservationEvent,
    SqliteEventStore,
    StatusEvent,
    ToolResult,
    VerifierShadowEvent,
    VerifierVerdictEvent,
)
from disco.core.events import EventSource
from disco.core.llm import ModelRole, OperatingMode, ToolSpec
from disco.core.llm.prompts import DriverPrompts
from disco.core.loop import (
    AgentLoop,
    NeverConfirm,
    TypedVerifierVerdict,
    VerifierContextSeed,
    host_verify_authoritative_enabled,
)
from disco.core.loop.finish.verify_gates import _bounded_model_verifier_cause
from loop_fakes import (
    FakeAnalyzer,
    FakeExecutor,
    FakeSummarizer,
    ScriptedAgent,
    action_step,
    finish_step,
)


def _verdict(*, passed: bool, fp: str = "HOST", verdict: str | None = None) -> dict:
    label = verdict or ("pass" if passed else "fail")
    return {
        "passed": passed,
        "verdict": label,
        "url": "http://127.0.0.1:8000/",
        "http_status": 200,
        "title": "app",
        "meaningful_content": passed,
        "visible_text_chars": 40 if passed else 0,
        "elements_count": 1 if passed else 0,
        "console_errors": [] if passed else [{"text": "Boom", "source": "app.js:1"}],
        "console_warnings": [],
        "network_failures": [],
        "screenshot_path": "",
        "vision": {"used": False, "passed": None, "notes": []},
        "failure_fingerprint": fp,
        "summary": "ok" if passed else "broken by host verifier",
        "next_action": "" if passed else "fix the console error",
    }


class _VerifyExecutor(FakeExecutor):
    def __init__(self, verdict: dict | None = None):
        super().__init__(
            tools=[
                ToolSpec(name="file_write", description="write", parameters_schema={}),
                ToolSpec(name="serve", description="serve", parameters_schema={}),
                ToolSpec(name="submit_plan", description="plan", parameters_schema={}),
                ToolSpec(name="verify_web_app", description="verify", parameters_schema={}),
            ]
        )
        self._verdict = verdict or _verdict(passed=True, fp="INLINE")
        self.verify_calls = 0

    async def execute(self, call):
        if call.tool_name == "verify_web_app":
            self.calls.append(call)
            self.verify_calls += 1
            return ToolResult(
                call_id=call.call_id,
                tool_name="verify_web_app",
                success=True,
                content=f"VERIFY {self._verdict['verdict']}",
                structured=self._verdict,
            )
        return await super().execute(call)


class _HostVerifier:
    def __init__(self, verdict: dict) -> None:
        self._verdict = verdict
        self.calls = []

    async def verify(self, deliverable):
        self.calls.append(deliverable)
        return self._verdict


class _VerifierJudge:
    def __init__(self, verdict: TypedVerifierVerdict) -> None:
        self._verdict = verdict
        self.seeds: list[VerifierContextSeed] = []
        self.transcript = "SECRET VERIFY TRANSCRIPT"

    async def judge(self, seed: VerifierContextSeed) -> TypedVerifierVerdict:
        self.seeds.append(seed)
        return self._verdict


def _loop(
    agent,
    executor,
    *,
    host_verifier=None,
    hook=None,
    verifier_judge=None,
    finish_alias: str | None = None,
):
    store = SqliteEventStore(":memory:")
    loop = AgentLoop(
        "conv",
        store,
        agent,
        executor,
        None,
        FakeAnalyzer(),
        NeverConfirm(),
        NoOpCondenser(),
        FakeSummarizer(),
        mode=OperatingMode.LONG_HORIZON,
        planning_tools=frozenset({"submit_plan"}),
        finish_alias=finish_alias,
        host_verifier=host_verifier,
        host_verifier_verdict_hook=hook,
        host_verify_authoritative=True,
        verifier_judge=verifier_judge,
    )
    return loop, store


def _env_messages(events) -> list[str]:
    return [
        e.message.content
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and e.message is not None
    ]


def _agent_messages(events) -> list[MessageEvent]:
    return [
        e
        for e in events
        if isinstance(e, MessageEvent) and e.source == EventSource.AGENT and e.message is not None
    ]


def _statuses(events) -> list[tuple[str, str | None]]:
    return [(e.status.value, e.detail) for e in events if isinstance(e, StatusEvent)]


def test_model_verifier_event_cause_rejects_untrusted_free_form_text() -> None:
    assert (
        _bounded_model_verifier_cause("provider echoed TOP_SECRET_RESPONSE_CONTENT")
        == "model verifier unavailable (unclassified structural failure)"
    )


@pytest.mark.asyncio
async def test_host_pass_finishes_verified_and_skips_inline_verify() -> None:
    host = _HostVerifier(_verdict(passed=True, fp="HOST"))
    execu = _VerifyExecutor(_verdict(passed=True, fp="INLINE"))
    agent = ScriptedAgent(
        [
            action_step(
                tool="file_write",
                args={"path": "index.html", "content": "<h1>hello</h1>"},
            ),
            finish_step("Verified cleanly by the host browser."),
        ]
    )
    loop, store = _loop(agent, execu, host_verifier=host)

    await loop.send_message("build a page")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED
    assert execu.verify_calls == 0
    assert len(host.calls) == 1
    events = await store.get_events("conv")
    verdicts = [e for e in events if isinstance(e, VerifierVerdictEvent)]
    assert len(verdicts) == 1
    assert verdicts[0].verified is True
    assert verdicts[0].verdict == "pass"
    assert not any(
        isinstance(e, ActionEvent) and e.tool_call.tool_name == "verify_web_app" for e in events
    )
    assert not any(
        isinstance(e, ObservationEvent) and e.tool_result.tool_name == "verify_web_app"
        for e in events
    )
    final = _agent_messages(events)[-1]
    assert final.message.content == "Verified cleanly by the host browser."
    assert final.meta.get("host_owned_terminal_warning") is not True
    assert agent.calls == 2


@pytest.mark.asyncio
async def test_later_host_pass_supersedes_stale_unverified_release_marker() -> None:
    host = _HostVerifier(_verdict(passed=True, fp="HOST"))
    execu = _VerifyExecutor(_verdict(passed=True, fp="INLINE"))
    agent = ScriptedAgent(
        [
            action_step(
                tool="file_write",
                args={"path": "index.html", "content": "<h1>hello</h1>"},
            ),
            finish_step("Verified after a fresh passing host verdict."),
        ]
    )
    loop, store = _loop(agent, execu, host_verifier=host)

    async def _seed_stale_marker() -> None:
        await store.append(
            "conv",
            StatusEvent(
                status=ConversationStatus.RUNNING,
                detail="unverified_release",
            ),
        )

    agent._before[0] = _seed_stale_marker
    await loop.send_message("build a page")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED
    events = await store.get_events("conv")
    verdicts = [e for e in events if isinstance(e, VerifierVerdictEvent)]
    assert verdicts[-1].verified is True
    final = _agent_messages(events)[-1]
    assert final.message.content == "Verified after a fresh passing host verdict."
    assert final.meta.get("host_owned_terminal_warning") is not True
    assert agent.calls == 2


@pytest.mark.asyncio
async def test_host_fail_refuses_then_releases_loudly_without_inline_verify() -> None:
    host = _HostVerifier(_verdict(passed=False, fp="HOST_FAIL"))
    execu = _VerifyExecutor(_verdict(passed=True, fp="INLINE"))
    agent = ScriptedAgent(
        [
            action_step(
                tool="file_write",
                args={"path": "index.html", "content": "<h1>hello</h1>"},
            ),
            finish_step(),
            finish_step(),
            finish_step(),
            finish_step(),
        ]
    )
    loop, store = _loop(agent, execu, host_verifier=host)

    await loop.send_message("build a page")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED
    assert execu.verify_calls == 0
    assert len(host.calls) == 4
    events = await store.get_events("conv")
    env = _env_messages(events)
    assert any("<system-reminder>" in m and "Host verification did not pass" in m for m in env)
    assert any("Boom @ app.js:1" in m for m in env)
    assert any("WITHOUT a passing host verifier verdict" in m for m in env)
    assert ("RUNNING", "unverified_release") in _statuses(events)
    assert loop._browser_verify_refusals == 0


@pytest.mark.asyncio
async def test_model_verifier_gets_bounded_seed_and_builder_gets_summary_only() -> None:
    raw = _verdict(passed=True, fp="HOST")
    raw["summary"] = "RAW_CHECK_SECRET"
    raw["screenshot_path"] = ".pmx/screenshots/0001-navigate.png"
    host = _HostVerifier(raw)
    judge = _VerifierJudge(
        TypedVerifierVerdict(
            verified=False,
            verdict="fail",
            detail="Typed verifier summary only.",
            failures=[{"kind": "copy_quality", "message": "Typed failure only."}],
            next_action="Revise the hero copy.",
            failure_fingerprint="typed-copy-quality",
        )
    )
    execu = _VerifyExecutor(_verdict(passed=True, fp="INLINE"))
    agent = ScriptedAgent(
        [
            action_step(
                tool="file_write",
                args={"path": "index.html", "content": "<h1>hello</h1>"},
            ),
            finish_step(),
            finish_step(),
            finish_step(),
            finish_step(),
        ]
    )
    loop, store = _loop(
        agent,
        execu,
        host_verifier=host,
        verifier_judge=judge,
        finish_alias="ready_for_static_site_verification",
    )

    await loop.send_message("build a page")
    await loop.run()

    assert judge.seeds
    seed = judge.seeds[0]
    assert set(seed.model_dump(mode="json")) == {
        "contract",
        "deliverable_paths",
        "check_results",
        "screenshot",
        "medium",
    }
    assert seed.contract["verify"]["finalizer"] == "ready_for_static_site_verification"
    assert seed.deliverable_paths == ["index.html"]
    assert seed.check_results["summary"] == "RAW_CHECK_SECRET"
    assert seed.screenshot.path == ".pmx/screenshots/0001-navigate.png"
    assert seed.medium is None

    events = await store.get_events("conv")
    verdicts = [e for e in events if isinstance(e, VerifierVerdictEvent)]
    assert verdicts
    assert verdicts[0].verdict == "fail"
    assert verdicts[0].detail == "Typed verifier summary only."
    assert verdicts[0].failures[0]["message"] == "Typed failure only."

    builder_context = "\n".join(msg.content for view in agent.seen_views for msg in view.messages)
    assert "Typed verifier summary only." in builder_context
    assert "Typed failure only." in builder_context
    assert "RAW_CHECK_SECRET" not in builder_context
    assert judge.transcript not in builder_context


@pytest.mark.asyncio
async def test_model_verifier_unavailable_fallback_is_visible_in_persisted_events() -> None:
    host = _HostVerifier(_verdict(passed=True, fp="HOST"))
    judge = _VerifierJudge(
        TypedVerifierVerdict(
            verified=False,
            verdict="unavailable",
            detail=(
                "model verifier unavailable (JSONDecodeError: Expecting value at line 1 column 1)"
            ),
            failures=[
                {
                    "kind": "verifier_unavailable",
                    "message": "JSONDecodeError: line 1 column 1",
                }
            ],
            failure_fingerprint="model_verifier_unavailable",
        )
    )
    agent = ScriptedAgent(
        [
            action_step(
                tool="file_write",
                args={"path": "index.html", "content": "<h1>hello</h1>"},
            ),
            finish_step(),
        ]
    )
    loop, store = _loop(
        agent,
        _VerifyExecutor(_verdict(passed=True, fp="INLINE")),
        host_verifier=host,
        verifier_judge=judge,
    )

    await loop.send_message("build a page")
    state = await loop.run()
    assert state.execution_status == ConversationStatus.FINISHED
    events = await store.get_events("conv")
    verdict = next(event for event in events if isinstance(event, VerifierVerdictEvent))
    shadow = next(event for event in events if isinstance(event, VerifierShadowEvent))
    # Deterministic host evidence remains authoritative and green, but the
    # independent judge degradation can no longer disappear into that verdict.
    assert verdict.verified is True and verdict.verdict == "pass"
    for event in (verdict, shadow):
        assert event.meta["model_verifier_status"] == "unavailable"
        assert event.meta["model_verifier_applied"] is False
        assert event.meta["model_verifier_cause"] == (
            "model verifier unavailable (JSONDecodeError: Expecting value at line 1 column 1)"
        )


@pytest.mark.asyncio
async def test_host_unavailable_degrades_to_inline_gate_not_refusal() -> None:
    """REL-1e flip safety: ``unavailable`` (verifier infra could not run) is not
    evidence the app is broken — it must NOT burn host-refusal cycles, and it
    must NOT delegate the browser gate to the host either (that would let the
    app finish with no verification at all). The inline verify_web_app gate
    stays the enforcement path."""
    host = _HostVerifier(
        _verdict(passed=False, fp="host_verifier_unavailable", verdict="unavailable")
    )
    execu = _VerifyExecutor(_verdict(passed=True, fp="INLINE"))
    agent = ScriptedAgent(
        [
            action_step(
                tool="file_write",
                args={"path": "index.html", "content": "<h1>hello</h1>"},
            ),
            finish_step(),
            action_step(tool="verify_web_app", args={}),
            finish_step(),
        ]
    )
    loop, store = _loop(agent, execu, host_verifier=host)

    await loop.send_message("build a page")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED
    # The inline gate enforced: the agent had to run a real verify_web_app.
    assert execu.verify_calls >= 1
    events = await store.get_events("conv")
    env = _env_messages(events)
    # No host-refusal cycle was burned and no loud unverified release happened.
    assert not any("Host verification did not pass" in m for m in env)
    assert not any("WITHOUT a passing host verifier verdict" in m for m in env)
    assert ("RUNNING", "unverified_release") not in _statuses(events)
    # The honest ``unavailable`` verdict is still on the audit trail.
    verdicts = [e for e in events if isinstance(e, VerifierVerdictEvent)]
    assert verdicts and all(v.verdict == "unavailable" for v in verdicts)
    assert all(v.verified is False for v in verdicts)


@pytest.mark.asyncio
async def test_host_unverifiable_finishes_with_explicit_unverified_marker() -> None:
    """Browser infrastructure absence is terminal but can never become a pass."""
    host_verdict = _verdict(passed=False, fp="browser_unavailable", verdict="unverifiable")
    host_verdict["startup_diagnostic"] = "exit=1; API_KEY=do-not-retain chromium launch failed"
    host = _HostVerifier(host_verdict)
    execu = _VerifyExecutor(_verdict(passed=True, fp="INLINE"))
    agent = ScriptedAgent(
        [
            action_step(
                tool="file_write",
                args={"path": "index.html", "content": "<h1>hello</h1>"},
            ),
            finish_step(
                'Created index.html. Verified: HTTP 200 and the heading "hello" is present.'
            ),
        ]
    )
    loop, store = _loop(agent, execu, host_verifier=host)

    await loop.send_message("build a page")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED
    assert execu.verify_calls == 0
    assert len(host.calls) == 1
    events = await store.get_events("conv")
    verdicts = [e for e in events if isinstance(e, VerifierVerdictEvent)]
    assert len(verdicts) == 1
    assert verdicts[0].verified is False
    assert verdicts[0].verdict == "unverifiable"
    assert verdicts[0].detail is not None
    assert "Browser startup diagnostic: exit=1; <redacted> chromium launch failed" in (
        verdicts[0].detail
    )
    assert "do-not-retain" not in verdicts[0].detail
    assert ("RUNNING", "unverified_release") in _statuses(events)
    env = _env_messages(events)
    assert any("WITHOUT browser-render verification" in m for m in env)
    assert any("UNVERIFIED" in m and "INCOMPLETE" in m for m in env)
    assert not any("Host verification did not pass" in m for m in env)
    final = _agent_messages(events)[-1]
    assert final.message.content.startswith("⚠ UNVERIFIED FINAL RESULT:")
    assert "Browser rendering remains UNVERIFIED" in final.message.content
    assert "Verified: HTTP 200" not in final.message.content
    assert final.meta.get("host_owned_terminal_warning") is True
    assert final.meta.get("unverified_release") is True
    assert agent.calls == 2


@pytest.mark.asyncio
async def test_authoritative_non_web_without_handoff_stays_unchanged() -> None:
    host = _HostVerifier(_verdict(passed=True, fp="HOST"))
    execu = _VerifyExecutor(_verdict(passed=True, fp="INLINE"))
    agent = ScriptedAgent(
        [
            action_step(tool="file_write", args={"path": "main.py", "content": "print(1)"}),
            finish_step(),
        ]
    )
    loop, store = _loop(agent, execu, host_verifier=host)

    await loop.send_message("write a script")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED
    assert host.calls == []
    assert execu.verify_calls == 0
    events = await store.get_events("conv")
    assert not any(isinstance(e, VerifierVerdictEvent) for e in events)


@pytest.mark.asyncio
async def test_files_handoff_without_validator_records_unverifiable() -> None:
    host = _HostVerifier(_verdict(passed=True, fp="HOST"))
    execu = _VerifyExecutor(_verdict(passed=True, fp="INLINE"))
    verdict_events: list[VerifierVerdictEvent] = []

    async def hook(event: VerifierVerdictEvent) -> None:
        verdict_events.append(event)

    agent = ScriptedAgent(
        [
            action_step(
                tool="file_write",
                args={"path": "report.txt", "content": "done"},
            ),
            action_step(
                tool="serve",
                args={"title": "Report", "path": "report.txt", "kind": "files"},
            ),
            finish_step(),
        ]
    )
    loop, store = _loop(agent, execu, host_verifier=host, hook=hook)

    await loop.send_message("write a report")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED
    assert host.calls == []
    assert execu.verify_calls == 0
    events = await store.get_events("conv")
    assert any(
        isinstance(e, DeliverableEvent) and e.artifact_kind == "files" and e.path == "report.txt"
        for e in events
    )
    verdicts = [e for e in events if isinstance(e, VerifierVerdictEvent)]
    assert len(verdicts) == 1
    assert verdicts[0].artifact_kind == "files"
    assert verdicts[0].verified is False
    assert verdicts[0].verdict == "unverifiable"
    assert verdict_events == verdicts


def test_authoritative_flag_default_on_and_explicit_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """REL-1e flip (2026-07-03): authoritative is the DEFAULT; only an explicit
    falsy value restores the REL-1c shadow posture."""
    monkeypatch.delenv("DISCO_HOST_VERIFY_AUTHORITATIVE", raising=False)
    monkeypatch.delenv("PMX_HOST_VERIFY_AUTHORITATIVE", raising=False)
    assert host_verify_authoritative_enabled() is True

    monkeypatch.setenv("DISCO_HOST_VERIFY_AUTHORITATIVE", "on")
    assert host_verify_authoritative_enabled() is True

    for falsy in ("0", "false", "no", "off", " OFF "):
        monkeypatch.setenv("DISCO_HOST_VERIFY_AUTHORITATIVE", falsy)
        assert host_verify_authoritative_enabled() is False, falsy


def test_prompt_self_verify_mandate_softens_only_when_authoritative() -> None:
    off = DriverPrompts().system_prompt(
        model_family="gpt",
        mode=OperatingMode.LONG_HORIZON,
        role=ModelRole.AGENT_DRIVER,
    )
    assert "Before declaring a web build finished, do ONE structured verify pass" in off
    assert "with `verify_web_app` after `preview_start`" in off
    assert "after handing it off with `serve(...)`" not in off

    on = DriverPrompts(host_verify_authoritative=True).system_prompt(
        model_family="gpt",
        mode=OperatingMode.LONG_HORIZON,
        role=ModelRole.AGENT_DRIVER,
    )
    assert "Before declaring a web build finished, do ONE structured verify pass" not in on
    assert "with `verify_web_app` after handing it off with `serve(...)`" in on
    assert "The platform also runs the host verifier at the finish gate" in on

    small = DriverPrompts(host_verify_authoritative=True).system_prompt(
        model_family="gpt",
        mode=OperatingMode.LONG_HORIZON,
        role=ModelRole.AGENT_DRIVER,
        assist=True,
    )
    assert "WEB BUILDS — before declaring finished, do ONE verify-pass" not in small
    assert "WEB BUILDS — do ONE structured verify pass with `verify_web_app`" in small
    assert "will refuse finish with a concrete failure" in small
