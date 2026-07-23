"""AppKit EPIC F — phase-based tool-scope enforcement (L3) tests.

The enforcement is on the SECURITY allowlist (callable_tool_names / the executor's
registry resolution), NOT merely the advertised/visible set: a barred raw tool
called by its QUALIFIED NAME is REFUSED (unknown_tool), never silently executed.
"""

from __future__ import annotations

import asyncio
import re

import pytest
from disco.core import (
    APPKIT_EJECTION_LOST_GUARANTEES,
    APPKIT_EJECTION_SOURCE_TRIGGER,
    APPKIT_EJECTION_TARGET_TRIGGER,
    ActionEvent,
    AppKitEjectionEvent,
    ConversationStatus,
    Event,
    SecurityRisk,
    StatusEvent,
    ToolCall,
    WorkspaceMutationEvent,
    WorkspaceVersionEvent,
)
from disco.core.appkit import APPSPEC_RELPATH, DESIGNSPEC_RELPATH
from disco.core.llm import ModelExecutionPolicy, OperatingMode
from disco.core.loop import BlastRadiusConfirm
from disco.core.security import RuleBasedAnalyzer
from disco.tools import (
    AGENT_TOOLS,
    DefaultToolExecutor,
    ToolRegistry,
    agent_scope,
    build_default_registry,
    research_scope,
)
from disco.tools.anatomy import ToolDef, ToolOutcome
from disco.tools.appkit_exec import AppKitToolExecutor
from disco.tools.appkit_scope import (
    APPKIT_LIFECYCLE,
    APPKIT_MUTATORS,
    APPKIT_PROBES,
    APPKIT_READ_TOOLS,
    AppKitEjectionReceipt,
    AppKitPhase,
    AppKitPhaseState,
    appkit_effective_scope,
    fold_appkit_phase_evidence,
)
from disco.tools.behavior import OPAQUE_MCP_BEHAVIOR
from disco.tools.builtin.app_kit import APPKIT_V2_TOOLS
from disco.tools.builtin.app_kit import AppSnapshotVersionTool as V2AppSnapshotVersionTool
from disco.tools.builtin.design_lint import DesignLintTool
from disco.tools.builtin.request_custom_build import RequestCustomBuildTool
from disco.tools.builtin.verify_appkit_app import VerifyAppKitAppTool
from pydantic import BaseModel
from tool_fakes import FakeSandboxInstance, call


class _FakeMcpTool:
    """Stand-in for an MCP-wrapped tool registered at widen time."""

    definition = ToolDef(
        name="mcp_remote_tool",
        description="a remote MCP tool",
        args_model=BaseModel,
        runs_in="in_process",
        behavior=OPAQUE_MCP_BEHAVIOR,
    )

    async def run(self, args, ctx) -> ToolOutcome:  # pragma: no cover - not executed
        return ToolOutcome(success=True, content="ok")


_STANDARD = ModelExecutionPolicy.standard()
_RAW_TOOLS = ("file_write", "shell", "code_exec", "exact_replace", "browser")
# AGENT_TOOLS intersected with what is actually REGISTERED — callable_tool_names
# is registry ∩ allowed_tools, and a few AGENT_TOOLS names (deploy_preview) are
# deferred / unregistered, so the normal Build callable set is this intersection.
_REGISTERED_AGENT_TOOLS = AGENT_TOOLS & build_default_registry().names()
_SOURCE_DIGEST = "a" * 64
_TARGET_DIGEST = "b" * 64


async def _fake_ejection(_call: ToolCall) -> AppKitEjectionReceipt:
    return AppKitEjectionReceipt(
        source_version_seq=1,
        source_tree_digest=_SOURCE_DIGEST,
        ejected_version_seq=2,
        ejected_tree_digest=_TARGET_DIGEST,
        preview_version_seq=2,
    )


def _appkit_registry() -> ToolRegistry:
    registry = build_default_registry()
    for tool_cls in (
        *APPKIT_V2_TOOLS,
        DesignLintTool,
        RequestCustomBuildTool,
        VerifyAppKitAppTool,
    ):
        registry.register(tool_cls())
    return registry


