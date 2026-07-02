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
    VerifierVerdictEvent,
)
from disco.core.events import EventSource
from disco.core.llm import ModelRole, OperatingMode, ToolSpec
from disco.core.llm.prompts import DriverPrompts
from disco.core.loop import AgentLoop, NeverConfirm, host_verify_authoritative_enabled
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


def _loop(agent, executor, *, host_verifier=None, hook=None):
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
        host_verifier=host_verifier,
        host_verifier_verdict_hook=hook,
        host_verify_authoritative=True,
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


def _statuses(events) -> list[tuple[str, str | None]]:
    return [(e.status.value, e.detail) for e in events if isinstance(e, StatusEvent)]


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
            finish_step(),
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
        isinstance(e, ActionEvent) and e.tool_call.tool_name == "verify_web_app"
        for e in events
    )
    assert not any(
        isinstance(e, ObservationEvent) and e.tool_result.tool_name == "verify_web_app"
        for e in events
    )


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
        isinstance(e, DeliverableEvent)
        and e.artifact_kind == "files"
        and e.path == "report.txt"
        for e in events
    )
    verdicts = [e for e in events if isinstance(e, VerifierVerdictEvent)]
    assert len(verdicts) == 1
    assert verdicts[0].artifact_kind == "files"
    assert verdicts[0].verified is False
    assert verdicts[0].verdict == "unverifiable"
    assert verdict_events == verdicts


def test_authoritative_flag_default_off_and_truthy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISCO_HOST_VERIFY_AUTHORITATIVE", raising=False)
    monkeypatch.delenv("PMX_HOST_VERIFY_AUTHORITATIVE", raising=False)
    assert host_verify_authoritative_enabled() is False

    monkeypatch.setenv("DISCO_HOST_VERIFY_AUTHORITATIVE", "on")
    assert host_verify_authoritative_enabled() is True

    monkeypatch.setenv("DISCO_HOST_VERIFY_AUTHORITATIVE", "0")
    assert host_verify_authoritative_enabled() is False


def test_prompt_self_verify_mandate_softens_only_when_authoritative() -> None:
    off = DriverPrompts().system_prompt(
        model_family="gpt",
        mode=OperatingMode.LONG_HORIZON,
        role=ModelRole.AGENT_DRIVER,
    )
    assert "Before declaring a web build finished, do ONE verify-pass" in off
    assert "The platform runs the host verifier at the finish gate" not in off

    on = DriverPrompts(host_verify_authoritative=True).system_prompt(
        model_family="gpt",
        mode=OperatingMode.LONG_HORIZON,
        role=ModelRole.AGENT_DRIVER,
    )
    assert "Before declaring a web build finished, do ONE verify-pass" not in on
    assert "The platform runs the host verifier at the finish gate" in on

    small = DriverPrompts(host_verify_authoritative=True).system_prompt(
        model_family="gpt",
        mode=OperatingMode.LONG_HORIZON,
        role=ModelRole.AGENT_DRIVER,
        assist=True,
    )
    assert "WEB BUILDS — before declaring finished, do ONE verify-pass" not in small
    assert "will refuse finish with a concrete failure" in small
