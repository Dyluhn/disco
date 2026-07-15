"""WF-3 workflow router runtime wiring."""

from __future__ import annotations

import json
from unittest import mock

import pytest
from disco.agent_server.runtime import ConversationRuntime, workflow_router_enabled
from disco.core import PlanEvent, SqliteEventStore, StatusEvent, ToolCall
from disco.core.llm import DefaultLLMRouter, OperatingMode
from disco.core.loop import RouterAgent
from disco.core.workflow import (
    WorkflowApproval,
    WorkflowDefinition,
    WorkflowInstance,
    WorkflowOutputContract,
    WorkflowPolicies,
    WorkflowVerify,
)
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


def _workflow_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        name="browser_automation",
        card="Use browser automation to inspect and report on a requested page.",
        params_model_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {},
        },
        tools=("browser",),
        policies=WorkflowPolicies(egress_allow=("example.com",)),
        output_contract=WorkflowOutputContract(
            path_template="outputs/browser.md",
            format="markdown",
        ),
        verify=WorkflowVerify(checks=("output_exists",), finalizer="finish"),
    )


def _save_approved_workflow(rt: ConversationRuntime, instance_id: str) -> None:
    defn = _workflow_definition()
    digest = defn.digest()
    instance = WorkflowInstance(
        definition_digest=digest,
        definition=defn,
        params={},
        enabled=True,
        approval=WorkflowApproval(
            approved_at="2026-07-04T12:00:00Z",
            approved_by="user_1",
            surface_shown_digest=digest,
        ),
    )
    root = rt.project_store().root
    assert root is not None
    workflows_dir = root / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)
    (workflows_dir / f"{instance_id}.json").write_text(
        json.dumps(instance.model_dump(mode="json")),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_enter_workflow_seeds_approved_plan_and_exposes_run_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISCO_WORKFLOW_ROUTER", "1")
    rt = _runtime()
    cid = "wf_enter_exec"
    instance_id = "browser_auto"
    _save_approved_workflow(rt, instance_id)
    loop = _compose(rt, cid, surface="agent")

    entered = await loop.executor.execute(
        ToolCall(tool_name="enter_workflow", arguments={"instance_id": instance_id})
    )

    assert entered.success, entered.content
    assert loop.mode == OperatingMode.LONG_HORIZON
    events = await rt._store.get_events(cid)
    plan = next(e for e in events if isinstance(e, PlanEvent))
    assert [step.title for step in plan.steps] == [
        "Run workflow: browser_automation — outputs/browser.md"
    ]
    assert any(isinstance(e, StatusEvent) and e.detail == "plan_approved" for e in events)
    execution_tools = {tool.name for tool in loop._tools_for_step()}
    assert "browser" in execution_tools
    assert "enter_workflow" not in execution_tools

    aborted = await loop.executor.execute(
        ToolCall(tool_name="workflow_abort", arguments={"reason": "wrong workflow"})
    )

    assert aborted.success, aborted.content
    assert loop.mode == OperatingMode.PLANNING
    router_tools = {tool.name for tool in loop._tools_for_step()}
    assert "enter_workflow" in router_tools
    assert "browser" not in router_tools
