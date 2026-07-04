"""WF-3 workflow router runtime wiring."""

from __future__ import annotations

from unittest import mock

import pytest
from disco.agent_server.runtime import ConversationRuntime, workflow_router_enabled
from disco.core import SqliteEventStore
from disco.core.llm import DefaultLLMRouter
from disco.core.loop import RouterAgent
from disco.tools import (
    AGENT_TOOLS,
    WORKFLOW_ROUTER_TOOLS,
    DefaultToolExecutor,
    ScopedPhaseExecutor,
)


def _runtime() -> ConversationRuntime:
    return ConversationRuntime(SqliteEventStore(":memory:"))


def _compose(rt: ConversationRuntime, cid: str, *, surface: str):
    rt.set_surface(cid, surface)
    router = mock.MagicMock(spec=DefaultLLMRouter)
    agent = mock.MagicMock(spec=RouterAgent)
    with mock.patch.object(rt, "_sandbox_service_now"):
        loop = rt._compose_build_loop(cid, router, agent)
    rt._loops[cid] = loop
    return loop


def test_workflow_router_flag_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISCO_WORKFLOW_ROUTER", raising=False)
    monkeypatch.delenv("PMX_WORKFLOW_ROUTER", raising=False)

    assert workflow_router_enabled() is False

    rt = _runtime()
    loop = _compose(rt, "wf_default_off", surface="agent")

    assert isinstance(loop.executor, DefaultToolExecutor)
    assert not isinstance(loop.executor, ScopedPhaseExecutor)
    assert loop.executor._scope.allowed_tools == AGENT_TOOLS


def test_workflow_router_flag_on_agent_uses_router_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISCO_WORKFLOW_ROUTER", "1")
    rt = _runtime()

    loop = _compose(rt, "wf_agent_on", surface="agent")

    assert isinstance(loop.executor, ScopedPhaseExecutor)
    names = loop.executor.callable_tool_names()
    assert WORKFLOW_ROUTER_TOOLS <= names
    for forbidden in ("browser", "shell", "shell_exec", "file_write", "mcp__x__y"):
        assert forbidden not in loop.executor._scope.allowed_tools
    assert WORKFLOW_ROUTER_TOOLS <= loop._planning_tools


def test_workflow_router_flag_on_build_surface_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISCO_WORKFLOW_ROUTER", "1")
    rt = _runtime()

    loop = _compose(rt, "wf_build_on", surface="build")

    assert isinstance(loop.executor, DefaultToolExecutor)
    assert not isinstance(loop.executor, ScopedPhaseExecutor)
    assert loop.executor._scope.allowed_tools == AGENT_TOOLS
    assert WORKFLOW_ROUTER_TOOLS.isdisjoint(loop.executor.callable_tool_names())
