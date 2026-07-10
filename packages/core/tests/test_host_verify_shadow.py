"""REL-1c host verifier shadow wiring."""

from __future__ import annotations

import asyncio

import pytest

from disco.core import (
    ActionEvent,
    ConversationStatus,
    NoOpCondenser,
    ObservationEvent,
    SqliteEventStore,
    ToolResult,
    VerifierShadowEvent,
    VerifierStartedEvent,
    VerifierVerdictEvent,
)
from disco.core.context import ArtifactMemoryStore
from disco.core.context.ledger import ArtifactRecord
from disco.core.llm import OperatingMode, ToolSpec
from disco.core.loop import AgentLoop, NeverConfirm
from loop_fakes import (
    FakeAnalyzer,
    FakeExecutor,
    FakeSummarizer,
    ScriptedAgent,
    action_step,
    finish_step,
)


def _verdict(*, passed: bool, fp: str = "FP", verdict: str | None = None) -> dict:
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
        "summary": "ok" if passed else "broken",
        "next_action": "" if passed else "fix it",
    }


class _VerifyExecutor(FakeExecutor):
    def __init__(self, verdict: dict):
        super().__init__(
            tools=[
                ToolSpec(name="file_write", description="write", parameters_schema={}),
                ToolSpec(name="serve", description="serve", parameters_schema={}),
                ToolSpec(name="submit_plan", description="plan", parameters_schema={}),
                ToolSpec(name="verify_web_app", description="verify", parameters_schema={}),
            ]
        )
        self._verdict = verdict
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
    def __init__(self, verdict: dict | None = None, *, delay_s: float = 0.0) -> None:
        self._verdict = verdict or _verdict(passed=True, fp="HOST")
        self._delay_s = delay_s
        self.calls = []

    async def verify(self, deliverable):
        self.calls.append(deliverable)
        if self._delay_s:
            await asyncio.sleep(self._delay_s)
        return self._verdict


class _PathSandbox:
    def __init__(self, existing: set[str]) -> None:
        self._existing = set(existing)

    async def file_exists(self, path: str) -> bool:
        return path in self._existing


class _ManifestSandbox(_PathSandbox):
    def __init__(self, existing: set[str]) -> None:
        super().__init__(existing)
        self.files: dict[str, bytes] = {}

    async def read_file(self, path: str) -> bytes:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    async def write_file(self, path: str, data: bytes) -> None:
        self.files[path] = data


def _web_agent() -> ScriptedAgent:
    return ScriptedAgent(
        [
            action_step(
                tool="file_write",
                args={"path": "index.html", "content": "<h1>hello</h1>"},
            ),
            finish_step(),
        ]
    )


def _root_deliverable_agent() -> ScriptedAgent:
    return ScriptedAgent(
        [
            action_step(
                tool="file_write",
                args={"path": "index.html", "content": "<h1>hello</h1>"},
            ),
            action_step(
                tool="serve",
                args={"title": "App", "path": ".", "kind": "app"},
            ),
            finish_step(),
        ]
    )


def _directory_deliverable_agent() -> ScriptedAgent:
    return ScriptedAgent(
        [
            action_step(
                tool="file_write",
                args={"path": "main.py", "content": "print(1)"},
            ),
            action_step(
                tool="serve",
                args={"title": "App", "path": "dist", "kind": "app"},
            ),
            finish_step(),
        ]
    )


def _non_web_agent() -> ScriptedAgent:
    return ScriptedAgent(
        [
            action_step(
                tool="file_write",
                args={"path": "main.py", "content": "print(1)"},
            ),
            finish_step(),
        ]
    )


def _loop(
    agent,
    executor,
    *,
    host_verifier=None,
    host_verify_timeout_s: float = 30.0,
    host_verifier_verdict_hook=None,
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
        host_verifier=host_verifier,
        host_verify_timeout_s=host_verify_timeout_s,
        host_verifier_verdict_hook=host_verifier_verdict_hook,
        # This file exercises the REL-1c SHADOW posture (advisory host verify).
        # Authoritative became the code default with the REL-1e flip, so pin
        # shadow explicitly; authoritative behavior is covered by
        # test_host_verify_authoritative.py.
        host_verify_authoritative=False,
    )
    return loop, store


