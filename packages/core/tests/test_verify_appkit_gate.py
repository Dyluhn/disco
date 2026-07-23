"""AppKit finish gate drives `verify_appkit_app` in strict AppKit mode."""

from __future__ import annotations

import hashlib
import json

import pytest
from disco.core import (
    ActionEvent,
    BuildPlatformAdmissionEvent,
    ConversationStatus,
    DeliverableEvent,
    LLMMessage,
    MessageEvent,
    NoOpCondenser,
    ObservationEvent,
    SqliteEventStore,
    StatusEvent,
    ToolResult,
    VerifierStartedEvent,
    VerifierVerdictEvent,
)
from disco.core.events import EventSource
from disco.core.llm import OperatingMode, ToolSpec
from disco.core.loop import AgentLoop, NeverConfirm
from disco.core.loop.finish import FinishGate, _latest_verify_verdict
from disco.core.verification import (
    AdmittedVerificationContract,
    HostVerificationClaim,
    VerificationCheckContract,
    VerificationClaimKind,
    VerificationDeliveryContract,
    VerificationRequestedClaim,
    VerificationRequirementsDirective,
    default_structured_web_claims,
    structured_web_verification_result,
)
from loop_fakes import (
    FakeAnalyzer,
    FakeExecutor,
    FakeSummarizer,
    ScriptedAgent,
    action_step,
    finish_step,
)


def _appkit_preview_runtime() -> dict:
    intent = {
        "command": None,
        "cwd": None,
        "framework": "vite",
        "launch_kind": "framework",
        "serve_dir": None,
    }
    payload = {
        "command": "npm run dev -- --port 8000 --host 0.0.0.0",
        "exec_dir": "/workspace",
        "intent": intent,
        "name": "appkit-live-vite",
        "port": 8000,
    }
    return {
        **payload,
        "status": "running",
        "projection_id": "pv_" + "a" * 32,
        "sandbox_instance_id": "sandbox-appkit",
        "sandbox_generation": 3,
        "url": "http://preview.appkit.test/",
        "launch_kind": "framework",
        "intent_digest": hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }


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
        "application_title": "Acme Leads",
        "canonical_entry_path": "dist/index.html",
        "preview_runtime": _appkit_preview_runtime(),
        "artifact_identity": {
            "scheme": "sha256-tree-manifest-v1",
            "digest": "sha256:" + "c" * 64,
            "entry_reference": "dist/index.html",
            "producer_id": "disco.appkit_strict_verifier@1",
        },
    }


def _appkit_contract(*, with_web: bool = False) -> AdmittedVerificationContract:
    strict = VerificationCheckContract(
        check_id="appkit_strict",
        receipt_kind="disco.appkit_strict@1",
        issuer_id="disco.appkit_strict_verifier@1",
        operation="host.verify_appkit_strict",
        required_execution_modality="appkit_strict_runtime",
        required_artifact_identity_scheme="sha256-tree-manifest-v1",
        accepted_claim_kinds=frozenset(
            {
                VerificationClaimKind.APPLICATION_IDENTITY,
                VerificationClaimKind.TARGET_SPECIFIC,
            }
        ),
        claims=(
            HostVerificationClaim(
                claim_id="appkit.strict_contract",
                kind=VerificationClaimKind.TARGET_SPECIFIC,
                expected="canonical AppKit entry passes the complete strict target verifier",
                source_authority="target.appkit.strict_floor@1",
            ),
        ),
    )
    checks = [strict]
    if with_web:
        checks.append(
            VerificationCheckContract(
                check_id="web_functional",
                receipt_kind="disco.web_functional@1",
                issuer_id="disco.host_web_verifier@1",
                operation="host.verify_deliverable",
                required_execution_modality="managed_preview",
                accepted_claim_kinds=frozenset(
                    {
                        VerificationClaimKind.ARTIFACT_IDENTITY,
                        VerificationClaimKind.HTTP_READY,
                        VerificationClaimKind.RENDERED_CONTENT,
                        VerificationClaimKind.VISIBLE_TEXT,
                        VerificationClaimKind.CONSOLE_CLEAN,
                        VerificationClaimKind.NETWORK_CLEAN,
                        VerificationClaimKind.INTERACTION,
                        VerificationClaimKind.ROUTE,
                        VerificationClaimKind.CONTRACT_SEMANTIC,
                        VerificationClaimKind.VISUAL_SEMANTIC,
                    }
                ),
                claims=default_structured_web_claims(),
            )
        )
    return AdmittedVerificationContract(
        target_id="disco.legacy_web@1",
        verifier_id="disco.appkit_strict_verifier@1",
        delivery=VerificationDeliveryContract(
            shape="web.legacy_deliverable",
            mode="interactive",
            entry_kind="deliverable_manifest",
            entry_reference="active-deliverable",
        ),
        preview_modality="legacy_host",
        checks=tuple(checks),
    )


