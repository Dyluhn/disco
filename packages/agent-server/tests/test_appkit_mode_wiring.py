"""AppKit runtime wiring of phase-based tool-scope enforcement."""

from __future__ import annotations

from unittest import mock

from disco.agent_server.runtime import ConversationRuntime
from disco.core import ActionEvent, SecurityRisk, SqliteEventStore, ToolCall
from disco.core.llm import DefaultLLMRouter, OperatingMode
from disco.core.loop import BlastRadiusConfirm, RouterAgent
from disco.core.security import RuleBasedAnalyzer
from disco.tools import AGENT_TOOLS, AppKitToolExecutor, DefaultToolExecutor


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
    assert "request_custom_build" in callable_names
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
