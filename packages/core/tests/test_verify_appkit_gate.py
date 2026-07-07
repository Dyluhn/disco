"""AppKit finish gate drives `verify_appkit_app` in strict AppKit mode."""

from __future__ import annotations

import pytest
from disco.core import (
    ActionEvent,
    MessageEvent,
    NoOpCondenser,
    ObservationEvent,
    SqliteEventStore,
    StatusEvent,
    ToolResult,
)
from disco.core.events import EventSource
from disco.core.llm import OperatingMode, ToolSpec
from disco.core.loop import AgentLoop, NeverConfirm
from disco.core.loop.finish import FinishGate, _latest_verify_verdict
from loop_fakes import (
    FakeAnalyzer,
    FakeExecutor,
    FakeSummarizer,
    ScriptedAgent,
    action_step,
    finish_step,
)


def _appkit_verdict(*, passed: bool, fp: str) -> dict:
    return {
        "ok": passed,
        "passed": passed,
        "verdict": "pass" if passed else "fail",
        "summary": "all checks passed" if passed else "worker_contract FAILED",
        "next_action": "" if passed else "Gate /admin behind a bearer token.",
        "failure_fingerprint": fp,
        "url": "http://127.0.0.1:8000/",
        "http_status": 200,
        "console_errors": [],
        "network_failures": [],
        "screenshot_path": "shot.png",
        "checks": [{"name": "worker_contract", "passed": passed, "evidence": "x"}],
        "verify_web_app": {"url": "http://127.0.0.1:8000/"},
    }


def _appkit_primitive_fail_verdict() -> dict:
    verdict = _appkit_verdict(passed=False, fp="primitive_verify:template_only")
    verdict["summary"] = "primitive_verify:template_only FAILED"
    verdict["next_action"] = "Replace the template-only primitive or add a verifier."
    verdict["checks"] = [
        {
            "name": "primitive_verify:template_only",
            "passed": False,
            "evidence": "template_only primitives cannot ship without a verifier.",
        }
    ]
    return verdict


class AppKitVerifyExecutor(FakeExecutor):
    def __init__(self, verdicts):
        super().__init__(
            tools=[
                ToolSpec(name="app_create", description="scaffold", parameters_schema={}),
                ToolSpec(name="submit_plan", description="plan", parameters_schema={}),
                ToolSpec(name="verify_appkit_app", description="verify", parameters_schema={}),
            ]
        )
        self._verdicts = list(verdicts)
        self.appkit_calls = 0
        self.web_calls = 0

    async def execute(self, call):
        if call.tool_name == "verify_appkit_app":
            i = self.appkit_calls
            self.appkit_calls += 1
            verdict = self._verdicts[min(i, len(self._verdicts) - 1)]
            self.calls.append(call)
            return ToolResult(
                call_id=call.call_id,
                tool_name="verify_appkit_app",
                success=True,
                content=f"APPKIT {verdict['verdict']}",
                structured=verdict,
            )
        if call.tool_name == "verify_web_app":
            self.web_calls += 1
        return await super().execute(call)


def _gate_loop(
    agent,
    executor,
    *,
    mode: OperatingMode = OperatingMode.LONG_HORIZON,
    autonomous: bool = False,
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
        mode=mode,
        planning_tools=frozenset({"submit_plan"}),
        host_verify_authoritative=False,
        autonomous=autonomous,
    )
    return loop, store


def _statuses(events):
    return [(e.status.value, e.detail) for e in events if isinstance(e, StatusEvent)]


def _env(events):
    return [
        e.message.content
        for e in events
        if isinstance(e, MessageEvent) and e.source == EventSource.ENVIRONMENT and e.message
    ]


class _ToolsExecutor:
    def __init__(self, names):
        self._names = names

    def available_tools(self):
        return [ToolSpec(name=n, description="", parameters_schema={}) for n in self._names]


class _MiniLoop:
    def __init__(self, names):
        self.executor = _ToolsExecutor(names)


def test_active_verify_tool_prefers_appkit() -> None:
    gate = FinishGate(_MiniLoop(["verify_web_app", "verify_appkit_app", "browser"]))
    assert gate._active_verify_tool() == "verify_appkit_app"


def test_active_verify_tool_falls_back_to_web() -> None:
    gate = FinishGate(_MiniLoop(["verify_web_app", "browser"]))
    assert gate._active_verify_tool() == "verify_web_app"
    assert gate._verify_tool_available() is True


def test_active_verify_tool_none_when_browserless() -> None:
    gate = FinishGate(_MiniLoop(["shell", "file_write"]))
    assert gate._active_verify_tool() is None


def test_latest_verify_verdict_reads_appkit_observations() -> None:
    def obs(tool: str, fp: str, seq: int) -> ObservationEvent:
        res = ToolResult(
            call_id="c",
            tool_name=tool,
            success=True,
            content="v",
            structured={"failure_fingerprint": fp, "passed": True},
        )
        return ObservationEvent(tool_result=res, action_id="a").model_copy(update={"seq": seq})

    events = [obs("verify_web_app", "WEB", 15), obs("verify_appkit_app", "KIT", 16)]
    assert _latest_verify_verdict(events, 10)["failure_fingerprint"] == "WEB"
    verdict = _latest_verify_verdict(events, 10, None, "verify_appkit_app")
    assert verdict["failure_fingerprint"] == "KIT"


