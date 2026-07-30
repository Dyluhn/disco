"""AppKit runtime wiring of phase-based tool-scope enforcement."""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from disco.agent_server.runtime import (
    APPKIT_LIVE_PREVIEW_NAME,
    ConversationRuntime,
    _sync_appkit_live_preview,
)
from disco.core import (
    APPKIT_EJECTION_LOST_GUARANTEES,
    APPKIT_EJECTION_SOURCE_TRIGGER,
    APPKIT_EJECTION_TARGET_TRIGGER,
    ActionEvent,
    AppKitEjectionEvent,
    ConversationStatus,
    SecurityRisk,
    SqliteEventStore,
    StatusEvent,
    ToolCall,
    WorkspaceMutationEvent,
    WorkspaceVersionEvent,
)
from disco.core.llm import DefaultLLMRouter, ModelRole, OperatingMode
from disco.core.loop import BlastRadiusConfirm, RouterAgent
from disco.core.loop.driver import Driver
from disco.core.security import RuleBasedAnalyzer
from disco.tools import (
    AGENT_TOOLS,
    AppKitPhase,
    AppKitToolExecutor,
    DefaultToolExecutor,
    ToolDef,
)
from disco.tools.appkit_scope import (
    AppKitEjectionReceipt,
)
from disco.tools.behavior import OPAQUE_MCP_BEHAVIOR
from pydantic import BaseModel

_EXECUTION_READS = {"file_list", "file_read", "search", "extract", "think"}


class _McpArgs(BaseModel):
    pass


def _rt() -> ConversationRuntime:
    return ConversationRuntime(SqliteEventStore(":memory:"))


def _loop_for(rt: ConversationRuntime, cid: str, *, appkit_mode: bool = False):
    rt.set_surface(cid, "agent")
    if appkit_mode:
        rt.set_appkit_mode(cid, True)
    router = mock.MagicMock(spec=DefaultLLMRouter)
    agent = mock.MagicMock(spec=RouterAgent)
    with mock.patch.object(rt, "_sandbox_service_now"):
        loop = rt._compose_build_loop(cid, router, agent)
    rt._loop_registry.bind(cid, loop)
    return loop


def test_appkit_mode_builds_appkit_executor() -> None:
    rt = _rt()
    loop = _loop_for(rt, "ak1", appkit_mode=True)
    assert isinstance(loop.executor, AppKitToolExecutor)
    assert loop.executor._appkit_on_preview_sync is not None
    assert loop.executor._sandbox._auto_preview_disabled is True


async def test_appkit_preview_keeps_dependency_failure_when_stale_vite_starts() -> None:
    preview = SimpleNamespace(status=SimpleNamespace(value="running"), update_error=None)

    class _Manager:
        @staticmethod
        def canonical_lifecycle_session():
            return None

        @staticmethod
        async def start(**kwargs):  # noqa: ANN003
            assert kwargs == {
                "framework": "vite",
                "name": APPKIT_LIVE_PREVIEW_NAME,
                "supervise": True,
            }
            return preview

    class _Session:
        _preview_manager = _Manager()

        @staticmethod
        async def read_file(path: str) -> bytes:
            if path == "package.json":
                return b'{"dependencies":{"new-package":"1.0.0"}}'
            if path == ".disco/appkit-vite-package.sha256":
                return b"stale-package-digest\n"
            raise AssertionError(path)

        @staticmethod
        async def list_dir(path: str):
            assert path == "node_modules"
            return []

        @staticmethod
        async def file_exists(path: str) -> bool:
            assert path == "package-lock.json"
            return True

        @staticmethod
        async def exec_shell(command: str, *, timeout_s: float):
            assert command == "npm ci --no-audit --no-fund"
            assert timeout_s == 300
            return SimpleNamespace(
                exit_code=1,
                timed_out=False,
                stderr="dependency refresh failed",
                stdout="",
            )

    await _sync_appkit_live_preview(_Session())  # type: ignore[arg-type]

    assert preview.update_error == (
        "AppKit preview dependency setup failed: dependency refresh failed"
    )