def _appkit_admission(
    contract: AdmittedVerificationContract,
) -> BuildPlatformAdmissionEvent:
    return BuildPlatformAdmissionEvent(
        route="platform",
        profile_id="disco.appkit_web@1",
        run_intent_id="intent-appkit",
        composition_authority="build_platform_core",
        composition_digest="sha256:" + "a" * 64,
        run_identity="run:sha256:" + "b" * 64,
        verification_claims=contract.required_claims,
        verification_contract=contract,
    )


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
                ToolSpec(name="file_read", description="read", parameters_schema={}),
                ToolSpec(name="submit_plan", description="plan", parameters_schema={}),
                ToolSpec(name="verify_appkit_app", description="verify", parameters_schema={}),
            ]
        )
        self._verdicts = list(verdicts)
        self.appkit_phase = "edit"
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
                success=verdict.get("tool_success", True),
                content=f"APPKIT {verdict['verdict']}",
                structured=verdict,
            )
        if call.tool_name == "verify_web_app":
            self.web_calls += 1
        return await super().execute(call)

    async def verification_preflight(self, operation: str) -> dict[str, object] | None:
        if operation != "host.verify_appkit_strict":
            return None
        return {
            "artifact_path": "dist/index.html",
            "artifact_kind": "app",
            "execution_identity": {
                "modality": "appkit_strict_runtime",
                "instance_id": "pv_" + "a" * 32,
                "generation": "sandbox-appkit:3",
                "locator": "http://preview.appkit.test/",
            },
        }


def _gate_loop(
    agent,
    executor,
    *,
    mode: OperatingMode = OperatingMode.LONG_HORIZON,
    autonomous: bool = False,
    host_verifier=None,
    host_authoritative: bool = False,
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
        mode=mode,
        planning_tools=frozenset({"submit_plan"}),
        host_verifier=host_verifier,
        host_verify_authoritative=host_authoritative,
        autonomous=autonomous,
        finish_alias=finish_alias,
    )
    return loop, store


class _GenericPassingHostVerifier:
    def __init__(self) -> None:
        self.calls = 0

    async def verify(self, deliverable):  # noqa: ANN001
        self.calls += 1
        return {"passed": True, "verdict": "pass", "url": deliverable.deployment_url}


class _StructuredPassingHostVerifier:
    def __init__(self, *, rendered_text: str = "AppKit is ready") -> None:
        self.calls = []
        self.rendered_text = rendered_text

    async def verify(self, deliverable):  # noqa: ANN001
        self.calls.append(deliverable)
        assert deliverable.verification_check is not None
        assert deliverable.verification_check.check_id == "web_functional"
        assert deliverable.preview_selection is not None
        url = deliverable.preview_selection.verification_target_url(deliverable.artifact_path)
        assert url is not None
        verdict = {
            "passed": True,
            "verdict": "pass",
            "url": url,
            "http_status": 200,
            "meaningful_content": True,
            "rendered_text": self.rendered_text,
            "console_errors": [],
            "network_failures": [],
            "artifact_identity": {"preview_live_match": True},
            "freshness": {
                "executor_generation": deliverable.workspace_generation,
                "synchronized_epoch": deliverable.workspace_epoch,
            },
            "summary": "structured AppKit Preview passed",
        }
        verdict["artifact_identity"] = {
            "conversation_id": deliverable.conversation_id,
            "artifact_path": deliverable.artifact_path,
            "artifact_kind": deliverable.artifact_kind,
            "requested_url": deliverable.deployment_url,
            "observed_url": url,
            "preview_selection": deliverable.preview_selection.model_dump(mode="json"),
            "preview_live_match": True,
        }
        verdict["verification_result"] = structured_web_verification_result(
            deliverable=deliverable,
            verdict=verdict,
        ).model_dump(mode="json")
        return verdict


