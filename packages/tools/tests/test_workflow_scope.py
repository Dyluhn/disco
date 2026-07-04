"""WF-3 workflow router scope and router-tool tests."""

from __future__ import annotations

import json

import pytest
from disco.core.llm import ModelExecutionPolicy
from disco.core.workflow import (
    GENERAL_WORKSPACE_TASK_TOOLS,
    WORKFLOW_CONTROL_TOOLS,
    WorkflowApproval,
    WorkflowDefinition,
    WorkflowInstance,
    WorkflowOutputContract,
    WorkflowPolicies,
    WorkflowScope,
    WorkflowVerify,
)
from disco.tools import (
    WORKFLOW_ROUTER_TOOLS,
    DefaultToolExecutor,
    ScopedPhaseExecutor,
    ToolRegistry,
    ToolScope,
    WorkflowPhase,
    WorkflowPhaseState,
    agent_scope,
    build_default_registry,
    workflow_effective_scope,
)
from disco.tools.appkit_scope import APPKIT_READ_TOOLS
from disco.tools.builtin.workflow_tools import (
    DraftWorkflowTool,
    EnterWorkflowTool,
    JsonDirWorkflowStore,
    ListWorkflowsTool,
    StoredWorkflowInstance,
    WorkflowStore,
)
from disco.tools.workflow_seed import (
    GENERAL_WORKSPACE_TASK_INSTANCE_ID,
    seed_general_workspace_task,
)
from tool_fakes import call

_STANDARD = ModelExecutionPolicy.standard()


def _definition(*, tools: tuple[str, ...] = ("file_read",)) -> WorkflowDefinition:
    return WorkflowDefinition(
        name="research_summary",
        card="Summarize a bounded research query into a single workflow output file.",
        params_model_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        tools=tools,
        policies=WorkflowPolicies(),
        output_contract=WorkflowOutputContract(
            path_template="outputs/{query}.md",
            format="markdown",
        ),
        verify=WorkflowVerify(checks=("output_exists",), finalizer="ready_for_workflow_output"),
    )


def _approval(defn: WorkflowDefinition) -> WorkflowApproval:
    return WorkflowApproval(
        approved_at="2026-07-04T12:00:00Z",
        approved_by="user_1",
        surface_shown_digest=defn.digest(),
    )


def _instance(
    *,
    enabled: bool,
    approved: bool,
    tools: tuple[str, ...] = ("file_read",),
) -> WorkflowInstance:
    defn = _definition(tools=tools)
    return WorkflowInstance(
        definition_digest=defn.digest(),
        definition=defn,
        params={"query": "mcp safety"},
        enabled=enabled,
        approval=_approval(defn) if approved else None,
    )


class _MemoryWorkflowStore(WorkflowStore):
    def __init__(self, instances: dict[str, WorkflowInstance]) -> None:
        self._instances = instances

    def list_instances(self) -> list[StoredWorkflowInstance]:
        return [
            StoredWorkflowInstance(instance_id=key, instance=value)
            for key, value in sorted(self._instances.items())
        ]

    def get_instance(self, instance_id: str) -> WorkflowInstance | None:
        return self._instances.get(instance_id)


def test_workflow_router_allowlist_is_router_tools_plus_appkit_reads() -> None:
    base = agent_scope(model_policy=_STANDARD)
    scope = workflow_effective_scope(
        phase=WorkflowPhase.ROUTER,
        compiled_run_scope=None,
        base_scope=base,
    )

    assert scope.allowed_tools == WORKFLOW_ROUTER_TOOLS | (APPKIT_READ_TOOLS & base.allowed_tools)
    assert WORKFLOW_ROUTER_TOOLS <= scope.allowed_tools
    for forbidden in (
        "browser",
        "shell",
        "shell_exec",
        "code_exec",
        "file_write",
        "safe_write_file",
        "file_edit",
        "mcp__github__search_issues",
        "schedule_create",
    ):
        assert forbidden not in scope.allowed_tools


def test_workflow_phase_swap_changes_scoped_executor_resolver_output() -> None:
    state = WorkflowPhaseState()
    base = agent_scope(model_policy=_STANDARD)
    registry = build_default_registry()
    executor_ref: dict[str, ScopedPhaseExecutor] = {}

    def _resolver() -> ToolScope:
        executor = executor_ref["executor"]
        return workflow_effective_scope(
            phase=state.phase,
            compiled_run_scope=state.compiled_run_scope,
            base_scope=executor.widened_scope,
        )

    executor = ScopedPhaseExecutor(registry, base, scope_resolver=_resolver)
    executor_ref["executor"] = executor

    assert "enter_workflow" not in executor.callable_tool_names()
    assert "file_write" not in executor.callable_tool_names()
    assert "file_read" in executor.callable_tool_names()

    state.phase = WorkflowPhase.RUN
    state.instance_id = "wf_ok"
    state.compiled_run_scope = WorkflowScope(
        allowed_tools=frozenset({"file_write"}),
        advertised=frozenset({"file_write"}),
    )

    assert executor.tool_scope("file_write") == "sandbox"
    assert executor.callable_tool_names() == frozenset({"file_write"})