def test_appkit_done_condition_profile_tracks_custom_build_widening() -> None:
    rt = _rt()
    loop = _loop_for(rt, "ak-dod-profile", appkit_mode=True)
    reader = loop._strict_appkit_active_reader
    assert reader is not None
    assert reader() is True

    assert isinstance(loop.executor, AppKitToolExecutor)
    loop.executor.appkit_phase.phase = AppKitPhase.CUSTOM_BUILD
    assert reader() is False


def test_loop_for_selects_strict_appkit_prompt_profile() -> None:
    rt = _rt()
    cid = "ak-prompt"
    rt.set_surface(cid, "agent")
    rt.set_appkit_mode(cid, True)
    with mock.patch.object(rt._drivers, "router", wraps=rt._drivers.router) as router_now:
        with mock.patch.object(rt, "_sandbox_service_now"):
            rt._loop_for(cid)
    assert any(call.kwargs.get("appkit_mode") is True for call in router_now.call_args_list)
    loop = rt._loop_registry.loop(cid)
    assert loop is not None
    prompt = loop._router._prompts.system_prompt(
        model_family="deepseek",
        mode=OperatingMode.LONG_HORIZON,
        role=ModelRole.AGENT_DRIVER,
    )
    assert "STRICT APPKIT" in prompt
    assert "`file_write`" not in prompt


def test_appkit_off_builds_default_executor() -> None:
    rt = _rt()
    loop = _loop_for(rt, "bld1", appkit_mode=False)
    assert isinstance(loop.executor, DefaultToolExecutor)
    assert not isinstance(loop.executor, AppKitToolExecutor)
    assert loop.executor._scope.allowed_tools == AGENT_TOOLS
    assert "request_custom_build" not in loop.executor.callable_tool_names()


def test_appkit_loop_starts_in_planning_with_narrow_allowlist() -> None:
    rt = _rt()
    loop = _loop_for(rt, "ak2", appkit_mode=True)
    assert loop.mode == OperatingMode.PLANNING
    callable_names = loop.executor.callable_tool_names()
    assert "submit_plan" in callable_names
    assert "request_custom_build" not in callable_names
    for raw in ("file_write", "shell", "code_exec"):
        assert raw not in callable_names
    assert "app_add_section" not in callable_names


def test_appkit_keeps_blast_radius_gate_and_high_risk_hatch() -> None:
    rt = _rt()
    loop = _loop_for(rt, "ak3", appkit_mode=True)
    assert isinstance(loop.policy, BlastRadiusConfirm)
    assert isinstance(loop.analyzer, RuleBasedAnalyzer)
    action = ActionEvent(
        thought="",
        tool_call=ToolCall(
            tool_name="request_custom_build",
            arguments={"reason": "need shell", "needed_capabilities": ["shell"]},
        ),
    )
    assert loop.analyzer.assess(action) == SecurityRisk.HIGH


def test_appkit_autonomous_drops_escape_hatch() -> None:
    rt = _rt()
    rt.set_autonomous("ak4", True)
    loop = _loop_for(rt, "ak4", appkit_mode=True)
    assert "request_custom_build" not in loop.executor.callable_tool_names()


def test_appkit_execution_inspect_allowed_tools_remain_callable_only() -> None:
    """Requery recognition must not widen DISCO_INSPECT capability evidence."""

    rt = _rt()
    loop = _loop_for(rt, "ak-inspect", appkit_mode=True)
    loop.mode = OperatingMode.LONG_HORIZON
    driver = Driver(loop)
    offered = driver.tools_for_step(mode=OperatingMode.LONG_HORIZON)
    allowed = driver.allowed_tool_names_for_mode(
        OperatingMode.LONG_HORIZON, available_tools=offered
    )

    assert "file_write" in driver.known_tool_names_for_requery()
    assert "file_write" not in loop.executor.callable_tool_names()
    assert "file_write" not in allowed
    assert "app_create" in allowed
    assert {tool.name for tool in offered} <= allowed