def _appkit_exec(
    *,
    phase: AppKitPhase = AppKitPhase.PLANNING,
    loop_mode: OperatingMode | None = OperatingMode.LONG_HORIZON,
    autonomous: bool = False,
    on_widen=None,
    on_eject=_fake_ejection,
    on_preview_sync=None,
) -> tuple[AppKitToolExecutor, AppKitPhaseState]:
    state = AppKitPhaseState(phase=phase)
    base = agent_scope(model_policy=_STANDARD)
    ex = AppKitToolExecutor(
        _appkit_registry(),
        base,
        appkit_phase=state,
        base_scope=base,
        autonomous=autonomous,
        mode_getter=lambda: loop_mode,
        on_widen=on_widen,
        on_eject=on_eject,
        on_preview_sync=on_preview_sync,
        sandbox=FakeSandboxInstance(),
    )
    return ex, state


async def _scaffold(ex: AppKitToolExecutor, *, primitive_id: str = "lead_gen"):
    return await ex.execute(
        call(
            "app_create",
            recipe_id="editorial-ledger",
            primitive_id=primitive_id,
            brief="Acme",
        )
    )


@pytest.mark.asyncio
async def test_successful_semantic_mutation_syncs_host_preview_once() -> None:
    calls = 0

    async def sync_preview() -> None:
        nonlocal calls
        calls += 1

    ex, _state = _appkit_exec(on_preview_sync=sync_preview)
    result = await _scaffold(ex)

    assert result.success is True
    assert calls == 1


@pytest.mark.asyncio
async def test_refused_semantic_mutation_does_not_sync_host_preview() -> None:
    calls = 0

    async def sync_preview() -> None:
        nonlocal calls
        calls += 1

    ex, _state = _appkit_exec(on_preview_sync=sync_preview)
    result = await ex.execute(call("app_create", recipe_id="does-not-exist"))

    assert result.success is False
    assert calls == 0


def _custom_build_events(
    *,
    action_call_id: str = "custom-call",
    ejection_call_id: str | None = None,
    ejection_view_id: str = "view-1",
    source_marker_digest: str = _SOURCE_DIGEST,
    target_marker_trigger: str = APPKIT_EJECTION_TARGET_TRIGGER,
) -> list[Event]:
    intent = WorkspaceMutationEvent(
        operation="agent.run-intent.user-message",
        run_protocol_version=1,
    ).model_copy(update={"seq": 1})
    admission = WorkspaceMutationEvent(
        operation="agent.view-admitted",
        run_protocol_version=1,
        run_intent_id=intent.id,
        agent_view_id="view-1",
    ).model_copy(update={"seq": 2})
    action = ActionEvent(
        agent_view_id="view-1",
        thought="human approved custom build",
        tool_call=ToolCall(
            call_id=action_call_id,
            tool_name="request_custom_build",
            arguments={"reason": "needs raw tools", "needed_capabilities": ["shell"]},
        ),
    ).model_copy(update={"seq": 3})
    waiting = StatusEvent(
        status=ConversationStatus.WAITING_FOR_CONFIRMATION,
        detail=action.id,
    ).model_copy(update={"seq": 4})
    resumed = StatusEvent(
        status=ConversationStatus.RUNNING,
        agent_view_id="view-1",
    ).model_copy(update={"seq": 5})
    source = WorkspaceVersionEvent(
        version_seq=1,
        tree_digest=source_marker_digest,
        trigger=APPKIT_EJECTION_SOURCE_TRIGGER,
    ).model_copy(update={"seq": 6})
    mutation = WorkspaceMutationEvent(
        operation="agent.appkit-ejection",
        paths=(".disco/appkit_ejection.json",),
    ).model_copy(update={"seq": 7})
    target = WorkspaceVersionEvent(
        version_seq=2,
        tree_digest=_TARGET_DIGEST,
        trigger=target_marker_trigger,
    ).model_copy(update={"seq": 8})
    ejection = AppKitEjectionEvent(
        agent_view_id=ejection_view_id,
        action_id=action.id,
        tool_call_id=ejection_call_id or action_call_id,
        source_version_seq=1,
        source_tree_digest=_SOURCE_DIGEST,
        ejected_version_seq=2,
        ejected_tree_digest=_TARGET_DIGEST,
        lost_guarantees=APPKIT_EJECTION_LOST_GUARANTEES,
    ).model_copy(update={"seq": 9})
    return [intent, admission, action, waiting, resumed, source, mutation, target, ejection]