class _SequencedStructuredHostVerifier(_StructuredPassingHostVerifier):
    def __init__(self, rendered_texts: list[str]) -> None:
        super().__init__()
        self._rendered_texts = rendered_texts

    async def verify(self, deliverable):  # noqa: ANN001
        self.rendered_text = self._rendered_texts[
            min(len(self.calls), len(self._rendered_texts) - 1)
        ]
        return await super().verify(deliverable)


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
    await loop._emit(
        BuildPlatformAdmissionEvent(
            route="platform",
            profile_id="disco.appkit_web@1",
            run_intent_id="intent-appkit",
            composition_authority="build_platform_core",
            composition_digest="sha256:" + "a" * 64,
            run_identity="run:sha256:" + "b" * 64,
            verification_claims=default_structured_web_claims(),
        )
    )
    await loop.send_message("build me a lead-gen app")
    await loop.run()

    events = await store.get_events("conv")
    assert execu.appkit_calls >= 1
    assert execu.web_calls == 0
    assert ("FINISHED", None) in _statuses(events)
    assert not any("did not pass" in m for m in _env(events))


@pytest.mark.asyncio
async def test_platform_appkit_strict_pass_materializes_exact_target_handoff() -> None:
    contract = _appkit_contract()
    agent = ScriptedAgent(
        [
            action_step(tool="app_create", args={"recipe_id": "editorial-ledger"}),
            finish_step(),
        ]
    )
    execu = AppKitVerifyExecutor([_appkit_verdict(passed=True, fp="CLEAN")])
    loop, store = _gate_loop(agent, execu)
    await loop._emit(_appkit_admission(contract))
    await loop.send_message("build me a lead-gen app")

    state = await loop.run()
    events = await store.get_events("conv")
    handoffs = [event for event in events if isinstance(event, DeliverableEvent)]

    assert state.execution_status is ConversationStatus.FINISHED
    assert execu.appkit_calls == 1
    assert handoffs
    assert handoffs[-1].source is EventSource.SYSTEM
    assert handoffs[-1].path == "dist/index.html"
    assert handoffs[-1].target_id == contract.target_id
    assert handoffs[-1].verification_contract_digest == contract.digest
    assert not any(detail == "noop_limit" for _status, detail in _statuses(events))
    verdicts = [event for event in events if isinstance(event, VerifierVerdictEvent)]
    starts = [event for event in events if isinstance(event, VerifierStartedEvent)]
    assert len(starts) == 1
    assert len(verdicts) == 1
    verify_action = next(
        event
        for event in events
        if isinstance(event, ActionEvent) and event.tool_call.tool_name == "verify_appkit_app"
    )
    verify_observation = next(
        event
        for event in events
        if isinstance(event, ObservationEvent) and event.action_id == verify_action.id
    )
    assert (handoffs[-1].seq or 0) < (starts[0].seq or 0)
    assert (starts[0].seq or 0) < (verify_action.seq or 0)
    assert (verify_action.seq or 0) < (verify_observation.seq or 0)
    assert (verify_observation.seq or 0) < (verdicts[0].seq or 0)
    assert verdicts[0].requested_by_event_id == starts[0].id
    assert verdicts[0].verification_result is not None
    assert verdicts[0].verification_result.passed
    assert verdicts[0].verification_result.receipt_kind == "disco.appkit_strict@1"
    assert verdicts[0].verification_result.execution_identity is not None
    assert verdicts[0].verification_result.execution_identity.modality == "appkit_strict_runtime"
    assert verdicts[0].verification_result.artifact_identity is not None
    assert verdicts[0].verification_result.artifact_identity.entry_reference == "dist/index.html"


