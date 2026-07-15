"""AppKit EPIC F — phase-based tool-scope enforcement (L3) tests.

The enforcement is on the SECURITY allowlist (callable_tool_names / the executor's
registry resolution), NOT merely the advertised/visible set: a barred raw tool
called by its QUALIFIED NAME is REFUSED (unknown_tool), never silently executed.
"""

from __future__ import annotations

from disco.core import ActionEvent, SecurityRisk, ToolCall
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
    AppKitPhase,
    AppKitPhaseState,
    appkit_effective_scope,
)
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
    )

    async def run(self, args, ctx) -> ToolOutcome:  # pragma: no cover - not executed
        return ToolOutcome(success=True, content="ok")


_STANDARD = ModelExecutionPolicy.standard()
_RAW_TOOLS = ("file_write", "shell", "code_exec", "exact_replace", "browser")
# AGENT_TOOLS intersected with what is actually REGISTERED — callable_tool_names
# is registry ∩ allowed_tools, and a few AGENT_TOOLS names (deploy_preview) are
# deferred / unregistered, so the normal Build callable set is this intersection.
_REGISTERED_AGENT_TOOLS = AGENT_TOOLS & build_default_registry().names()


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
        sandbox=FakeSandboxInstance(),
    )
    return ex, state


async def _scaffold(ex: AppKitToolExecutor):
    return await ex.execute(call("app_create", recipe_id="editorial-ledger", brief="Acme"))


# ---- (c) planning phase (loop PLANNING) narrows the allowlist ----------------


def test_planning_phase_callable_names_are_reads_plus_plan_plus_hatch():
    ex, _ = _appkit_exec(loop_mode=OperatingMode.PLANNING)
    callable_names = ex.callable_tool_names()
    assert callable_names == APPKIT_READ_TOOLS | {"submit_plan", "request_custom_build"}
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
    assert state.phase == AppKitPhase.CUSTOM_BUILD
    # widened to the normal agent_scope: raw tools now callable.
    assert ex.callable_tool_names() == _REGISTERED_AGENT_TOOLS
    for raw in ("file_write", "shell", "code_exec"):
        assert raw in ex.callable_tool_names()
    # a raw call now actually resolves (not unknown_tool).
    res2 = await ex.execute(call("file_write", path="note.txt", content="hi"))
    assert not (res2.structured and res2.structured.get("kind") == "unknown_tool")


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
    assert applied == ["widened"]
    assert state.phase == AppKitPhase.CUSTOM_BUILD
    # after widening, the MCP name is callable.
    assert "mcp_remote_tool" in ex.callable_tool_names()


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