# ---- (c) planning phase (loop PLANNING) narrows the allowlist ----------------


def test_planning_phase_callable_names_are_reads_plus_plan_without_hatch():
    ex, _ = _appkit_exec(loop_mode=OperatingMode.PLANNING)
    callable_names = ex.callable_tool_names()
    assert callable_names == APPKIT_READ_TOOLS | {"submit_plan"}
    # No mutators, no raw tools advertised as callable in planning.
    assert not (callable_names & APPKIT_MUTATORS)
    for raw in _RAW_TOOLS:
        assert raw not in callable_names


# ---- (a) build phase REFUSES raw tools by qualified name → unknown_tool ------


async def test_build_phase_refuses_raw_tools_by_qualified_name():
    ex, _ = _appkit_exec(phase=AppKitPhase.BUILD)
    for raw in ("file_write", "shell", "code_exec"):
        # called by its real, registered qualified name — must be refused on the
        # ALLOWLIST (not just hidden), never executed.
        res = await ex.execute(call(raw, path="x", content="y", command="ls", code="print(1)"))
        assert res.success is False, raw
        assert res.structured is not None and res.structured["kind"] == "unknown_tool", raw
        assert raw not in ex.callable_tool_names()


@pytest.mark.parametrize(
    ("phase", "loop_mode", "expected_recovery"),
    [
        (AppKitPhase.PLANNING, OperatingMode.PLANNING, {"submit_plan"}),
        (AppKitPhase.PLANNING, OperatingMode.LONG_HORIZON, {"app_create"}),
        (
            AppKitPhase.BUILD,
            OperatingMode.LONG_HORIZON,
            {
                "app_update_content",
                "app_add_section",
                "app_set_design",
                "app_add_primitive",
                "design_lint",
                "verify_appkit_app",
                "app_snapshot_version",
            },
        ),
    ],
    ids=("planning", "bootstrap", "build"),
)
async def test_registered_scope_denial_is_phase_aware_and_only_directs_callable_tools(
    phase: AppKitPhase,
    loop_mode: OperatingMode,
    expected_recovery: set[str],
):
    ex, _ = _appkit_exec(phase=phase, loop_mode=loop_mode, autonomous=True)
    res = await ex.execute(call("file_write", path="x", content="y"))
    assert res.success is False
    assert res.structured is not None and res.structured["kind"] == "unknown_tool"
    assert "strict AppKit scope denied registered tool 'file_write'" in res.error
    assert "it was not executed" in res.error
    assert "do not retry" in res.error
    recovery = res.error.partition("Recovery: ")[2]
    directed_tools = set(re.findall(r"`([^`]+)`", recovery))
    assert directed_tools == expected_recovery
    assert directed_tools <= ex.callable_tool_names()


@pytest.mark.parametrize(
    ("primitive_id", "expect_add_primitive"),
    [("local_list", False), ("lead_gen", True)],
)
async def test_build_denial_recovery_respects_created_base_primitive(
    primitive_id: str, expect_add_primitive: bool
):
    ex, state = _appkit_exec(
        phase=AppKitPhase.PLANNING,
        loop_mode=OperatingMode.LONG_HORIZON,
        autonomous=True,
    )
    created = await _scaffold(ex, primitive_id=primitive_id)
    assert created.success, created.error
    assert state.phase == AppKitPhase.BUILD

    denied = await ex.execute(call("file_write", path="x", content="y"))
    assert not denied.success
    recovery = denied.error.partition("Recovery: ")[2]
    directed_tools = set(re.findall(r"`([^`]+)`", recovery))
    assert ("app_add_primitive" in directed_tools) is expect_add_primitive
    assert directed_tools <= ex.callable_tool_names()


def test_strict_appkit_requery_distinguishes_scope_denied_from_truly_unknown():
    """Registered-but-barred tools must reach the canonical executor denial.

    The driver may silently requery names that do not exist at all.  It must not
    requery a real registry tool merely because strict AppKit withholds it: doing
    so hides the actionable out-of-scope observation and lets repeated hidden
    repairs trip the live-thrash breaker before the model can select app_create.
    """

    ex, _ = _appkit_exec(phase=AppKitPhase.PLANNING)
    assert "file_write" not in ex.callable_tool_names()
    assert "file_write" in ex.known_tool_names_for_requery()
    assert "write_file" not in ex.known_tool_names_for_requery()