def _statuses(events):
    return [getattr(e, "status", None) for e in events]


@pytest.mark.asyncio
async def test_shadow_gate_emits_verifier_events_in_order() -> None:
    host = _HostVerifier(_verdict(passed=True, fp="HOST"))
    execu = _VerifyExecutor(_verdict(passed=True, fp="INLINE"))
    loop, store = _loop(_web_agent(), execu, host_verifier=host)

    await loop.send_message("build a page")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED
    assert execu.verify_calls == 1
    assert len(host.calls) == 1
    assert host.calls[0].artifact_path == "index.html"

    events = await store.get_events("conv")
    verifier_events = [
        e
        for e in events
        if isinstance(e, (VerifierStartedEvent, VerifierShadowEvent, VerifierVerdictEvent))
    ]
    assert [type(e) for e in verifier_events] == [
        VerifierStartedEvent,
        VerifierShadowEvent,
        VerifierVerdictEvent,
    ]
    shadow = verifier_events[1]
    assert isinstance(shadow, VerifierShadowEvent)
    assert shadow.inline_verdict == "pass"
    assert shadow.host_verdict == "pass"
    assert shadow.agreement is True

    verdict = verifier_events[2]
    assert isinstance(verdict, VerifierVerdictEvent)
    assert verdict.verified is True
    assert verdict.verdict == "pass"


@pytest.mark.asyncio
async def test_shadow_gate_calls_verdict_hook_once() -> None:
    calls: list[VerifierVerdictEvent] = []

    async def hook(event: VerifierVerdictEvent) -> None:
        calls.append(event)

    host = _HostVerifier(_verdict(passed=True, fp="HOST"))
    execu = _VerifyExecutor(_verdict(passed=True, fp="INLINE"))
    loop, _store = _loop(
        _web_agent(),
        execu,
        host_verifier=host,
        host_verifier_verdict_hook=hook,
    )

    await loop.send_message("build a page")
    await loop.run()

    assert len(calls) == 1
    assert calls[0].artifact_path == "index.html"
    assert calls[0].verdict == "pass"


@pytest.mark.asyncio
async def test_root_deliverable_resolves_to_primary_artifact_for_host_verify() -> None:
    host = _HostVerifier(_verdict(passed=True, fp="HOST"))
    execu = _VerifyExecutor(_verdict(passed=True, fp="INLINE"))
    execu.sandbox = _PathSandbox({".", "index.html"})  # type: ignore[attr-defined]
    loop, store = _loop(_root_deliverable_agent(), execu, host_verifier=host)

    await loop.send_message("build a page")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED
    assert len(host.calls) == 1
    assert host.calls[0].artifact_path == "index.html"
    events = await store.get_events("conv")
    verifier_paths = [
        e.artifact_path
        for e in events
        if isinstance(e, (VerifierStartedEvent, VerifierShadowEvent, VerifierVerdictEvent))
    ]
    assert verifier_paths == ["index.html", "index.html", "index.html"]


@pytest.mark.asyncio
async def test_directory_deliverable_without_primary_artifact_skips_host_verify() -> None:
    host = _HostVerifier(_verdict(passed=True, fp="HOST"))
    execu = _VerifyExecutor(_verdict(passed=True, fp="INLINE"))
    execu.sandbox = _PathSandbox({"dist"})  # type: ignore[attr-defined]
    loop, store = _loop(_directory_deliverable_agent(), execu, host_verifier=host)

    await loop.send_message("build a page")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED
    assert host.calls == []
    events = await store.get_events("conv")
    assert not any(
        isinstance(e, (VerifierStartedEvent, VerifierShadowEvent, VerifierVerdictEvent))
        for e in events
    )


@pytest.mark.asyncio
async def test_manifest_app_record_resolves_host_verify_without_deliverable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DISCO_ARTIFACT_MANIFEST_READER", raising=False)
    monkeypatch.delenv("PMX_ARTIFACT_MANIFEST_READER", raising=False)
    host = _HostVerifier(_verdict(passed=True, fp="HOST"))
    execu = _VerifyExecutor(_verdict(passed=True, fp="INLINE"))
    sbx = _ManifestSandbox({"dist/index.html"})
    await ArtifactMemoryStore(sbx).upsert_artifact(
        ArtifactRecord(path="dist", kind="app", shown=True)
    )
    execu.sandbox = sbx  # type: ignore[attr-defined]
    loop, store = _loop(_non_web_agent(), execu, host_verifier=host)

    await loop.send_message("build a page")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED
    assert len(host.calls) == 1
    assert host.calls[0].artifact_path == "dist/index.html"
    events = await store.get_events("conv")
    verdicts = [e for e in events if isinstance(e, VerifierVerdictEvent)]
    assert len(verdicts) == 1
    assert verdicts[0].artifact_path == "dist/index.html"


