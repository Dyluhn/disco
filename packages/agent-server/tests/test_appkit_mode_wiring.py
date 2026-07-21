"""AppKit runtime wiring of phase-based tool-scope enforcement."""

from __future__ import annotations

from unittest import mock

from disco.agent_server.runtime import ConversationRuntime
from disco.core import (
    ActionEvent,
    ObservationEvent,
    SecurityRisk,
    SqliteEventStore,
    ToolCall,
    ToolResult,
    WorkspaceMutationEvent,
)
from disco.core.llm import DefaultLLMRouter, ModelRole, OperatingMode
from disco.core.loop import BlastRadiusConfirm, RouterAgent
from disco.core.loop.driver import Driver
from disco.core.security import RuleBasedAnalyzer
from disco.tools import AGENT_TOOLS, AppKitPhase, AppKitToolExecutor, DefaultToolExecutor

_EXECUTION_READS = {"file_list", "file_read", "search", "extract", "think"}


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
    rt._loops[cid] = loop
    return loop


def test_appkit_mode_builds_appkit_executor() -> None:
    rt = _rt()
    loop = _loop_for(rt, "ak1", appkit_mode=True)
    assert isinstance(loop.executor, AppKitToolExecutor)


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
    with mock.patch.object(rt, "_router_now", wraps=rt._router_now) as router_now:
        with mock.patch.object(rt, "_sandbox_service_now"):
            rt._loop_for(cid)
    assert any(call.kwargs.get("appkit_mode") is True for call in router_now.call_args_list)
    prompt = rt._loops[cid]._router._prompts.system_prompt(
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
    observation = ObservationEvent(
        action_id=action.id,
        agent_view_id="view-1",
        tool_result=ToolResult(
            call_id=custom_call.call_id,
            tool_name=custom_call.tool_name,
            success=True,
            content="approved",
        ),
    ).model_copy(update={"seq": 4})
    await loop.executor.prepare_for_events([intent, admission, action, observation])
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

    class _FakeMcpDef:
        name = "mcp_remote_search"
        description = "remote mcp tool"

    router = mock.MagicMock(spec=DefaultLLMRouter)
    agent = mock.MagicMock(spec=RouterAgent)
    rt._mcp_http_tools = {"mcp_remote_search": _FakeMcpDef()}  # type: ignore[assignment]
    with mock.patch.object(rt, "_sandbox_service_now"):
        loop = rt._compose_build_loop("ak5", router, agent)

    assert "mcp_remote_search" not in loop.executor.callable_tool_names()
    assert isinstance(loop.executor, AppKitToolExecutor)
    assert loop.executor._appkit_on_widen is not None


def test_set_appkit_mode_and_effective() -> None:
    rt = _rt()
    assert rt._effective_appkit_mode("c_new") is False
    rt.set_appkit_mode("c_on", True)
    assert rt._effective_appkit_mode("c_on") is True
    rt.set_appkit_mode("c_off", False)
    assert rt._effective_appkit_mode("c_off") is False


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
    assert rt._effective_appkit_mode(cid) is True

    resp2 = asyncio.run(route.endpoint(CreateConversationBody(surface="agent")))
    cid2 = resp2["conversation_id"]
    assert rt._effective_appkit_mode(cid2) is False