# ---- (b) the AppKit mutators + probes ARE callable in build phase ------------


def test_build_phase_allows_appkit_mutators_and_probes():
    ex, _ = _appkit_exec(phase=AppKitPhase.BUILD)
    callable_names = ex.callable_tool_names()
    for name in (
        "app_create",
        "app_add_section",
        "app_update_content",
        "app_set_design",
        "design_lint",
        "verify_web_app",
    ):
        assert name in callable_names, name
    assert APPKIT_MUTATORS <= callable_names
    assert APPKIT_PROBES <= callable_names


# ---- (b2) EPIC H3: app_snapshot_version is BUILD-phase-only -------------------


def test_app_snapshot_version_build_phase_only_and_not_on_normal_build():
    # callable in the build phase
    build_ex, _ = _appkit_exec(phase=AppKitPhase.BUILD)
    build_names = build_ex.callable_tool_names()
    assert APPKIT_LIFECYCLE <= build_names
    assert "app_snapshot_version" in build_names
    # NOT callable in the planning phase (an app must already exist)
    plan_ex, _ = _appkit_exec(phase=AppKitPhase.PLANNING)
    assert "app_snapshot_version" not in plan_ex.callable_tool_names()
    # disclaude's normal Build still carries the legacy v1 app_snapshot_version
    # tool; B3 only keeps the V2 AppKit snapshot implementation out of normal Build.
    normal_tool = build_default_registry().get(
        "app_snapshot_version", scope=agent_scope(model_policy=_STANDARD)
    )
    assert normal_tool is not None
    assert not isinstance(normal_tool, V2AppSnapshotVersionTool)


# ---- (d) a SUCCESSFUL app_create transitions planning → build ----------------


async def test_successful_app_create_transitions_planning_to_build():
    ex, state = _appkit_exec(phase=AppKitPhase.PLANNING, loop_mode=OperatingMode.LONG_HORIZON)
    # bootstrap: app_create callable in execution-mode planning; the rest of the
    # mutators are not yet.
    assert "app_create" in ex.callable_tool_names()
    assert "app_add_section" not in ex.callable_tool_names()

    res = await _scaffold(ex)
    assert res.success is True, res.error
    assert state.phase == AppKitPhase.BUILD
    # build unlocked the full mutator set.
    assert "app_add_section" in ex.callable_tool_names()
    assert APPKIT_MUTATORS <= ex.callable_tool_names()
    # raw tools STILL barred after the transition.
    assert "file_write" not in ex.callable_tool_names()


async def test_failed_app_create_does_not_transition():
    ex, state = _appkit_exec(phase=AppKitPhase.PLANNING)
    res = await ex.execute(call("app_create", recipe_id="does-not-exist"))
    assert res.success is False
    assert state.phase == AppKitPhase.PLANNING  # no advance on failure


# ---- (e) request_custom_build is HIGH-risk + gated, widens on confirm --------


def test_request_custom_build_is_high_risk_and_gated():
    analyzer = RuleBasedAnalyzer({"request_custom_build": SecurityRisk.HIGH})
    action = ActionEvent(
        thought="",
        tool_call=ToolCall(
            tool_name="request_custom_build",
            arguments={"reason": "need shell", "needed_capabilities": ["shell"]},
        ),
    )
    assert analyzer.assess(action) == SecurityRisk.HIGH
    # BlastRadiusConfirm gates an in_process HIGH-risk call (it is not sandboxed).
    gate = BlastRadiusConfirm()
    assert (
        gate.should_confirm_action(
            SecurityRisk.HIGH, scope="in_process", tool_name="request_custom_build"
        )
        is True
    )


