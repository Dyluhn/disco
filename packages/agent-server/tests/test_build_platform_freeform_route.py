from __future__ import annotations

from unittest import mock

import pytest
from disco.agent_server.build_platform_shadow import (
    BuildPlatformRouteError,
    select_freeform_platform_route,
)
from disco.agent_server.runtime import ConversationRuntime
from disco.core import (
    BuildPlatformAdmissionEvent,
    ConversationStatus,
    SqliteEventStore,
    StatusEvent,
    WorkspaceMutationEvent,
    current_build_platform_admission,
)
from disco.core.build_platform import (
    APPKIT_PROFILE_ID,
    ARTIFACT_TARGET_ID,
    FREEFORM_ARTIFACT_PROFILE_ID,
)
from disco.core.llm import DefaultLLMRouter, OperatingMode, ToolSpec
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
    runtime.settings._set_surface(conversation_id, "agent")
    runtime.settings.set_appkit_mode(conversation_id, appkit)
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
    runtime._loop_registry.bind("pinned", loop)
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
    runtime.settings._set_surface("blocked", "agent")
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
    assert not runtime._run_resources.has_executor("blocked")
    assert runtime._loop_registry.loop("blocked") is None
    assert runtime._build_platform.route_records == {}


def test_artifact_and_workflow_profiles_remain_outside_freeform_cutover(monkeypatch) -> None:
    monkeypatch.setenv("DISCO_FREEFORM_PLATFORM_ROUTE", "1")
    runtime = ConversationRuntime(SqliteEventStore(":memory:"))
    runtime.settings._set_surface("artifact", "build")
    runtime.settings.set_artifact_mode("artifact", True)
    with mock.patch.object(runtime, "_sandbox_service_now"):
        loop = runtime._compose_build_loop(
            "artifact",
            mock.MagicMock(spec=DefaultLLMRouter),
            mock.MagicMock(spec=RouterAgent),
        )
    assert runtime._build_platform.route_records == {}
    assert isinstance(loop.executor, DefaultToolExecutor)
    assert loop.mode is OperatingMode.INTERACTIVE
    assert loop._delivery_contract_resolver is None


@pytest.mark.asyncio
async def test_platform_admission_is_durable_idempotent_and_restart_pinned(monkeypatch) -> None:
    runtime, loop = _compose(monkeypatch, "durable", platform=True)
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
    contract = admission.verification_contract
    assert contract is not None
    assert contract.target_id == "disco.legacy_web@1"
    assert contract.delivery.mode == "interactive"
    assert contract.delivery.shape == "web.legacy_deliverable"
    assert [(check.check_id, check.receipt_kind, check.issuer_id) for check in contract.checks] == [
        (
            "web_functional",
            "disco.web_functional@1",
            "disco.host_web_verifier@1",
        )
    ]
    assert contract.required_claims == admission.verification_claims

    monkeypatch.delenv("DISCO_FREEFORM_PLATFORM_ROUTE", raising=False)
    restarted = ConversationRuntime(runtime._store)
    restarted.settings._set_surface("durable", "agent")
    await restarted._build_platform.prepare_route_pin("durable")
    assert restarted._build_platform.route_pins["durable"] == "platform"
    with mock.patch.object(restarted, "_sandbox_service_now"):
        restarted._compose_build_loop(
            "durable",
            mock.MagicMock(spec=DefaultLLMRouter),
            mock.MagicMock(spec=RouterAgent),
        )
    assert restarted._build_platform.route_records["durable"].active_route == "platform"
    replayed = [
        event
        for event in await restarted._store.get_events("durable")
        if isinstance(event, BuildPlatformAdmissionEvent)
    ][0]
    assert replayed.verification_contract == contract

    drifted_record = select_freeform_platform_route(
        tool_specs=(
            *loop.executor.available_tools(),
            ToolSpec(name="drift_probe", description="drift", parameters_schema={}),
        ),
        delivery_kind="app",
    )
    restarted._build_platform._set_selection(
        "durable",
        route="platform",
        profile=drifted_record.composition.profile.id,
        record=drifted_record,
    )
    with pytest.raises(RuntimeError, match="durable Build admission differs"):
        await restarted._build_platform.record_route_locked("durable")
    assert not restarted._run_resources.has_executor("durable")
    assert "durable" not in restarted._build_platform.selected_routes
    assert "durable" not in restarted._build_platform.route_records

    artifact_record = select_freeform_platform_route(
        tool_specs=loop.executor.available_tools(),
        delivery_kind="files",
    )
    restarted._build_platform._set_selection(
        "durable",
        route="platform",
        profile=FREEFORM_ARTIFACT_PROFILE_ID,
        record=artifact_record,
    )
    with pytest.raises(RuntimeError, match="different Build admission"):
        await restarted._build_platform.record_route_locked("durable")