@pytest.mark.asyncio
async def test_direct_strict_pass_before_finish_is_reverified_under_typed_start() -> None:
    contract = _appkit_contract(with_web=True)
    host = _StructuredPassingHostVerifier()
    agent = ScriptedAgent(
        [
            action_step(tool="app_create", args={"recipe_id": "editorial-ledger"}),
            finish_step(),
            action_step(tool="verify_appkit_app", args={}),
            finish_step(),
        ]
    )
    execu = AppKitVerifyExecutor(
        [
            _appkit_verdict(passed=False, fp="REPAIR"),
            _appkit_verdict(passed=True, fp="DIRECT_PASS"),
            _appkit_verdict(passed=True, fp="GATE_PASS"),
        ]
    )
    loop, store = _gate_loop(agent, execu, host_verifier=host)
    await loop._emit(_appkit_admission(contract))
    await loop.send_message("build and verify the lead-gen app")

    state = await loop.run()
    events = await store.get_events("conv")
    starts = [event for event in events if isinstance(event, VerifierStartedEvent)]
    verdicts = [event for event in events if isinstance(event, VerifierVerdictEvent)]

    assert state.execution_status is ConversationStatus.FINISHED
    assert execu.appkit_calls == 3
    assert len(host.calls) == 1
    assert len(starts) == len(verdicts) == 3
    strict_start = next(start for start in reversed(starts) if start.check_id == "appkit_strict")
    strict_verdict = next(
        verdict
        for verdict in reversed(verdicts)
        if verdict.verification_result is not None
        and verdict.verification_result.receipt_kind == "disco.appkit_strict@1"
    )
    assert strict_verdict.requested_by_event_id == strict_start.id
    assert strict_verdict.verification_result is not None
    assert strict_verdict.verification_result.passed
    strict_actions = [
        event
        for event in events
        if isinstance(event, ActionEvent) and event.tool_call.tool_name == "verify_appkit_app"
    ]
    strict_observations = [
        event
        for event in events
        if isinstance(event, ObservationEvent) and event.action_id == strict_actions[-1].id
    ]
    assert (strict_start.seq or 0) < (strict_actions[-1].seq or 0)
    assert (strict_actions[-1].seq or 0) < (strict_observations[-1].seq or 0)
    assert (strict_observations[-1].seq or 0) < (strict_verdict.seq or 0)


@pytest.mark.asyncio
async def test_appkit_productive_repair_refreshes_exact_handoff_before_composed_checks() -> None:
    contract = _appkit_contract(with_web=True)
    host = _SequencedStructuredHostVerifier(["wrong title", "Final title"])
    agent = ScriptedAgent(
        [
            action_step(tool="app_create", args={"recipe_id": "editorial-ledger"}),
            finish_step(),
            action_step(
                tool="app_create",
                args={"recipe_id": "editorial-ledger", "overwrite": True},
            ),
            finish_step(),
        ]
    )
    execu = AppKitVerifyExecutor([_appkit_verdict(passed=True, fp="CLEAN")])
    loop, store = _gate_loop(agent, execu, host_verifier=host)
    await loop._emit(_appkit_admission(contract))
    await loop._emit(
        MessageEvent(
            source=EventSource.USER,
            message=LLMMessage(role="user", content="Build the app with the governed title."),
            verification_requirements=VerificationRequirementsDirective(
                claims=(
                    VerificationRequestedClaim(
                        claim_id="app.title",
                        kind=VerificationClaimKind.VISIBLE_TEXT,
                        expected="Final title",
                    ),
                )
            ),
        )
    )
    await loop.send_message("Build the app.")

    state = await loop.run()
    events = await store.get_events("conv")
    handoffs = [event for event in events if isinstance(event, DeliverableEvent)]
    create_actions = [
        event
        for event in events
        if isinstance(event, ActionEvent) and event.tool_call.tool_name == "app_create"
    ]
    second_create_observation = next(
        event
        for event in events
        if isinstance(event, ObservationEvent) and event.action_id == create_actions[-1].id
    )

    assert state.execution_status is ConversationStatus.FINISHED
    assert len(host.calls) == 2
    assert len(handoffs) == 2
    assert (handoffs[0].seq or 0) < (second_create_observation.seq or 0)
    assert (second_create_observation.seq or 0) < (handoffs[-1].seq or 0)
    assert host.calls[-1].deliverable_event_id == handoffs[-1].id
    assert host.calls[-1].preview_selection is not None