async def test_confirmed_request_custom_build_widens_to_agent_scope():
    ex, state = _appkit_exec(phase=AppKitPhase.BUILD)
    # before: raw tools barred.
    assert "file_write" not in ex.callable_tool_names()
    # reaching execute() == post-confirm (the gate ran in the loop).
    res = await ex.execute(
        call("request_custom_build", reason="need raw shell", needed_capabilities=["shell"])
    )
    assert res.success is True
    assert res.structured is not None and "ejection" in res.structured
    # Callback success alone is not the durable authority. The exact typed
    # revision markers and ejection event widen on reconciliation.
    assert state.phase == AppKitPhase.BUILD
    await ex.prepare_for_events(_custom_build_events())
    assert state.phase == AppKitPhase.CUSTOM_BUILD
    # widened to the normal agent_scope: raw tools now callable.
    assert ex.callable_tool_names() == _REGISTERED_AGENT_TOOLS
    for raw in ("file_write", "shell", "code_exec"):
        assert raw in ex.callable_tool_names()
    # a raw call now actually resolves (not unknown_tool).
    res2 = await ex.execute(call("file_write", path="note.txt", content="hi"))
    assert not (res2.structured and res2.structured.get("kind") == "unknown_tool")


async def test_request_custom_build_without_host_revision_service_fails_closed():
    ex, state = _appkit_exec(phase=AppKitPhase.BUILD, on_eject=None)

    result = await ex.execute(
        call("request_custom_build", reason="need raw shell", needed_capabilities=["shell"])
    )

    assert result.success is False
    assert "host revision service is unavailable" in result.error
    assert state.phase is AppKitPhase.BUILD
    assert "file_write" not in ex.callable_tool_names()


# ---- (f) request_custom_build unavailable / refused in autonomous mode -------


async def test_request_custom_build_unavailable_in_autonomous():
    ex, state = _appkit_exec(phase=AppKitPhase.BUILD, autonomous=True)
    assert "request_custom_build" not in ex.callable_tool_names()
    res = await ex.execute(call("request_custom_build", reason="x", needed_capabilities=[]))
    assert res.success is False
    assert res.structured is not None and res.structured["kind"] == "unknown_tool"
    assert state.phase == AppKitPhase.BUILD  # no auto-approve widening


def test_planning_autonomous_drops_hatch():
    ex, _ = _appkit_exec(loop_mode=OperatingMode.PLANNING, autonomous=True)
    assert "request_custom_build" not in ex.callable_tool_names()
    assert ex.callable_tool_names() == APPKIT_READ_TOOLS | {"submit_plan"}


# ---- (g) P0: MCP names are NOT in the allowlist in strict mode ---------------


async def test_mcp_delta_not_applied_in_strict_mode_until_widen():
    applied: list[str] = []

    def on_widen() -> None:
        # mimic _apply_mcp_scope: register the MCP wrapper AND union its name into the
        # (widened) scope — exactly the two steps the real runtime defers to widen time.
        applied.append("widened")
        ex._registry.register(_FakeMcpTool())
        ex._scope = ex._scope.model_copy(
            update={"allowed_tools": ex._scope.allowed_tools | {"mcp_remote_tool"}}
        )

    ex, state = _appkit_exec(phase=AppKitPhase.BUILD, on_widen=on_widen)
    # strict mode: an MCP qualified name must NOT bypass the phase scope.
    assert "mcp_remote_tool" not in ex.callable_tool_names()
    # widen via the gated escape hatch.
    res = await ex.execute(call("request_custom_build", reason="mcp", needed_capabilities=[]))
    assert res.success is True
    assert applied == []
    await ex.prepare_for_events(_custom_build_events())
    assert applied == ["widened"]
    assert state.phase == AppKitPhase.CUSTOM_BUILD
    # after widening, the MCP name is callable.
    assert "mcp_remote_tool" in ex.callable_tool_names()
    await ex.prepare_for_events(_custom_build_events())
    assert applied == ["widened"]
    assert "mcp_remote_tool" in ex.callable_tool_names()