def test_json_dir_workflow_store_reads_instances_from_projects_root(tmp_path) -> None:
    store = JsonDirWorkflowStore(tmp_path)
    workflows_dir = tmp_path / "workflows"
    workflows_dir.mkdir()
    instance = _instance(enabled=True, approved=True)
    (workflows_dir / "wf_ok.json").write_text(
        json.dumps(instance.model_dump(mode="json")),
        encoding="utf-8",
    )

    rows = store.list_instances()

    assert [row.instance_id for row in rows] == ["wf_ok"]
    assert store.get_instance("wf_ok") == instance
    assert store.get_instance("missing") is None


@pytest.mark.asyncio
async def test_enter_workflow_requires_enabled_and_approved() -> None:
    state = WorkflowPhaseState()
    registry = ToolRegistry()
    store = _MemoryWorkflowStore(
        {
            "disabled": _instance(enabled=False, approved=True),
            "unapproved": _instance(enabled=True, approved=False),
            "approved": _instance(enabled=True, approved=True),
        }
    )
    registry.register(EnterWorkflowTool(store, state, lambda: frozenset()))
    executor = DefaultToolExecutor(
        registry,
        ToolScope(allowed_tools=frozenset({"enter_workflow"})),
    )

    disabled = await executor.execute(call("enter_workflow", instance_id="disabled"))
    unapproved = await executor.execute(call("enter_workflow", instance_id="unapproved"))
    approved = await executor.execute(call("enter_workflow", instance_id="approved"))

    assert disabled.success is False
    assert disabled.structured is not None
    assert disabled.structured["reason"] == "workflow_not_enabled"
    assert unapproved.success is False
    assert unapproved.structured is not None
    assert unapproved.structured["reason"] == "workflow_not_approved"
    assert approved.success is True
    assert state.phase == WorkflowPhase.RUN
    assert state.instance_id == "approved"
    assert state.compiled_run_scope is not None
    assert "file_read" in state.compiled_run_scope.allowed_tools


@pytest.mark.asyncio
async def test_seeded_general_workspace_task_lists_and_enters_bounded_scope(
    tmp_path,
) -> None:
    seed_general_workspace_task(tmp_path)
    store = JsonDirWorkflowStore(tmp_path)
    state = WorkflowPhaseState()
    registry = build_default_registry()
    for tool in (
        ListWorkflowsTool(store),
        EnterWorkflowTool(store, state, lambda: frozenset()),
    ):
        registry.register(tool)
    executor_ref: dict[str, ScopedPhaseExecutor] = {}

    def _resolver() -> ToolScope:
        executor = executor_ref["executor"]
        return workflow_effective_scope(
            phase=state.phase,
            compiled_run_scope=state.compiled_run_scope,
            base_scope=executor.widened_scope,
        )

    executor = ScopedPhaseExecutor(
        registry,
        agent_scope(model_policy=_STANDARD),
        scope_resolver=_resolver,
    )
    executor_ref["executor"] = executor

    listed = await executor.execute(call("list_workflows"))

    assert listed.success is True
    assert listed.structured is not None
    workflows = listed.structured["workflows"]
    assert len(workflows) == 1
    assert workflows[0]["instance_id"] == GENERAL_WORKSPACE_TASK_INSTANCE_ID
    assert workflows[0]["name"] == "General Workspace Task"
    assert workflows[0]["enabled"] is True
    assert workflows[0]["approved"] is True

    entered = await executor.execute(
        call("enter_workflow", instance_id=GENERAL_WORKSPACE_TASK_INSTANCE_ID)
    )

    expected = frozenset(GENERAL_WORKSPACE_TASK_TOOLS) | WORKFLOW_CONTROL_TOOLS
    assert entered.success is True
    assert state.phase == WorkflowPhase.RUN
    assert state.compiled_run_scope is not None
    assert state.compiled_run_scope.allowed_tools == expected
    assert executor._scope.allowed_tools == expected
    for forbidden in ("browser", "shell"):
        rejected = await executor.execute(call(forbidden))
        assert rejected.success is False
        assert rejected.structured is not None
        assert rejected.structured["kind"] == "unknown_tool"


@pytest.mark.asyncio
async def test_draft_workflow_returns_unpersisted_definition_json() -> None:
    registry = ToolRegistry()
    registry.register(DraftWorkflowTool())
    executor = DefaultToolExecutor(
        registry,
        ToolScope(allowed_tools=frozenset({"draft_workflow"})),
    )

    result = await executor.execute(
        call(
            "draft_workflow",
            name="research_summary",
            card="Summarize a bounded research query into a workflow output.",
            params=[
                {
                    "name": "query",
                    "type": "string",
                    "required": True,
                    "description": "Research query.",
                }
            ],
            tools=["file_read", "search"],
            output_path_template="outputs/{query}.md",
            output_format="markdown",
            verify_checks=["output_exists"],
            finalizer="ready_for_workflow_output",
        )
    )

    assert result.success is True
    assert result.structured is not None
    assert result.structured["status"] == "draft"
    assert result.structured["persisted"] is False
    assert result.content.startswith("DRAFT WorkflowDefinition:")
    WorkflowDefinition.model_validate(result.structured["definition"])