@pytest.mark.asyncio
async def test_strict_admitted_contract_survives_executor_phase_signal_change() -> None:
    """Durable target authority, not a transient tool-surface phase, selects the gate."""

    contract = _appkit_contract()
    agent = ScriptedAgent(
        [
            action_step(tool="app_create", args={"recipe_id": "editorial-ledger"}),
            finish_step(),
        ]
    )
    execu = AppKitVerifyExecutor([_appkit_verdict(passed=True, fp="CLEAN")])
    execu.appkit_phase = None
    loop, _store = _gate_loop(agent, execu)
    await loop._emit(_appkit_admission(contract))
    await loop.send_message("build me a lead-gen app")

    state = await loop.run()

    assert state.execution_status is ConversationStatus.FINISHED
    assert execu.appkit_calls == 1
    assert execu.web_calls == 0


@pytest.mark.asyncio
async def test_platform_appkit_unavailable_verifier_never_releases_unverified() -> None:
    contract = _appkit_contract()
    unavailable = _appkit_verdict(passed=False, fp="UNAVAILABLE")
    unavailable["tool_success"] = False
    agent = ScriptedAgent(
        [
            action_step(tool="app_create", args={"recipe_id": "editorial-ledger"}),
            finish_step(),
            finish_step(),
        ]
    )
    execu = AppKitVerifyExecutor([unavailable])
    loop, store = _gate_loop(agent, execu)
    await loop._emit(_appkit_admission(contract))
    await loop.send_message("build me a lead-gen app")

    state = await loop.run()
    events = await store.get_events("conv")

    assert state.execution_status is not ConversationStatus.FINISHED
    assert any(status == "STUCK" for status, _detail in _statuses(events))
    assert not any(detail == "unverified_release" for _status, detail in _statuses(events))
    assert not any(status == "FINISHED" for status, _detail in _statuses(events))
    starts = [event for event in events if isinstance(event, VerifierStartedEvent)]
    verdicts = [event for event in events if isinstance(event, VerifierVerdictEvent)]
    assert len(starts) == len(verdicts)
    assert len(starts) >= 2
    assert all(
        verdict.requested_by_event_id == start.id
        for start, verdict in zip(starts, verdicts, strict=True)
    )
    assert all(
        verdict.verification_result is not None
        and verdict.verification_result.status.value == "unavailable"
        for verdict in verdicts
    )
    stuck = next(
        event
        for event in reversed(events)
        if isinstance(event, StatusEvent) and event.status is ConversationStatus.STUCK
    )
    assert (verdicts[-1].seq or 0) < (stuck.seq or 0)


@pytest.mark.asyncio
async def test_platform_appkit_failures_pair_every_typed_start_with_fail_verdict() -> None:
    contract = _appkit_contract()
    failed = _appkit_verdict(passed=False, fp="STRICT_FAIL")
    agent = ScriptedAgent(
        [
            action_step(tool="app_create", args={"recipe_id": "editorial-ledger"}),
            finish_step(),
            finish_step(),
        ]
    )
    execu = AppKitVerifyExecutor([failed])
    loop, store = _gate_loop(agent, execu)
    await loop._emit(_appkit_admission(contract))
    await loop.send_message("build me a lead-gen app")

    state = await loop.run()
    events = await store.get_events("conv")
    starts = [event for event in events if isinstance(event, VerifierStartedEvent)]
    verdicts = [event for event in events if isinstance(event, VerifierVerdictEvent)]

    assert state.execution_status is not ConversationStatus.FINISHED
    assert len(starts) == len(verdicts)
    assert len(starts) >= 2
    assert all(
        verdict.requested_by_event_id == start.id
        for start, verdict in zip(starts, verdicts, strict=True)
    )
    assert all(
        verdict.verification_result is not None
        and verdict.verification_result.status.value == "fail"
        for verdict in verdicts
    )
    stuck = next(
        event
        for event in reversed(events)
        if isinstance(event, StatusEvent) and event.status is ConversationStatus.STUCK
    )
    assert (verdicts[-1].seq or 0) < (stuck.seq or 0)