def test_custom_build_fold_rejects_mispairs_and_stale_view_results():
    assert fold_appkit_phase_evidence(_custom_build_events(ejection_call_id="wrong")) is None
    assert fold_appkit_phase_evidence(_custom_build_events(ejection_view_id="other-view")) is None
    assert fold_appkit_phase_evidence(_custom_build_events(source_marker_digest="c" * 64)) is None
    assert (
        fold_appkit_phase_evidence(_custom_build_events(target_marker_trigger="ordinary-turn"))
        is None
    )

    stale = _custom_build_events()
    newer_intent = WorkspaceMutationEvent(
        operation="agent.run-intent.user-message",
        run_protocol_version=1,
    ).model_copy(update={"seq": 9})
    stale_ejection = stale[-1].model_copy(update={"seq": 10})
    assert fold_appkit_phase_evidence([*stale[:-1], newer_intent, stale_ejection]) is None

    # Matching non-null strings are not authority without a typed admission.
    orphan_pair = _custom_build_events()[2:]
    orphan_pair = [
        event.model_copy(update={"seq": index + 1}) for index, event in enumerate(orphan_pair)
    ]
    assert fold_appkit_phase_evidence(orphan_pair) is None

    # Even accidental/replayed reuse of a view id cannot bridge a newer intent.
    reused_admission = WorkspaceMutationEvent(
        operation="agent.view-admitted",
        run_protocol_version=1,
        run_intent_id=newer_intent.id,
        agent_view_id="view-1",
    ).model_copy(update={"seq": 10})
    reused_stale_ejection = stale[-1].model_copy(update={"seq": 11})
    assert (
        fold_appkit_phase_evidence(
            [*stale[:-1], newer_intent, reused_admission, reused_stale_ejection]
        )
        is None
    )


def test_custom_build_fold_requires_typed_ejection_and_is_sticky_afterward():
    crash_window = _custom_build_events()[:-1]
    assert fold_appkit_phase_evidence(crash_window) is None

    widened = _custom_build_events()
    newer_intent = WorkspaceMutationEvent(
        operation="agent.run-intent.user-message",
        run_protocol_version=1,
    ).model_copy(update={"seq": 10})
    assert fold_appkit_phase_evidence([*widened, newer_intent]) is AppKitPhase.CUSTOM_BUILD


async def test_current_specs_recover_build_after_eviction_and_rollback_downgrades():
    first, first_state = _appkit_exec(phase=AppKitPhase.PLANNING)
    scaffold = await _scaffold(first, primitive_id="local_list")
    assert scaffold.success and first_state.phase is AppKitPhase.BUILD
    sandbox = first.sandbox
    assert sandbox is not None

    # A fresh executor (loop eviction/restart) has no trusted in-memory phase.
    restarted_state = AppKitPhaseState()
    base = agent_scope(model_policy=_STANDARD)
    restarted = AppKitToolExecutor(
        _appkit_registry(),
        base,
        appkit_phase=restarted_state,
        base_scope=base,
        autonomous=False,
        mode_getter=lambda: OperatingMode.LONG_HORIZON,
        sandbox=sandbox,
    )
    await restarted.prepare_for_events([])
    assert restarted_state.phase is AppKitPhase.BUILD
    assert restarted._appkit_base_primitive == "local_list"
    assert "app_add_primitive" not in restarted._strict_scope_recovery(
        sorted(restarted.callable_tool_names())
    )

    await sandbox.delete_file(DESIGNSPEC_RELPATH)
    rollback = WorkspaceMutationEvent(operation="workspace.restore").model_copy(update={"seq": 1})
    await restarted.prepare_for_events([rollback])
    assert restarted_state.phase is AppKitPhase.PLANNING
    assert "app_create" in restarted.callable_tool_names()
    assert "app_update_content" not in restarted.callable_tool_names()


async def test_spec_validation_cache_retries_infra_without_downgrading_proven_build():
    first, _ = _appkit_exec()
    scaffold = await _scaffold(first)
    assert scaffold.success
    sandbox = first.sandbox
    assert sandbox is not None

    state = AppKitPhaseState()
    base = agent_scope(model_policy=_STANDARD)
    restarted = AppKitToolExecutor(
        _appkit_registry(),
        base,
        appkit_phase=state,
        base_scope=base,
        autonomous=False,
        mode_getter=lambda: OperatingMode.LONG_HORIZON,
        sandbox=sandbox,
    )
    reads = 0
    original_read = sandbox.read_file

    async def counted_read(path: str) -> bytes:
        nonlocal reads
        reads += 1
        return await original_read(path)

    sandbox.read_file = counted_read  # type: ignore[method-assign]
    await restarted.prepare_for_events([])
    await restarted.prepare_for_events([])
    assert state.phase is AppKitPhase.BUILD
    assert reads == 2

    mutation = WorkspaceMutationEvent(operation="workspace.editor-write").model_copy(
        update={"seq": 1}
    )

    async def infrastructure_error(path: str) -> bytes:
        raise RuntimeError(f"temporary read failure for {path}")

    sandbox.read_file = infrastructure_error  # type: ignore[method-assign]
    await restarted.prepare_for_events([mutation])
    assert state.phase is AppKitPhase.BUILD

    # The failed generation was not cached: the same event generation is retried.
    sandbox.read_file = counted_read  # type: ignore[method-assign]
    await restarted.prepare_for_events([mutation])
    assert state.phase is AppKitPhase.BUILD
    assert reads == 4


