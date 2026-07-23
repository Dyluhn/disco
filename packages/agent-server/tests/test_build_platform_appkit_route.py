from __future__ import annotations

from unittest import mock

import pytest
from disco.agent_server.build_platform_shadow import BuildPlatformRouteError
from disco.agent_server.runtime import ConversationRuntime
from disco.core import BuildPlatformAdmissionEvent, SqliteEventStore, WorkspaceMutationEvent
from disco.core.build_platform import APPKIT_PROFILE_ID, PolicyDecision
from disco.core.llm import DefaultLLMRouter
from disco.core.loop import RouterAgent
from disco.tools import AppKitToolExecutor


def _compose(
    monkeypatch: pytest.MonkeyPatch,
    conversation_id: str,
    *,
    platform: bool,
):
    if platform:
        monkeypatch.delenv("DISCO_APPKIT_PLATFORM_ROUTE", raising=False)
    else:
        monkeypatch.setenv("DISCO_APPKIT_PLATFORM_ROUTE", "0")
    runtime = ConversationRuntime(SqliteEventStore(":memory:"))
    runtime.set_surface(conversation_id, "agent")
    runtime.set_appkit_mode(conversation_id, True)
    with mock.patch.object(runtime, "_sandbox_service_now"):
        loop = runtime._compose_build_loop(
            conversation_id,
            mock.MagicMock(spec=DefaultLLMRouter),
            mock.MagicMock(spec=RouterAgent),
        )
    return runtime, loop


def _loop_contract(loop) -> tuple[object, ...]:
    return (
        type(loop.executor),
        tuple(tool.name for tool in loop.executor.available_tools()),
        loop.mode,
        type(loop.policy),
        type(loop.condenser),
        type(loop._host_verifier),
        loop._strict_appkit_active_reader is not None,
    )


def test_appkit_defaults_to_exact_platform_composition_with_legacy_effect_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_runtime, legacy_loop = _compose(monkeypatch, "legacy-appkit", platform=False)
    platform_runtime, platform_loop = _compose(monkeypatch, "platform-appkit", platform=True)

    record = platform_runtime._build_platform.route_records["platform-appkit"]
    assert record.source == "appkit"
    assert record.active_route == "platform"
    assert record.execution_bridge == "legacy_host"
    assert record.rollback_switch == "DISCO_APPKIT_PLATFORM_ROUTE"
    assert record.composition.profile.id == APPKIT_PROFILE_ID
    assert all(comparison.matches for comparison in record.comparisons)
    assert record.composition.blocked_operations == ()
    rules = {rule.key: rule.decision for rule in record.composition.effective_policy.rules}
    assert rules["mutation.raw_files"] is PolicyDecision.DENY
    assert rules["mutation.semantic"] is PolicyDecision.ALLOW
    assert _loop_contract(platform_loop) == _loop_contract(legacy_loop)
    assert isinstance(platform_loop.executor, AppKitToolExecutor)
    assert legacy_runtime._build_platform.route_records == {}
    assert legacy_runtime._build_platform.selected_routes["legacy-appkit"] == "legacy"


def test_appkit_platform_resolution_failure_has_no_freeform_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DISCO_APPKIT_PLATFORM_ROUTE", raising=False)
    runtime = ConversationRuntime(SqliteEventStore(":memory:"))
    runtime.set_surface("blocked-appkit", "agent")
    runtime.set_appkit_mode("blocked-appkit", True)
    with (
        mock.patch.object(runtime, "_sandbox_service_now"),
        mock.patch(
            "disco.agent_server.build_platform_runtime.select_appkit_platform_route",
            side_effect=BuildPlatformRouteError("strict parity mismatch"),
        ),
        pytest.raises(BuildPlatformRouteError, match="strict parity mismatch"),
    ):
        runtime._compose_build_loop(
            "blocked-appkit",
            mock.MagicMock(spec=DefaultLLMRouter),
            mock.MagicMock(spec=RouterAgent),
        )
    assert runtime._executors == {}
    assert runtime._build_platform.route_records == {}
    assert runtime._build_platform.selected_routes == {}


@pytest.mark.asyncio
async def test_appkit_platform_admission_is_durable_and_rollback_is_new_run_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _loop = _compose(monkeypatch, "durable-appkit", platform=True)
    intent = await runtime._store.append(
        "durable-appkit",
        WorkspaceMutationEvent(
            operation="agent.run-intent.message",
            run_protocol_version=1,
        ),
    )
    await runtime._build_platform.record_route_locked("durable-appkit")
    admissions = [
        event
        for event in await runtime._store.get_events("durable-appkit")
        if isinstance(event, BuildPlatformAdmissionEvent)
    ]
    assert len(admissions) == 1
    assert admissions[0].route == "platform"
    assert admissions[0].profile_id == APPKIT_PROFILE_ID.canonical
    assert admissions[0].run_intent_id == intent.id
    contract = admissions[0].verification_contract
    assert contract is not None
    assert [
        (check.check_id, check.receipt_kind, check.issuer_id, check.operation)
        for check in contract.checks
    ] == [
        (
            "appkit_strict",
            "disco.appkit_strict@1",
            "disco.appkit_strict_verifier@1",
            "host.verify_appkit_strict",
        )
    ]
    assert [claim.claim_id for claim in contract.required_claims] == ["appkit.strict_contract"]

    monkeypatch.setenv("DISCO_APPKIT_PLATFORM_ROUTE", "0")
    restarted = ConversationRuntime(runtime._store)
    restarted.set_surface("durable-appkit", "agent")
    await restarted._build_platform.prepare_route_pin("durable-appkit")
    with mock.patch.object(restarted, "_sandbox_service_now"):
        restarted._compose_build_loop(
            "durable-appkit",
            mock.MagicMock(spec=DefaultLLMRouter),
            mock.MagicMock(spec=RouterAgent),
        )
    assert restarted._build_platform.selected_routes["durable-appkit"] == "platform"
    assert restarted._build_platform.route_records["durable-appkit"].source == "appkit"
    replayed = [
        event
        for event in await restarted._store.get_events("durable-appkit")
        if isinstance(event, BuildPlatformAdmissionEvent)
    ][0]
    assert replayed.verification_contract == contract