@pytest.mark.asyncio
async def test_platform_appkit_raw_pass_without_artifact_seal_cannot_finish() -> None:
    contract = _appkit_contract()
    unsealed = _appkit_verdict(passed=True, fp="UNSEALED")
    unsealed.pop("artifact_identity")
    agent = ScriptedAgent(
        [
            action_step(tool="app_create", args={"recipe_id": "editorial-ledger"}),
            finish_step(),
            finish_step(),
        ]
    )
    execu = AppKitVerifyExecutor([unsealed])
    loop, store = _gate_loop(agent, execu)
    await loop._emit(_appkit_admission(contract))
    await loop.send_message("build me a lead-gen app")

    state = await loop.run()
    events = await store.get_events("conv")
    typed = [
        event.verification_result
        for event in events
        if isinstance(event, VerifierVerdictEvent) and event.verification_result is not None
    ]

    assert state.execution_status is not ConversationStatus.FINISHED
    assert typed
    assert all(result.status.value == "unavailable" for result in typed)
    assert any("immutable artifact identity" in result.reason for result in typed)


@pytest.mark.asyncio
async def test_platform_appkit_raw_pass_with_wrong_seal_scheme_cannot_finish() -> None:
    contract = _appkit_contract()
    wrong_scheme = _appkit_verdict(passed=True, fp="WRONG-SCHEME")
    wrong_scheme["artifact_identity"]["scheme"] = "opaque-unverified"
    agent = ScriptedAgent(
        [
            action_step(tool="app_create", args={"recipe_id": "editorial-ledger"}),
            finish_step(),
            finish_step(),
        ]
    )
    execu = AppKitVerifyExecutor([wrong_scheme])
    loop, store = _gate_loop(agent, execu)
    await loop._emit(_appkit_admission(contract))
    await loop.send_message("build me a lead-gen app")

    state = await loop.run()
    events = await store.get_events("conv")
    typed = [
        event.verification_result
        for event in events
        if isinstance(event, VerifierVerdictEvent) and event.verification_result is not None
    ]

    assert state.execution_status is not ConversationStatus.FINISHED
    assert typed
    assert all(result.status.value == "unavailable" for result in typed)
    assert any("artifact producer authority" in result.reason for result in typed)


@pytest.mark.asyncio
async def test_platform_appkit_does_not_drop_external_mandatory_claim() -> None:
    contract = _appkit_contract(with_web=True)
    host = _StructuredPassingHostVerifier(rendered_text="Account ready")
    agent = ScriptedAgent(
        [
            action_step(tool="app_create", args={"recipe_id": "editorial-ledger"}),
            finish_step(),
            finish_step(),
        ]
    )
    execu = AppKitVerifyExecutor([_appkit_verdict(passed=True, fp="CLEAN")])
    loop, store = _gate_loop(agent, execu, host_verifier=host)
    await loop._emit(_appkit_admission(contract))
    await loop._emit(
        MessageEvent(
            source=EventSource.USER,
            message=LLMMessage(role="user", content="The app must show Account ready."),
            verification_requirements=VerificationRequirementsDirective(
                claims=(
                    VerificationRequestedClaim(
                        claim_id="appkit.visible_text:account_ready",
                        kind=VerificationClaimKind.VISIBLE_TEXT,
                        expected="Account ready",
                    ),
                )
            ),
        )
    )
    await loop.send_message("build me a lead-gen app")

    state = await loop.run()
    events = await store.get_events("conv")

    assert state.execution_status is ConversationStatus.FINISHED
    assert execu.appkit_calls == 1
    assert len(host.calls) == 1
    verdicts = [event for event in events if isinstance(event, VerifierVerdictEvent)]
    assert {
        event.verification_result.receipt_kind
        for event in verdicts
        if event.verification_result is not None and event.verification_result.passed
    } == {"disco.appkit_strict@1", "disco.web_functional@1"}


@pytest.mark.asyncio
async def test_platform_appkit_strict_only_cannot_release_unowned_required_claim() -> None:
    contract = _appkit_contract()
    agent = ScriptedAgent(
        [
            action_step(tool="app_create", args={"recipe_id": "editorial-ledger"}),
            finish_step(),
            finish_step(),
        ]
    )
    execu = AppKitVerifyExecutor([_appkit_verdict(passed=True, fp="CLEAN")])
    loop, store = _gate_loop(agent, execu)
    await loop._emit(_appkit_admission(contract))
    await loop._emit(
        MessageEvent(
            source=EventSource.USER,
            message=LLMMessage(role="user", content="The app must show Account ready."),
            verification_requirements=VerificationRequirementsDirective(
                claims=(
                    VerificationRequestedClaim(
                        claim_id="appkit.visible_text:account_ready",
                        kind=VerificationClaimKind.VISIBLE_TEXT,
                        expected="Account ready",
                    ),
                )
            ),
        )
    )
    await loop.send_message("build me a lead-gen app")

    state = await loop.run()
    events = await store.get_events("conv")

    assert state.execution_status is not ConversationStatus.FINISHED
    assert any(status == "STUCK" for status, _detail in _statuses(events))
    assert not any(status == "FINISHED" for status, _detail in _statuses(events))
    assert any("has 0 admitted verifier owners" in message for message in _env(events))