@pytest.mark.asyncio
async def test_appkit_build_finishes_on_appkit_pass_verdict() -> None:
    agent = ScriptedAgent(
        [
            action_step(tool="app_create", args={"recipe_id": "editorial-ledger"}),
            finish_step(),
        ]
    )
    execu = AppKitVerifyExecutor([_appkit_verdict(passed=True, fp="CLEAN")])
    loop, store = _gate_loop(agent, execu)
    await loop.send_message("build me a lead-gen app")
    await loop.run()

    events = await store.get_events("conv")
    assert execu.appkit_calls >= 1
    assert execu.web_calls == 0
    assert ("FINISHED", None) in _statuses(events)
    assert not any("did not pass" in m for m in _env(events))


@pytest.mark.asyncio
async def test_appkit_deadlock_sequence_replan_to_finish_terminates_cleanly() -> None:
    agent = ScriptedAgent(
        [
            action_step(
                tool="submit_plan",
                args={
                    "summary": "Build the AppKit app",
                    "steps": [{"title": "Create the AppKit app"}],
                },
            ),
            action_step(tool="app_create", args={"recipe_id": "editorial-ledger"}),
            action_step(
                tool="propose_plan_update",
                args={
                    "summary": "Finish the completed AppKit app",
                    "steps": [{"title": "Finish and hand off the AppKit app"}],
                },
            ),
            action_step(tool="finish", args={"summary": "done"}),
        ]
    )
    execu = AppKitVerifyExecutor([_appkit_verdict(passed=True, fp="CLEAN")])
    loop, store = _gate_loop(
        agent,
        execu,
        mode=OperatingMode.PLANNING,
        autonomous=True,
    )

    await loop.send_message("Build the app using the 'editorial-ledger' recipe.")
    state = await loop.run()

    events = await store.get_events("conv")
    statuses = _statuses(events)
    env = _env(events)
    assert state.execution_status.value == "FINISHED"
    assert ("FINISHED", None) in statuses
    assert not any(s in ("STUCK", "ERROR") for s, _ in statuses), statuses
    assert not any("quoted user literal is missing" in m for m in env), env
    assert not any("approved plan has not been executed" in m for m in env), env
    assert execu.appkit_calls >= 1
    assert all(call.tool_name != "shell" for call in execu.calls)


@pytest.mark.asyncio
async def test_appkit_finish_verify_arg_defers_to_appkit_verifier_without_shell() -> None:
    agent = ScriptedAgent(
        [
            action_step(tool="app_create", args={"recipe_id": "editorial-ledger"}),
            action_step(
                tool="finish",
                args={"summary": "done", "verify": "npm run build"},
            ),
        ]
    )
    execu = AppKitVerifyExecutor([_appkit_verdict(passed=True, fp="CLEAN")])
    loop, store = _gate_loop(agent, execu)

    await loop.send_message("build me a lead-gen app")
    await loop.run()

    events = await store.get_events("conv")
    assert execu.appkit_calls >= 1
    assert all(call.tool_name != "shell" for call in execu.calls)
    assert not any(
        isinstance(e, ActionEvent) and e.tool_call.tool_name == "shell" for e in events
    )
    assert ("FINISHED", None) in _statuses(events)


@pytest.mark.asyncio
async def test_appkit_build_refuses_on_appkit_fail_verdict() -> None:
    agent = ScriptedAgent(
        [
            action_step(tool="app_create", args={"recipe_id": "editorial-ledger"}),
            finish_step(),
        ]
    )
    execu = AppKitVerifyExecutor([_appkit_verdict(passed=False, fp="FP1")])
    loop, store = _gate_loop(agent, execu)
    await loop.send_message("build me a lead-gen app")
    await loop.run()

    events = await store.get_events("conv")
    assert execu.appkit_calls >= 1
    assert execu.web_calls == 0
    assert any("verify_appkit_app did not pass" in m for m in _env(events)), _env(events)


@pytest.mark.asyncio
async def test_appkit_failing_primitive_verify_refuses_finish() -> None:
    agent = ScriptedAgent(
        [
            action_step(tool="app_create", args={"recipe_id": "editorial-ledger"}),
            finish_step(),
        ]
    )
    execu = AppKitVerifyExecutor([_appkit_primitive_fail_verdict()])
    loop, store = _gate_loop(agent, execu)

    await loop.send_message("build me a lead-gen app")
    state = await loop.run()

    events = await store.get_events("conv")
    env = _env(events)
    statuses = _statuses(events)
    assert state.execution_status.value != "FINISHED"
    assert not any(s == "FINISHED" for s, _ in statuses), statuses
    assert any("verify_appkit_app did not pass" in m for m in env), env
    assert any("primitive_verify:template_only" in m for m in env), env