@pytest.mark.asyncio
async def test_manifest_reader_explicit_off_preserves_event_only_host_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISCO_ARTIFACT_MANIFEST_READER", "off")
    host = _HostVerifier(_verdict(passed=True, fp="HOST"))
    execu = _VerifyExecutor(_verdict(passed=True, fp="INLINE"))
    sbx = _ManifestSandbox({"dist/index.html"})
    await ArtifactMemoryStore(sbx).upsert_artifact(
        ArtifactRecord(path="dist", kind="app", shown=True)
    )
    execu.sandbox = sbx  # type: ignore[attr-defined]
    loop, store = _loop(_non_web_agent(), execu, host_verifier=host)

    await loop.send_message("build a page")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED
    assert host.calls == []
    events = await store.get_events("conv")
    assert not any(isinstance(e, VerifierVerdictEvent) for e in events)


@pytest.mark.asyncio
async def test_empty_manifest_does_not_resolve_host_deliverable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DISCO_ARTIFACT_MANIFEST_READER", raising=False)
    monkeypatch.delenv("PMX_ARTIFACT_MANIFEST_READER", raising=False)
    host = _HostVerifier(_verdict(passed=True, fp="HOST"))
    execu = _VerifyExecutor(_verdict(passed=True, fp="INLINE"))
    sbx = _ManifestSandbox({"dist/index.html"})
    await ArtifactMemoryStore(sbx).record_artifacts(())
    execu.sandbox = sbx  # type: ignore[attr-defined]
    loop, store = _loop(_non_web_agent(), execu, host_verifier=host)

    await loop.send_message("build a page")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED
    assert host.calls == []
    events = await store.get_events("conv")
    assert not any(isinstance(e, VerifierVerdictEvent) for e in events)


@pytest.mark.asyncio
async def test_finish_outcome_identical_with_host_absent_present_or_timeout() -> None:
    cases = [
        None,
        _HostVerifier(_verdict(passed=False, fp="HOST_FAIL")),
        _HostVerifier(_verdict(passed=True, fp="SLOW"), delay_s=1.0),
    ]
    outcomes: list[ConversationStatus] = []
    for idx, host in enumerate(cases):
        execu = _VerifyExecutor(_verdict(passed=True, fp="INLINE"))
        loop, store = _loop(
            _web_agent(),
            execu,
            host_verifier=host,
            host_verify_timeout_s=0.001 if idx == 2 else 30.0,
        )
        await loop.send_message("build a page")
        state = await loop.run()
        events = await store.get_events("conv")
        outcomes.append(state.execution_status)
        assert ConversationStatus.FINISHED in _statuses(events)
        assert execu.verify_calls == 1

    assert outcomes == [
        ConversationStatus.FINISHED,
        ConversationStatus.FINISHED,
        ConversationStatus.FINISHED,
    ]


@pytest.mark.asyncio
async def test_non_web_deliverable_skips_host_verify() -> None:
    host = _HostVerifier()
    execu = _VerifyExecutor(_verdict(passed=True, fp="INLINE"))
    loop, store = _loop(_non_web_agent(), execu, host_verifier=host)

    await loop.send_message("write a script")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED
    assert host.calls == []
    assert execu.verify_calls == 0
    events = await store.get_events("conv")
    assert not any(
        isinstance(e, (VerifierStartedEvent, VerifierShadowEvent, VerifierVerdictEvent))
        for e in events
    )
    assert not any(
        isinstance(e, ActionEvent)
        and e.tool_call
        and e.tool_call.tool_name == "verify_web_app"
        for e in events
    )
    assert not any(
        isinstance(e, ObservationEvent) and e.tool_result.tool_name == "verify_web_app"
        for e in events
    )