@pytest.mark.asyncio
async def test_platform_appkit_application_identity_uses_appspec_not_visible_copy() -> None:
    contract = _appkit_contract()
    mismatched = _appkit_verdict(passed=True, fp="CLEAN")
    mismatched["application_title"] = "Old title"
    exact = _appkit_verdict(passed=True, fp="CLEAN")
    exact["application_title"] = "Revised title"
    agent = ScriptedAgent(
        [
            action_step(tool="app_create", args={"recipe_id": "editorial-ledger"}),
            finish_step(),
            action_step(
                tool="app_create",
                args={"recipe_id": "editorial-ledger", "overwrite": True},
            ),
            finish_step(),
        ]
    )
    execu = AppKitVerifyExecutor([mismatched, exact])
    loop, store = _gate_loop(agent, execu)
    await loop._emit(_appkit_admission(contract))
    await loop._emit(
        MessageEvent(
            source=EventSource.USER,
            message=LLMMessage(role="user", content="Set the application title."),
            verification_requirements=VerificationRequirementsDirective(
                claims=(
                    VerificationRequestedClaim(
                        claim_id="application.title:revised",
                        kind=VerificationClaimKind.APPLICATION_IDENTITY,
                        expected="Revised title",
                    ),
                )
            ),
        )
    )
    await loop.send_message("build me a lead-gen app")

    state = await loop.run()
    events = await store.get_events("conv")
    typed = [
        event.verification_result
        for event in events
        if isinstance(event, VerifierVerdictEvent) and event.verification_result is not None
    ]

    assert state.execution_status is ConversationStatus.FINISHED
    assert len(typed) == 2
    assert typed[0].status.value == "fail"
    assert typed[1].status.value == "pass"
    first_identity = next(
        result
        for result in typed[0].claim_results
        if result.kind is VerificationClaimKind.APPLICATION_IDENTITY
    )
    assert first_identity.status.value == "fail"
    assert first_identity.reason == "authoritative AppSpec name is 'Old title'"


@pytest.mark.asyncio
async def test_platform_contract_does_not_invent_unadmitted_semantic_claim() -> None:
    contract = _appkit_contract(with_web=True)
    host = _StructuredPassingHostVerifier()
    agent = ScriptedAgent(
        [
            action_step(tool="app_create", args={"recipe_id": "editorial-ledger"}),
            finish_step(),
        ]
    )
    execu = AppKitVerifyExecutor([_appkit_verdict(passed=True, fp="CLEAN")])
    loop, store = _gate_loop(
        agent,
        execu,
        host_verifier=host,
        finish_alias="ready_for_app_verification",
    )
    await loop._emit(_appkit_admission(contract))
    await loop.send_message("build me a lead-gen app")

    state = await loop.run()
    events = await store.get_events("conv")

    assert state.execution_status is ConversationStatus.FINISHED
    assert len(host.calls) == 1
    assert {claim.claim_id for claim in host.calls[0].required_claims} == {
        claim.claim_id for claim in contract.checks[1].claims
    }
    assert all(
        result.kind is not VerificationClaimKind.CONTRACT_SEMANTIC
        for event in events
        if isinstance(event, VerifierVerdictEvent) and event.verification_result is not None
        for result in event.verification_result.claim_results
    )