async def test_fresh_executor_fails_closed_on_spec_read_infrastructure_error():
    sandbox = FakeSandboxInstance()

    async def infrastructure_error(path: str) -> bytes:
        raise RuntimeError(f"temporary read failure for {path}")

    sandbox.read_file = infrastructure_error  # type: ignore[method-assign]
    state = AppKitPhaseState()
    base = agent_scope(model_policy=_STANDARD)
    ex = AppKitToolExecutor(
        _appkit_registry(),
        base,
        appkit_phase=state,
        base_scope=base,
        autonomous=False,
        mode_getter=lambda: OperatingMode.LONG_HORIZON,
        sandbox=sandbox,
    )
    await ex.prepare_for_events([])
    assert state.phase is AppKitPhase.PLANNING
    assert "app_update_content" not in ex.callable_tool_names()


async def test_effect_fence_reprepares_after_wait_and_blocks_stale_custom_scope():
    lock = asyncio.Lock()
    state = AppKitPhaseState()
    base = agent_scope(model_policy=_STANDARD)
    sandbox = FakeSandboxInstance()
    ex = AppKitToolExecutor(
        _appkit_registry(),
        base,
        appkit_phase=state,
        base_scope=base,
        autonomous=False,
        mode_getter=lambda: OperatingMode.LONG_HORIZON,
        sandbox=sandbox,
        workspace_lock=lock,
    )
    scaffold = await _scaffold(ex)
    assert scaffold.success and state.phase is AppKitPhase.BUILD

    # Simulate stale process-local widening with no durable custom-build pair.
    state.phase = AppKitPhase.CUSTOM_BUILD
    assert "file_write" in ex.callable_tool_names()
    current_events: list[Event] = []
    await lock.acquire()
    pending = asyncio.create_task(
        ex.execute_attributed(
            call("file_write", path="stale.txt", content="must not land"),
            None,
            prepare=lambda: ex.prepare_for_events(current_events),
        )
    )
    await asyncio.sleep(0)
    await sandbox.delete_file(APPSPEC_RELPATH)
    current_events.append(
        WorkspaceMutationEvent(operation="workspace.restore").model_copy(update={"seq": 1})
    )
    lock.release()

    result = await pending
    assert not result.success
    assert result.structured is not None and result.structured["kind"] == "unknown_tool"
    assert state.phase is AppKitPhase.PLANNING
    assert "stale.txt" not in sandbox._fs


# ---- (h) REGRESSION: normal Build + Research scopes unchanged ----------------


def test_regression_normal_build_scope_unchanged():
    ex = DefaultToolExecutor(
        build_default_registry(),
        agent_scope(model_policy=_STANDARD),
        sandbox=FakeSandboxInstance(),
    )
    assert ex.callable_tool_names() == _REGISTERED_AGENT_TOOLS
    # request_custom_build is NOT part of the normal Build action space.
    assert "request_custom_build" not in ex.callable_tool_names()
    for raw in ("file_write", "shell", "code_exec"):
        assert raw in ex.callable_tool_names()


def test_regression_research_scope_unchanged():
    ex = DefaultToolExecutor(
        build_default_registry(), research_scope(), sandbox=FakeSandboxInstance()
    )
    names = ex.callable_tool_names()
    assert "request_custom_build" not in names
    assert "app_create" not in names
    assert "file_write" not in names  # research never had it


# ---- scope-function unit coverage -------------------------------------------


def test_appkit_effective_scope_custom_build_returns_base():
    base = agent_scope(model_policy=_STANDARD)
    scope = appkit_effective_scope(
        loop_mode=OperatingMode.LONG_HORIZON,
        phase=AppKitPhase.CUSTOM_BUILD,
        base_scope=base,
        autonomous=False,
    )
    assert scope is base
