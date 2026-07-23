from __future__ import annotations

from unittest import mock

import pytest
from disco.agent_server.build_platform_shadow import BuildPlatformRouteError
from disco.agent_server.runtime import ConversationRuntime
from disco.core import (
    BuildPlatformAdmissionEvent,
    ConversationStatus,
    SqliteEventStore,
    StatusEvent,
    WorkspaceMutationEvent,
)
from disco.core.build_platform import APPKIT_PROFILE_ID
from disco.core.llm import DefaultLLMRouter, OperatingMode
from disco.core.loop import RouterAgent
from disco.tools import AppKitToolExecutor, DefaultToolExecutor


def _compose(
    monkeypatch: pytest.MonkeyPatch,
    conversation_id: str,
    *,
    platform: bool,
    appkit: bool = False,
):
    if platform:
        monkeypatch.setenv("DISCO_FREEFORM_PLATFORM_ROUTE", "1")
    else:
        monkeypatch.delenv("DISCO_FREEFORM_PLATFORM_ROUTE", raising=False)
    runtime = ConversationRuntime(SqliteEventStore(":memory:"))
    runtime.set_surface(conversation_id, "agent")
    runtime.set_appkit_mode(conversation_id, appkit)
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
        loop._planning_tools,
        type(loop.policy),
        type(loop.condenser),
        type(loop._host_verifier),
        loop._finish_alias,
        loop._strict_appkit_active_reader is not None,
    )


def test_freeform_opt_in_selects_platform_with_exact_legacy_execution_bridge(monkeypatch) -> None:
    legacy_runtime, legacy_loop = _compose(monkeypatch, "legacy", platform=False)
    platform_runtime, platform_loop = _compose(monkeypatch, "platform", platform=True)

    record = platform_runtime._build_platform.route_records["platform"]
    assert record.active_route == "platform"
    assert record.composition_authority == "build_platform_core"
    assert record.execution_bridge == "legacy_host"
    assert record.composition_digest == record.composition.digest.digest
    assert record.composition.blocked_operations == ()
    assert all(comparison.matches for comparison in record.comparisons)
    assert _loop_contract(platform_loop) == _loop_contract(legacy_loop)
    assert isinstance(platform_loop.executor, DefaultToolExecutor)
    assert platform_loop.mode is OperatingMode.PLANNING
    assert platform_loop.executor._sandbox._auto_preview_disabled is True
    assert legacy_loop.executor._sandbox._auto_preview_disabled is True
    assert legacy_runtime._build_platform.route_records == {}


def test_cutover_is_pinned_to_the_existing_loop_and_switch_is_new_run_only(monkeypatch) -> None:
    runtime, loop = _compose(monkeypatch, "pinned", platform=True)
    runtime._loops["pinned"] = loop
    monkeypatch.delenv("DISCO_FREEFORM_PLATFORM_ROUTE", raising=False)
    assert runtime._loop_for("pinned") is loop
    assert runtime._build_platform.route_records["pinned"].active_route == "platform"

    rollback_runtime, rollback_loop = _compose(monkeypatch, "rollback", platform=False)
    assert rollback_runtime._build_platform.route_records == {}
    assert isinstance(rollback_loop.executor, DefaultToolExecutor)


def test_appkit_is_not_silently_routed_through_freeform_opt_in(monkeypatch) -> None:
    runtime, loop = _compose(monkeypatch, "strict", platform=True, appkit=True)
    record = runtime._build_platform.route_records["strict"]
    assert record.source == "appkit"
    assert record.composition.profile.id == APPKIT_PROFILE_ID
    assert isinstance(loop.executor, AppKitToolExecutor)


def test_platform_resolution_failure_blocks_without_legacy_fallback(monkeypatch) -> None:
    monkeypatch.setenv("DISCO_FREEFORM_PLATFORM_ROUTE", "1")
    runtime = ConversationRuntime(SqliteEventStore(":memory:"))
    runtime.set_surface("blocked", "agent")
    with (
        mock.patch.object(runtime, "_sandbox_service_now"),
        mock.patch(
            "disco.agent_server.build_platform_runtime.select_freeform_platform_route",
            side_effect=BuildPlatformRouteError("parity mismatch"),
        ),
        pytest.raises(BuildPlatformRouteError, match="parity mismatch"),
    ):
        runtime._compose_build_loop(
            "blocked",
            mock.MagicMock(spec=DefaultLLMRouter),
            mock.MagicMock(spec=RouterAgent),
        )
    assert "blocked" not in runtime._executors
    assert "blocked" not in runtime._loops
    assert runtime._build_platform.route_records == {}


def test_artifact_and_workflow_profiles_remain_outside_freeform_cutover(monkeypatch) -> None:
    monkeypatch.setenv("DISCO_FREEFORM_PLATFORM_ROUTE", "1")
    runtime = ConversationRuntime(SqliteEventStore(":memory:"))
    runtime.set_surface("artifact", "build")
    runtime.set_artifact_mode("artifact", True)
    with mock.patch.object(runtime, "_sandbox_service_now"):
        loop = runtime._compose_build_loop(
            "artifact",
            mock.MagicMock(spec=DefaultLLMRouter),
            mock.MagicMock(spec=RouterAgent),
        )
    assert runtime._build_platform.route_records == {}
    assert isinstance(loop.executor, DefaultToolExecutor)
    assert loop.mode is OperatingMode.INTERACTIVE