def test_ordinary_build_execution_retains_prompt_directed_reads_but_not_submit_plan() -> None:
    rt = _rt()
    loop = _loop_for(rt, "ordinary-execution", appkit_mode=False)
    loop.mode = OperatingMode.LONG_HORIZON
    driver = Driver(loop)
    offered = driver.tools_for_step(mode=loop.mode)
    offered_names = {tool.name for tool in offered}
    allowed = driver.allowed_tool_names_for_mode(loop.mode, available_tools=offered)

    assert _EXECUTION_READS <= offered_names
    assert _EXECUTION_READS <= allowed
    assert "submit_plan" not in offered_names
    assert "submit_plan" not in allowed
    assert "questions_v2" not in allowed


async def test_appkit_prompt_offered_and_allowed_agree_through_widening() -> None:
    rt = _rt()
    cid = "ak-cross-layer"
    rt.set_surface(cid, "agent")
    rt.set_appkit_mode(cid, True)
    with mock.patch.object(rt, "_sandbox_service_now"):
        loop = rt._loop_for(cid)
    driver = Driver(loop)

    def snapshot() -> tuple[set[str], set[str], str]:
        offered = driver.tools_for_step(mode=loop.mode)
        offered_names = {tool.name for tool in offered}
        allowed = driver.allowed_tool_names_for_mode(loop.mode, available_tools=offered)
        prompt = loop._router._prompts.system_prompt(
            model_family="deepseek",
            mode=loop.mode,
            role=ModelRole.AGENT_DRIVER,
        )
        return offered_names, allowed, prompt

    # Planning: submit is real; the high-risk hatch is absent at executor,
    # offer, allow, and prompt layers.
    planning_offered, planning_allowed, planning_prompt = snapshot()
    assert "submit_plan" in planning_offered <= planning_allowed
    assert "submit_plan" in planning_prompt
    assert "request_custom_build" not in loop.executor.callable_tool_names()
    assert "request_custom_build" not in planning_offered
    assert "request_custom_build" not in planning_allowed
    assert "request_custom_build" not in planning_prompt

    # Execution bootstrap: app_create + the confirmed hatch are now offered,
    # allowed, and directed; prompt-directed reads/think remain available.
    loop.mode = OperatingMode.LONG_HORIZON
    bootstrap_offered, bootstrap_allowed, bootstrap_prompt = snapshot()
    assert {"app_create", "request_custom_build"} <= bootstrap_offered <= bootstrap_allowed
    assert "app_create" in bootstrap_prompt and "request_custom_build" in bootstrap_prompt
    assert _EXECUTION_READS <= bootstrap_offered <= bootstrap_allowed
    assert "submit_plan" not in bootstrap_offered
    assert "submit_plan" not in bootstrap_allowed
    assert "questions_v2" not in bootstrap_allowed

    # Build: strict semantic operations replace bootstrap-only creation while
    # the human-confirmed escape remains exactly aligned.
    assert isinstance(loop.executor, AppKitToolExecutor)
    loop.executor.appkit_phase.phase = AppKitPhase.BUILD
    build_offered, build_allowed, build_prompt = snapshot()
    assert {"app_update_content", "verify_appkit_app", "request_custom_build"} <= build_offered
    assert build_offered <= build_allowed
    assert "app_update_content" in build_prompt and "request_custom_build" in build_prompt
    assert _EXECUTION_READS <= build_offered <= build_allowed
    assert "submit_plan" not in build_allowed

    # Post-confirmation: executor scope and the active router profile widen
    # together. Strict semantic-only wording disappears as raw Build tools arrive.
    custom_call = ToolCall(
        tool_name="request_custom_build",
        arguments={"reason": "need raw files", "needed_capabilities": ["file_write"]},
    )

    async def fake_ejection(_call: ToolCall) -> AppKitEjectionReceipt:
        return AppKitEjectionReceipt(
            source_version_seq=1,
            source_tree_digest="a" * 64,
            ejected_version_seq=2,
            ejected_tree_digest="b" * 64,
            preview_version_seq=2,
        )

    loop.executor.set_ejection_callback(fake_ejection)
    widened = await loop.executor.execute(custom_call)
    assert widened.success
    assert "file_write" not in loop.executor.callable_tool_names()
    intent = WorkspaceMutationEvent(
        operation="agent.run-intent.user-message", run_protocol_version=1
    ).model_copy(update={"seq": 1})
    admission = WorkspaceMutationEvent(
        operation="agent.view-admitted",
        run_protocol_version=1,
        run_intent_id=intent.id,
        agent_view_id="view-1",
    ).model_copy(update={"seq": 2})
    action = ActionEvent(
        thought="approved widening",
        tool_call=custom_call,
        agent_view_id="view-1",
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
        tree_digest="a" * 64,
        trigger=APPKIT_EJECTION_SOURCE_TRIGGER,
    ).model_copy(update={"seq": 6})
    target = WorkspaceVersionEvent(
        version_seq=2,
        tree_digest="b" * 64,
        trigger=APPKIT_EJECTION_TARGET_TRIGGER,
    ).model_copy(update={"seq": 7})
    ejection = AppKitEjectionEvent(
        agent_view_id="view-1",
        action_id=action.id,
        tool_call_id=custom_call.call_id,
        source_version_seq=1,
        source_tree_digest="a" * 64,
        ejected_version_seq=2,
        ejected_tree_digest="b" * 64,
        lost_guarantees=APPKIT_EJECTION_LOST_GUARANTEES,
    ).model_copy(update={"seq": 8})
    await loop.executor.prepare_for_events(
        [intent, admission, action, waiting, resumed, source, target, ejection]
    )
    widened_offered, widened_allowed, widened_prompt = snapshot()
    assert "file_write" in loop.executor.callable_tool_names()
    assert "file_write" in widened_offered <= widened_allowed
    assert "file_write" in widened_prompt
    assert "STRICT APPKIT" not in widened_prompt
    assert "semantic tools actually offered" not in widened_prompt
    assert "request_custom_build" not in widened_offered
    assert "submit_plan" not in widened_offered
    assert "submit_plan" not in widened_allowed
    assert "questions_v2" not in widened_allowed