@pytest.mark.asyncio
async def test_delivery_selection_replaces_web_contract_with_artifact_target(monkeypatch) -> None:
    runtime, loop = _compose(monkeypatch, "script-delivery", platform=True)
    await runtime._store.append(
        "script-delivery",
        WorkspaceMutationEvent(
            operation="agent.run-intent.message",
            run_protocol_version=1,
        ),
    )
    await runtime._build_platform.record_route_locked("script-delivery")
    before = current_build_platform_admission(
        await runtime._store.get_events("script-delivery")
    )
    assert before is not None
    assert before.verification_contract is not None
    assert before.verification_contract.target_id == "disco.legacy_web@1"

    resolver = loop._delivery_contract_resolver
    assert resolver is not None
    async with runtime.workspace.fence("script-delivery"):
        await resolver("files")

    events = await runtime._store.get_events("script-delivery")
    selected = current_build_platform_admission(events)
    assert selected is not None
    assert selected.transition == "delivery_selection"
    assert selected.supersedes_admission_id == before.id
    assert selected.profile_id == FREEFORM_ARTIFACT_PROFILE_ID.canonical
    contract = selected.verification_contract
    assert contract is not None
    assert contract.target_id == ARTIFACT_TARGET_ID.canonical
    assert contract.delivery.mode == "artifact"
    assert contract.preview_modality == "none"
    assert contract.checks == ()
    assert contract.required is False
    assert contract.unverified_finish == "allow_without_verified_label"

    async with runtime.workspace.fence("script-delivery"):
        await resolver("files")
    admissions = [
        event
        for event in await runtime._store.get_events("script-delivery")
        if isinstance(event, BuildPlatformAdmissionEvent)
    ]
    assert len(admissions) == 2

    restarted = ConversationRuntime(runtime._store)
    restarted.settings._set_surface("script-delivery", "agent")
    await restarted._build_platform.prepare_route_pin("script-delivery")
    with mock.patch.object(restarted, "_sandbox_service_now"):
        restarted._compose_build_loop(
            "script-delivery",
            mock.MagicMock(spec=DefaultLLMRouter),
            mock.MagicMock(spec=RouterAgent),
        )
    record = restarted._build_platform.route_records["script-delivery"]
    assert record.composition.profile.id == FREEFORM_ARTIFACT_PROFILE_ID
    assert record.composition.target == ARTIFACT_TARGET_ID
    await restarted._build_platform.record_route_locked("script-delivery")


@pytest.mark.asyncio
async def test_delivery_selection_requires_current_task_process_fence(monkeypatch) -> None:
    runtime, loop = _compose(monkeypatch, "foreign-fence", platform=True)
    await runtime._store.append(
        "foreign-fence",
        WorkspaceMutationEvent(
            operation="agent.run-intent.message",
            run_protocol_version=1,
        ),
    )
    await runtime._build_platform.record_route_locked("foreign-fence")

    async with runtime.workspace.lock("foreign-fence"):
        with pytest.raises(RuntimeError, match="current-task fence ownership"):
            await runtime._build_platform.bind_delivery_locked(
                "foreign-fence",
                "files",
                tool_specs=loop.executor.available_tools(),
            )


@pytest.mark.asyncio
async def test_failed_delivery_reselection_discards_stale_runtime_selection(monkeypatch) -> None:
    runtime, loop = _compose(monkeypatch, "reselection-failure", platform=True)
    await runtime._store.append(
        "reselection-failure",
        WorkspaceMutationEvent(
            operation="agent.run-intent.message",
            run_protocol_version=1,
        ),
    )
    await runtime._build_platform.record_route_locked("reselection-failure")

    with (
        mock.patch(
            "disco.agent_server.build_platform_runtime.select_freeform_platform_route",
            side_effect=BuildPlatformRouteError("delivery parity mismatch"),
        ),
        pytest.raises(BuildPlatformRouteError, match="delivery parity mismatch"),
    ):
        async with runtime.workspace.fence("reselection-failure"):
            await runtime._build_platform.bind_delivery_locked(
                "reselection-failure",
                "files",
                tool_specs=loop.executor.available_tools(),
            )

    assert not runtime._run_resources.has_executor("reselection-failure")
    assert "reselection-failure" not in runtime._build_platform.selected_routes
    assert "reselection-failure" not in runtime._build_platform.route_records


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
    restarted.settings._set_surface("legacy-pin", "agent")
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