@pytest.mark.asyncio
async def test_platform_appkit_rejects_unimplemented_second_required_check() -> None:
    base = _appkit_contract()
    extra = VerificationCheckContract(
        check_id="device_policy",
        receipt_kind="synthetic.device_policy@1",
        issuer_id="synthetic.device_policy_verifier@1",
        operation="host.verify_device_policy",
        required_execution_modality="device_runtime",
        accepted_claim_kinds=frozenset({VerificationClaimKind.TARGET_SPECIFIC}),
        claims=(
            HostVerificationClaim(
                claim_id="appkit.device_policy",
                kind=VerificationClaimKind.TARGET_SPECIFIC,
                expected="device policy passes",
                source_authority="target.appkit.device_policy@1",
            ),
        ),
    )
    contract = base.model_copy(update={"checks": (*base.checks, extra)})
    agent = ScriptedAgent(
        [
            action_step(tool="app_create", args={"recipe_id": "editorial-ledger"}),
            finish_step(),
            finish_step(),
        ]
    )
    execu = AppKitVerifyExecutor([_appkit_verdict(passed=True, fp="CLEAN")])
    loop, store = _gate_loop(agent, execu)
    await loop._emit(_appkit_admission(contract))
    await loop.send_message("build me a lead-gen app")

    state = await loop.run()
    events = await store.get_events("conv")

    assert state.execution_status is not ConversationStatus.FINISHED
    assert execu.appkit_calls >= 1
    assert any(status == "STUCK" for status, _detail in _statuses(events))


@pytest.mark.asyncio
async def test_platform_appkit_optional_unowned_claim_does_not_block_required_floor() -> None:
    contract = _appkit_contract()
    agent = ScriptedAgent(
        [
            action_step(tool="app_create", args={"recipe_id": "editorial-ledger"}),
            finish_step(),
        ]
    )
    execu = AppKitVerifyExecutor([_appkit_verdict(passed=True, fp="CLEAN")])
    loop, store = _gate_loop(agent, execu)
    await loop._emit(_appkit_admission(contract))
    await loop._emit(
        MessageEvent(
            source=EventSource.USER,
            message=LLMMessage(role="user", content="Optionally note Account ready."),
            verification_requirements=VerificationRequirementsDirective(
                claims=(
                    VerificationRequestedClaim(
                        claim_id="appkit.optional_text:account_ready",
                        kind=VerificationClaimKind.VISIBLE_TEXT,
                        required=False,
                        expected="Account ready",
                    ),
                )
            ),
        )
    )
    await loop.send_message("build me a lead-gen app")

    state = await loop.run()

    assert state.execution_status is ConversationStatus.FINISHED
    assert execu.appkit_calls == 1


@pytest.mark.asyncio
async def test_generic_host_pass_cannot_waive_strict_appkit_verifier() -> None:
    host = _GenericPassingHostVerifier()
    agent = ScriptedAgent(
        [
            action_step(tool="app_create", args={"recipe_id": "editorial-ledger"}),
            finish_step(),
        ]
    )
    execu = AppKitVerifyExecutor([_appkit_verdict(passed=True, fp="CLEAN")])
    loop, store = _gate_loop(
        agent,
        execu,
        host_verifier=host,
        host_authoritative=True,
    )

    await loop.send_message("build me a lead-gen app")
    await loop.run()

    assert host.calls == 0
    assert execu.appkit_calls >= 1
    assert ("FINISHED", None) in _statuses(await store.get_events("conv"))


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
    assert not any(isinstance(e, ActionEvent) and e.tool_call.tool_name == "shell" for e in events)
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
async def test_appkit_direct_failed_verifier_diagnostics_halt_autonomous_no_progress() -> None:
    agent = ScriptedAgent(
        [
            action_step(tool="app_create", args={"recipe_id": "editorial-ledger"}),
            action_step(tool="verify_appkit_app"),
            action_step(tool="file_read", args={"path": "src/App.tsx"}),
            action_step(tool="verify_appkit_app"),
            action_step(tool="file_read", args={"path": "worker.ts"}),
        ]
    )
    execu = AppKitVerifyExecutor([_appkit_verdict(passed=False, fp="UNCHANGED")])
    loop, store = _gate_loop(agent, execu, autonomous=True)

    await loop.send_message("build me a lead-gen app")
    state = await loop.run()

    events = await store.get_events("conv")
    statuses = _statuses(events)
    assert state.execution_status.value == "STUCK"
    assert execu.appkit_calls == 2
    assert not any(status == "FINISHED" for status, _detail in statuses)
    assert any(
        detail == "verifier_no_progress"
        or (detail or "").startswith("verifier_no_progress:verify_appkit_app:")
        for _status, detail in statuses
    )


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