def test_appkit_mcp_delta_deferred_not_in_strict_allowlist() -> None:
    rt = _rt()
    rt.set_appkit_mode("ak5", True)

    router = mock.MagicMock(spec=DefaultLLMRouter)
    agent = mock.MagicMock(spec=RouterAgent)
    mcp_def = ToolDef(
        name="mcp_remote_search",
        description="remote mcp tool",
        args_model=_McpArgs,
        runs_in="in_process",
        behavior=OPAQUE_MCP_BEHAVIOR,
    )
    rt._mcp._http_tools = {"mcp_remote_search": mcp_def}
    with mock.patch.object(rt, "_sandbox_service_now"):
        loop = rt._compose_build_loop("ak5", router, agent)

    assert "mcp_remote_search" not in loop.executor.callable_tool_names()
    assert isinstance(loop.executor, AppKitToolExecutor)
    assert loop.executor._appkit_on_widen is not None


def test_set_appkit_mode_and_effective() -> None:
    rt = _rt()
    assert rt._settings._effective_appkit_mode("c_new") is False
    rt.set_appkit_mode("c_on", True)
    assert rt._settings._effective_appkit_mode("c_on") is True
    rt.set_appkit_mode("c_off", False)
    assert rt._settings._effective_appkit_mode("c_off") is False


def test_effective_appkit_mode_reads_live_ejection_state() -> None:
    rt = _rt()
    rt.set_appkit_mode("c_ejected", True)

    with mock.patch.object(rt._appkit_ejections, "is_appkit_ejected", return_value=True):
        assert rt._settings._effective_appkit_mode("c_ejected") is False


def test_appkit_mode_flag_round_trips_from_body() -> None:
    import asyncio

    from disco.agent_server.routes._common import CreateConversationBody
    from disco.agent_server.routes.conversations import make_conversations_router

    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(store)
    router = make_conversations_router(store, runtime=rt)
    route = next(r for r in router.routes if getattr(r, "path", "") == "/conversations")

    resp = asyncio.run(route.endpoint(CreateConversationBody(surface="agent", appkit_mode=True)))
    cid = resp["conversation_id"]
    assert rt._settings._effective_appkit_mode(cid) is True

    resp2 = asyncio.run(route.endpoint(CreateConversationBody(surface="agent")))
    cid2 = resp2["conversation_id"]
    assert rt._settings._effective_appkit_mode(cid2) is False