@pytest.mark.asyncio
async def test_platform_admission_is_durable_idempotent_and_restart_pinned(monkeypatch) -> None:
    runtime, _loop = _compose(monkeypatch, "durable", platform=True)
    intent = await runtime._store.append(
        "durable",
        WorkspaceMutationEvent(
            operation="agent.run-intent.message",
            run_protocol_version=1,
        ),
    )
    await runtime._build_platform.record_route_locked("durable")
    await runtime._build_platform.record_route_locked("durable")
    admissions = [
        event
        for event in await runtime._store.get_events("durable")
        if isinstance(event, BuildPlatformAdmissionEvent)
    ]
    assert len(admissions) == 1
    admission = admissions[0]
    assert admission.route == "platform"
    assert admission.run_intent_id == intent.id
    assert admission.composition_digest is not None
    assert admission.run_identity is not None
    assert {claim.kind.value for claim in admission.verification_claims} == {
        "artifact_identity",
        "http_ready",
        "rendered_content",
        "console_clean",
        "network_clean",
    }

    monkeypatch.delenv("DISCO_FREEFORM_PLATFORM_ROUTE", raising=False)
    restarted = ConversationRuntime(runtime._store)
    restarted.set_surface("durable", "agent")
    await restarted._build_platform.prepare_route_pin("durable")
    assert restarted._build_platform.route_pins["durable"] == "platform"
    with mock.patch.object(restarted, "_sandbox_service_now"):
        restarted._compose_build_loop(
            "durable",
            mock.MagicMock(spec=DefaultLLMRouter),
            mock.MagicMock(spec=RouterAgent),
        )
    assert restarted._build_platform.route_records["durable"].active_route == "platform"


@pytest.mark.asyncio
async def test_legacy_park_is_pinned_but_terminal_run_releases_rollout_choice(monkeypatch) -> None:
    runtime, _loop = _compose(monkeypatch, "legacy-pin", platform=False)
    await runtime._build_platform.record_route_locked("legacy-pin")
    route_events = await runtime._store.get_events("legacy-pin")
    generated_intents = [
        event
        for event in route_events
        if isinstance(event, WorkspaceMutationEvent)
        and event.operation == "agent.run-intent.runtime-kick"
    ]
    admissions = [event for event in route_events if isinstance(event, BuildPlatformAdmissionEvent)]
    assert len(generated_intents) == 1
    assert len(admissions) == 1
    assert admissions[0].route == "legacy"
    assert admissions[0].run_intent_id == generated_intents[0].id
    await runtime._store.append(
        "legacy-pin",
        StatusEvent(status=ConversationStatus.PAUSED),
    )

    monkeypatch.setenv("DISCO_FREEFORM_PLATFORM_ROUTE", "1")
    restarted = ConversationRuntime(runtime._store)
    restarted.set_surface("legacy-pin", "agent")
    await restarted._build_platform.prepare_route_pin("legacy-pin")
    assert restarted._build_platform.route_pins["legacy-pin"] == "legacy"
    with mock.patch.object(restarted, "_sandbox_service_now"):
        restarted._compose_build_loop(
            "legacy-pin",
            mock.MagicMock(spec=DefaultLLMRouter),
            mock.MagicMock(spec=RouterAgent),
        )
    assert restarted._build_platform.selected_routes["legacy-pin"] == "legacy"
    assert restarted._build_platform.route_records == {}

    await restarted._store.append(
        "legacy-pin",
        StatusEvent(status=ConversationStatus.FINISHED),
    )
    await restarted._build_platform.prepare_route_pin("legacy-pin")
    assert "legacy-pin" not in restarted._build_platform.route_pins
    with mock.patch.object(restarted, "_sandbox_service_now"):
        restarted._compose_build_loop(
            "legacy-pin",
            mock.MagicMock(spec=DefaultLLMRouter),
            mock.MagicMock(spec=RouterAgent),
        )
    assert restarted._build_platform.selected_routes["legacy-pin"] == "platform"
    await restarted._build_platform.record_route_locked("legacy-pin")
    restarted_events = await restarted._store.get_events("legacy-pin")
    restarted_intents = [
        event
        for event in restarted_events
        if isinstance(event, WorkspaceMutationEvent)
        and event.operation == "agent.run-intent.runtime-kick"
    ]
    restarted_admissions = [
        event for event in restarted_events if isinstance(event, BuildPlatformAdmissionEvent)
    ]
    assert len(restarted_intents) == 2
    assert len(restarted_admissions) == 2
    assert restarted_admissions[-1].route == "platform"
    assert restarted_admissions[-1].run_intent_id == restarted_intents[-1].id
    assert restarted_admissions[-1].run_intent_id != restarted_admissions[0].run_intent_id
