"""WF-3 workflow router scope and router-tool tests."""

from __future__ import annotations

import json

import pytest
from disco.core import SecurityRisk
from disco.core.llm import ModelExecutionPolicy
from disco.core.workflow import (
    BROWSER_AUTOMATION_DEFINITION,
    BROWSER_AUTOMATION_TOOLS,
    BUILTIN_WORKFLOW_TOOLS,
    DAILY_EMAIL_BRIEF_DEFINITION,
    DAILY_EMAIL_BRIEF_MCP_TOOL_NAMES,
    DAILY_EMAIL_BRIEF_TOOLS,
    DOCUMENT_DECK_STUDIO_DEFINITION,
    DOCUMENT_DECK_STUDIO_TOOLS,
    FORM_FILL_DEFINITION,
    FORM_FILL_TOOLS,
    GENERAL_WORKSPACE_TASK_DEFINITION,
    GENERAL_WORKSPACE_TASK_TOOLS,
    SCRIPTED_WORKSPACE_TASK_DEFINITION,
    SCRIPTED_WORKSPACE_TASK_TOOLS,
    SKILL_AUTHORING_DEFINITION,
    SKILL_AUTHORING_TOOLS,
    WORKFLOW_CONTROL_TOOLS,
    McpMount,
    WorkflowApproval,
    WorkflowDefinition,
    WorkflowInstance,
    WorkflowOutputContract,
    WorkflowPolicies,
    WorkflowScope,
    WorkflowVerify,
    compile_workflow_scope,
    simulate_definition,
    validate_definition,
)
from disco.tools import (
    AGENT_TOOLS,
    WORKFLOW_ROUTER_CONTROL_TOOLS,
    WORKFLOW_ROUTER_TOOLS,
    WORKFLOW_RUN_CONTROL_TOOLS,
    DefaultToolExecutor,
    ScopedPhaseExecutor,
    ToolContext,
    ToolDef,
    ToolOutcome,
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
    DraftWorkflowArgs,
    DraftWorkflowTool,
    EnterWorkflowTool,
    JsonDirWorkflowStore,
    ListWorkflowsTool,
    StoredWorkflowInstance,
    WorkflowStore,
    build_workflow_definition,
    workflow_router_tools,
)
from disco.tools.workflow_seed import (
    BROWSER_AUTOMATION_INSTANCE_ID,
    DAILY_EMAIL_BRIEF_INSTANCE_ID,
    DOCUMENT_DECK_STUDIO_INSTANCE_ID,
    FORM_FILL_INSTANCE_ID,
    GENERAL_WORKSPACE_TASK_INSTANCE_ID,
    SCRIPTED_WORKSPACE_TASK_INSTANCE_ID,
    SKILL_AUTHORING_INSTANCE_ID,
    seed_builtin_workflows,
    seed_daily_email_brief,
    seed_document_deck_studio,
    seed_general_workspace_task,
    seed_scripted_workspace_task,
)
from pydantic import BaseModel, ConfigDict
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


class _FakeMcpArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _FakeMcpTool:
    def __init__(self, name: str, *, read_only: bool) -> None:
        self.definition = ToolDef(
            name=name,
            description=f"Fake MCP tool {name}",
            args_model=_FakeMcpArgs,
            base_risk=SecurityRisk.LOW,
            runs_in="in_process",
            read_only=read_only,
        )

    async def run(self, args: _FakeMcpArgs, ctx: ToolContext) -> ToolOutcome:  # noqa: ARG002
        return ToolOutcome(success=True, content="ok")


def _fake_gmail_mcp_names() -> frozenset[str]:
    registry = build_default_registry()
    for name, read_only in (
        ("mcp__gmail__search_threads", True),
        ("mcp__gmail__get_thread", True),
        ("mcp__gmail__create_draft", False),
        ("mcp__gmail__send_message", False),
        ("mcp__gmail__label_thread", False),
    ):
        registry.register(_FakeMcpTool(name, read_only=read_only))
    return frozenset(name for name in registry.names() if name.startswith("mcp__"))


def _daily_email_brief_expected_surface() -> frozenset[str]:
    return (
        frozenset(DAILY_EMAIL_BRIEF_TOOLS)
        | {
            "mcp__gmail__search_threads",
            "mcp__gmail__get_thread",
        }
        | WORKFLOW_CONTROL_TOOLS
    )


def _first_party_expected_surface(tools: tuple[str, ...]) -> frozenset[str]:
    return frozenset(tools) | WORKFLOW_CONTROL_TOOLS


def test_workflow_router_allowlist_is_router_tools_plus_appkit_reads() -> None:
    base = agent_scope(model_policy=_STANDARD)
    scope = workflow_effective_scope(
        phase=WorkflowPhase.ROUTER,
        compiled_run_scope=None,
        base_scope=base,
    )

    assert scope.allowed_tools == (
        WORKFLOW_ROUTER_TOOLS
        | WORKFLOW_ROUTER_CONTROL_TOOLS
        | (APPKIT_READ_TOOLS & base.allowed_tools)
    )
    assert WORKFLOW_ROUTER_TOOLS <= scope.allowed_tools
    assert "needs_input" in scope.allowed_tools
    assert "workflow_abort" not in scope.allowed_tools
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


@pytest.mark.asyncio
async def test_router_phase_denies_file_write_with_workflow_routing_hint() -> None:
    state = WorkflowPhaseState()
    registry = build_default_registry()
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

    result = await executor.execute(call("file_write"))

    assert result.success is False
    assert result.structured is not None
    assert result.structured["kind"] == "unknown_tool"
    assert "This conversation routes work through workflows" in result.content
    assert "enter_workflow(instance_id)" in result.content
    assert "general_workspace_task" in result.content
    assert "file_write" in executor.known_tool_names_for_requery()


@pytest.mark.asyncio
async def test_workflow_run_denial_text_names_workflow_exit() -> None:
    state = WorkflowPhaseState(
        phase=WorkflowPhase.RUN,
        instance_id="wf_read_only",
        compiled_run_scope=WorkflowScope(
            allowed_tools=frozenset({"file_read"}),
            advertised=frozenset({"file_read"}),
        ),
        output_path_template="outputs/report.md",
    )
    registry = build_default_registry()
    executor_ref: dict[str, ScopedPhaseExecutor] = {}

    def _resolver() -> ToolScope:
        executor = executor_ref["executor"]
        return workflow_effective_scope(
            phase=state.phase,
            compiled_run_scope=state.compiled_run_scope,
            base_scope=executor.widened_scope,
            output_path_template=state.output_path_template,
        )

    executor = ScopedPhaseExecutor(
        registry,
        agent_scope(model_policy=_STANDARD),
        scope_resolver=_resolver,
    )
    executor_ref["executor"] = executor

    result = await executor.execute(call("file_write"))

    assert result.success is False
    assert (
        result.content
        == "unknown or out-of-scope tool 'file_write'; available: ['file_read']. "
        "This workflow completes by writing outputs/report.md and then calling finish; "
        "workflow_abort returns to the router if the goal needs tools outside this seal."
    )
    assert "enter_workflow" not in result.content
    assert "general_workspace_task" not in result.content


@pytest.mark.asyncio
async def test_workflow_run_finish_denial_is_actionable_if_it_reaches_executor() -> None:
    state = WorkflowPhaseState(
        phase=WorkflowPhase.RUN,
        instance_id="wf_read_only",
        compiled_run_scope=WorkflowScope(
            allowed_tools=frozenset({"file_read"}),
            advertised=frozenset({"file_read"}),
        ),
        output_path_template="outputs/report.md",
    )
    registry = build_default_registry()
    executor_ref: dict[str, ScopedPhaseExecutor] = {}

    def _resolver() -> ToolScope:
        executor = executor_ref["executor"]
        return workflow_effective_scope(
            phase=state.phase,
            compiled_run_scope=state.compiled_run_scope,
            base_scope=executor.widened_scope,
            output_path_template=state.output_path_template,
        )

    executor = ScopedPhaseExecutor(
        registry,
        agent_scope(model_policy=_STANDARD),
        scope_resolver=_resolver,
    )
    executor_ref["executor"] = executor

    result = await executor.execute(call("finish", summary="done"))

    assert result.success is False
    assert result.structured is not None
    assert result.structured["kind"] == "unknown_tool"
    assert result.content == (
        "finish refused: this workflow completes by writing outputs/report.md. "
        "Write it (file_write), then call finish."
    )
    assert "unknown or out-of-scope" not in result.content


@pytest.mark.asyncio
async def test_default_executor_denial_text_unchanged_when_router_not_active() -> None:
    executor = DefaultToolExecutor(
        build_default_registry(),
        ToolScope(allowed_tools=frozenset({"file_read"})),
    )

    result = await executor.execute(call("file_write"))

    assert result.success is False
    assert result.content == "unknown or out-of-scope tool 'file_write'; available: ['file_read']"
    assert "enter_workflow" not in result.content
    assert "general_workspace_task" not in result.content


def test_sealed_run_scope_excludes_router_and_ask_tools() -> None:
    compiled = compile_workflow_scope(_definition(tools=("file_read",)), frozenset())
    base = ToolScope(
        allowed_tools=AGENT_TOOLS
        | WORKFLOW_ROUTER_TOOLS
        | frozenset({"ask_user", "clarify", "questions_v2"}),
        advertised_tools=None,
    )

    scope = workflow_effective_scope(
        phase=WorkflowPhase.RUN,
        compiled_run_scope=compiled,
        base_scope=base,
    )

    assert {"file_read", "finish", "skip", "needs_input"} <= scope.allowed_tools
    assert WORKFLOW_RUN_CONTROL_TOOLS <= scope.allowed_tools
    assert scope.allowed_tools.isdisjoint(WORKFLOW_ROUTER_TOOLS)
    assert scope.allowed_tools.isdisjoint({"ask_user", "clarify", "questions_v2"})
    assert scope.advertised_tools == scope.allowed_tools


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


def test_json_dir_workflow_store_saves_instances_to_projects_root(tmp_path) -> None:
    store = JsonDirWorkflowStore(tmp_path)
    instance = _instance(enabled=False, approved=False)

    path = store.save_instance("wf_draft", instance)

    assert path == tmp_path / "workflows" / "wf_draft.json"
    assert store.get_instance("wf_draft") == instance


def test_validate_definition_collects_all_preapproval_findings() -> None:
    mount = McpMount(server="github", tool_names=("create_issue",), read_only=False)
    defn = _definition(tools=("file_read", "unknown_builtin")).model_copy(
        update={
            "params_model_schema": {"type": "object"},
            "mcp_mounts": (mount,),
            "skills": ("missing_skill",),
        }
    )

    findings = validate_definition(
        defn,
        BUILTIN_WORKFLOW_TOOLS | WORKFLOW_CONTROL_TOOLS,
        frozenset(),
    )

    codes = {finding.code for finding in findings}
    assert {
        "params_schema_gate",
        "unknown_builtin_tool",
        "missing_mcp_tool",
        "write_policy_inconsistent",
        "unknown_skill",
    } <= codes


def test_daily_email_brief_definition_compiles_to_exact_gmail_surface() -> None:
    compiled = compile_workflow_scope(
        DAILY_EMAIL_BRIEF_DEFINITION,
        _fake_gmail_mcp_names(),
    )

    expected = _daily_email_brief_expected_surface()
    assert DAILY_EMAIL_BRIEF_MCP_TOOL_NAMES == ("search_threads", "get_thread")
    assert all(mount.read_only for mount in DAILY_EMAIL_BRIEF_DEFINITION.mcp_mounts)
    assert compiled.allowed_tools == expected
    assert compiled.advertised == expected
    assert compiled.advertised.isdisjoint(
        {
            "mcp__gmail__create_draft",
            "mcp__gmail__send_message",
            "mcp__gmail__label_thread",
        }
    )


@pytest.mark.parametrize(
    ("definition", "tools", "forbidden"),
    (
        (
            SCRIPTED_WORKSPACE_TASK_DEFINITION,
            SCRIPTED_WORKSPACE_TASK_TOOLS,
            {"browser", "search", "extract", "mcp__github__search_issues"},
        ),
        (
            BROWSER_AUTOMATION_DEFINITION,
            BROWSER_AUTOMATION_TOOLS,
            {"shell", "shell_exec", "code_exec", "file_edit"},
        ),
        (
            FORM_FILL_DEFINITION,
            FORM_FILL_TOOLS,
            {"shell", "shell_exec", "code_exec", "file_edit", "think"},
        ),
        (
            SKILL_AUTHORING_DEFINITION,
            SKILL_AUTHORING_TOOLS,
            {"browser", "shell", "shell_exec", "code_exec", "file_edit"},
        ),
    ),
)
def test_new_builtin_definitions_compile_to_exact_first_party_surface(
    definition: WorkflowDefinition,
    tools: tuple[str, ...],
    forbidden: set[str],
) -> None:
    compiled = compile_workflow_scope(definition, frozenset())

    expected = _first_party_expected_surface(tools)
    assert compiled.allowed_tools == expected
    assert compiled.advertised == expected
    assert compiled.advertised.isdisjoint(forbidden)


@pytest.mark.parametrize(
    "definition",
    (
        GENERAL_WORKSPACE_TASK_TOOLS,
        SCRIPTED_WORKSPACE_TASK_TOOLS,
        BROWSER_AUTOMATION_TOOLS,
        FORM_FILL_TOOLS,
        SKILL_AUTHORING_TOOLS,
        DAILY_EMAIL_BRIEF_TOOLS,
    ),
)
def test_builtin_workflow_tools_are_known(definition: tuple[str, ...]) -> None:
    assert set(definition) <= BUILTIN_WORKFLOW_TOOLS


@pytest.mark.parametrize(
    ("definition", "available_mcp_names"),
    (
        (GENERAL_WORKSPACE_TASK_DEFINITION, frozenset()),
        (SCRIPTED_WORKSPACE_TASK_DEFINITION, frozenset()),
        (BROWSER_AUTOMATION_DEFINITION, frozenset()),
        (FORM_FILL_DEFINITION, frozenset()),
        (SKILL_AUTHORING_DEFINITION, frozenset()),
        (DAILY_EMAIL_BRIEF_DEFINITION, _fake_gmail_mcp_names()),
    ),
)
def test_builtin_workflow_definition_digests_are_stable(
    definition: WorkflowDefinition,
    available_mcp_names: frozenset[str],
) -> None:
    assert compile_workflow_scope(definition, available_mcp_names).allowed_tools
    findings = validate_definition(
        definition,
        BUILTIN_WORKFLOW_TOOLS | WORKFLOW_CONTROL_TOOLS,
        available_mcp_names,
    )
    if "browser" in definition.tools and not definition.policies.egress_allow:
        assert [(finding.severity, finding.code) for finding in findings] == [
            ("warning", "browser_without_egress")
        ]
    else:
        assert findings == []
    digest = definition.digest()
    round_tripped = WorkflowDefinition.model_validate(definition.model_dump(mode="json"))
    assert definition.digest() == digest
    assert round_tripped.digest() == digest


def test_daily_email_brief_missing_gmail_mount_reports_error_finding() -> None:
    with pytest.raises(ValueError, match="missing mounted MCP tool"):
        compile_workflow_scope(DAILY_EMAIL_BRIEF_DEFINITION, frozenset())

    findings = validate_definition(
        DAILY_EMAIL_BRIEF_DEFINITION,
        BUILTIN_WORKFLOW_TOOLS | WORKFLOW_CONTROL_TOOLS,
        frozenset(),
    )

    missing = [finding for finding in findings if finding.code == "missing_mcp_tool"]
    assert {finding.message for finding in missing} == {
        "missing mounted MCP tool: mcp__gmail__get_thread",
        "missing mounted MCP tool: mcp__gmail__search_threads",
    }


def test_daily_email_brief_sealed_schedule_scope_excludes_router_and_ask_tools() -> None:
    compiled = compile_workflow_scope(
        DAILY_EMAIL_BRIEF_DEFINITION,
        _fake_gmail_mcp_names(),
    )
    base = ToolScope(
        allowed_tools=AGENT_TOOLS
        | WORKFLOW_ROUTER_TOOLS
        | frozenset({"ask_user", "clarify", "questions_v2"}),
        advertised_tools=None,
    )

    scope = workflow_effective_scope(
        phase=WorkflowPhase.RUN,
        compiled_run_scope=compiled,
        base_scope=base,
    )

    expected = _daily_email_brief_expected_surface() | WORKFLOW_RUN_CONTROL_TOOLS
    assert scope.allowed_tools == expected
    assert scope.allowed_tools.isdisjoint(WORKFLOW_ROUTER_TOOLS)
    assert scope.allowed_tools.isdisjoint({"ask_user", "clarify", "questions_v2"})
    assert scope.advertised_tools == expected


def test_simulate_definition_writes_fixture_output(tmp_path) -> None:
    defn = _definition(tools=("file_read",))

    result = simulate_definition(
        defn,
        params={"query": "mcp-safety"},
        workspace_root=tmp_path,
        available_builtin_names=BUILTIN_WORKFLOW_TOOLS | WORKFLOW_CONTROL_TOOLS,
        available_mcp_names=frozenset(),
    )

    assert result.ok is True
    assert result.output_path == "outputs/mcp-safety.md"
    assert result.output_format == "markdown"
    assert (tmp_path / "outputs" / "mcp-safety.md").read_text(encoding="utf-8").startswith(
        "# Workflow simulation fixture"
    )


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
    assert "default workflow for small file/workspace tasks" in workflows[0]["card"]
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
    assert executor._scope.allowed_tools == expected | WORKFLOW_RUN_CONTROL_TOOLS
    for forbidden in ("browser", "shell"):
        rejected = await executor.execute(call(forbidden))
        assert rejected.success is False
        assert rejected.structured is not None
        assert rejected.structured["kind"] == "unknown_tool"


def test_scripted_workspace_task_definition_compiles_and_seeds(tmp_path) -> None:
    compiled = compile_workflow_scope(SCRIPTED_WORKSPACE_TASK_DEFINITION, frozenset())

    expected = frozenset(SCRIPTED_WORKSPACE_TASK_TOOLS) | WORKFLOW_CONTROL_TOOLS
    assert compiled.allowed_tools == expected
    assert compiled.advertised == expected
    path = seed_scripted_workspace_task(tmp_path)
    instance = JsonDirWorkflowStore(tmp_path).get_instance(
        SCRIPTED_WORKSPACE_TASK_INSTANCE_ID
    )

    assert path == tmp_path / "workflows" / "scripted_workspace_task.json"
    assert instance is not None
    assert instance.enabled is True
    assert instance.approval is not None
    assert instance.approval.approved_by == "disco_builtin_seed"
    assert instance.definition == SCRIPTED_WORKSPACE_TASK_DEFINITION
    assert instance.definition_digest == SCRIPTED_WORKSPACE_TASK_DEFINITION.digest()


def test_document_deck_studio_definition_compiles_and_seeds(tmp_path) -> None:
    compiled = compile_workflow_scope(DOCUMENT_DECK_STUDIO_DEFINITION, frozenset())

    expected = frozenset(DOCUMENT_DECK_STUDIO_TOOLS) | WORKFLOW_CONTROL_TOOLS
    assert compiled.allowed_tools == expected
    assert compiled.advertised == expected
    assert DOCUMENT_DECK_STUDIO_DEFINITION.policies.untrusted_content is True
    assert DOCUMENT_DECK_STUDIO_DEFINITION.policies.allows_writes is True
    assert DOCUMENT_DECK_STUDIO_DEFINITION.output_contract.path_template == (
        "reports/artifact-summary.md"
    )
    assert DOCUMENT_DECK_STUDIO_DEFINITION.verify.checks == (
        "file_exists",
        "non_empty",
    )

    path = seed_document_deck_studio(tmp_path)
    instance = JsonDirWorkflowStore(tmp_path).get_instance(
        DOCUMENT_DECK_STUDIO_INSTANCE_ID
    )

    assert path == tmp_path / "workflows" / "document_deck_studio.json"
    assert instance is not None
    assert instance.enabled is True
    assert instance.approval is not None
    assert instance.approval.approved_by == "disco_builtin_seed"
    assert instance.definition == DOCUMENT_DECK_STUDIO_DEFINITION
    assert instance.definition_digest == DOCUMENT_DECK_STUDIO_DEFINITION.digest()


@pytest.mark.asyncio
async def test_workflow_abort_flips_run_to_router_and_reexposes_router_scope() -> None:
    state = WorkflowPhaseState(
        phase=WorkflowPhase.RUN,
        instance_id="wf_wrong",
        compiled_run_scope=WorkflowScope(
            allowed_tools=frozenset({"file_read", "needs_input"}),
            advertised=frozenset({"file_read", "needs_input"}),
        ),
    )
    registry = build_default_registry()
    for tool in workflow_router_tools(
        store=_MemoryWorkflowStore({}),
        phase_state=state,
        mcp_tool_names_getter=lambda: frozenset(),
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

    assert "workflow_abort" in executor.callable_tool_names()
    assert "list_workflows" not in executor.callable_tool_names()

    result = await executor.execute(
        call("workflow_abort", reason="selected workflow cannot run commands")
    )

    assert result.success is True
    assert state.phase == WorkflowPhase.ROUTER
    assert state.instance_id is None
    assert state.compiled_run_scope is None
    assert "You are back at the router" in result.content
    assert "list_workflows" in executor.callable_tool_names()
    assert "enter_workflow" in executor.callable_tool_names()
    assert "workflow_abort" not in executor.callable_tool_names()


@pytest.mark.asyncio
async def test_workflow_abort_is_out_of_scope_in_router_phase() -> None:
    state = WorkflowPhaseState()
    registry = build_default_registry()
    for tool in workflow_router_tools(
        store=_MemoryWorkflowStore({}),
        phase_state=state,
        mcp_tool_names_getter=lambda: frozenset(),
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

    result = await executor.execute(call("workflow_abort", reason="not in a run"))

    assert result.success is False
    assert result.structured is not None
    assert result.structured["kind"] == "unknown_tool"
    assert "workflow_abort" not in executor.callable_tool_names()
    assert "list_workflows" in executor.callable_tool_names()


def test_seeded_daily_email_brief_is_disabled_and_unapproved(tmp_path) -> None:
    seed_daily_email_brief(tmp_path)
    store = JsonDirWorkflowStore(tmp_path)

    instance = store.get_instance(DAILY_EMAIL_BRIEF_INSTANCE_ID)

    assert instance is not None
    assert instance.enabled is False
    assert instance.approval is None
    assert instance.definition == DAILY_EMAIL_BRIEF_DEFINITION
    assert instance.params == {}
    assert instance.definition.output_contract.path_template == "reports/email-brief-{date}.md"


def test_seed_builtin_workflows_writes_all_seven_instances(tmp_path) -> None:
    seeded = seed_builtin_workflows(tmp_path)
    store = JsonDirWorkflowStore(tmp_path)

    assert [instance_id for instance_id, _ in seeded] == [
        GENERAL_WORKSPACE_TASK_INSTANCE_ID,
        SCRIPTED_WORKSPACE_TASK_INSTANCE_ID,
        DOCUMENT_DECK_STUDIO_INSTANCE_ID,
        DAILY_EMAIL_BRIEF_INSTANCE_ID,
        BROWSER_AUTOMATION_INSTANCE_ID,
        FORM_FILL_INSTANCE_ID,
        SKILL_AUTHORING_INSTANCE_ID,
    ]
    assert {path.name for _, path in seeded} == {
        "general_workspace_task.json",
        "scripted_workspace_task.json",
        "document_deck_studio.json",
        "daily_email_brief.json",
        "browser_automation.json",
        "form_fill.json",
        "skill_authoring.json",
    }

    instances = {
        row.instance_id: row.instance for row in store.list_instances()
    }
    assert set(instances) == {
        GENERAL_WORKSPACE_TASK_INSTANCE_ID,
        SCRIPTED_WORKSPACE_TASK_INSTANCE_ID,
        DOCUMENT_DECK_STUDIO_INSTANCE_ID,
        DAILY_EMAIL_BRIEF_INSTANCE_ID,
        BROWSER_AUTOMATION_INSTANCE_ID,
        FORM_FILL_INSTANCE_ID,
        SKILL_AUTHORING_INSTANCE_ID,
    }
    for instance_id in (
        GENERAL_WORKSPACE_TASK_INSTANCE_ID,
        SCRIPTED_WORKSPACE_TASK_INSTANCE_ID,
        DOCUMENT_DECK_STUDIO_INSTANCE_ID,
        BROWSER_AUTOMATION_INSTANCE_ID,
        FORM_FILL_INSTANCE_ID,
        SKILL_AUTHORING_INSTANCE_ID,
    ):
        instance = instances[instance_id]
        assert instance.enabled is True
        assert instance.approval is not None
        assert instance.approval.surface_shown_digest == instance.definition.digest()

    assert instances[DAILY_EMAIL_BRIEF_INSTANCE_ID].enabled is False
    assert instances[DAILY_EMAIL_BRIEF_INSTANCE_ID].approval is None


def test_seed_builtin_workflows_refreshes_stale_builtin_digest(tmp_path) -> None:
    seed_general_workspace_task(tmp_path)
    stale_path = tmp_path / "workflows" / "general_workspace_task.json"
    payload = json.loads(stale_path.read_text(encoding="utf-8"))
    payload["definition_digest"] = "sha256:stale"
    assert isinstance(payload["approval"], dict)
    payload["approval"]["surface_shown_digest"] = "sha256:stale"
    stale_path.write_text(json.dumps(payload), encoding="utf-8")

    seed_builtin_workflows(tmp_path)
    instance = JsonDirWorkflowStore(tmp_path).get_instance(
        GENERAL_WORKSPACE_TASK_INSTANCE_ID
    )

    assert instance is not None
    digest = GENERAL_WORKSPACE_TASK_DEFINITION.digest()
    assert instance.definition_digest == digest
    assert instance.approval is not None
    assert instance.approval.approved_by == "disco_builtin_seed"
    assert instance.approval.surface_shown_digest == digest


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


def test_build_workflow_definition_matches_draft_tool_shape() -> None:
    args = DraftWorkflowArgs(
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

    defn = build_workflow_definition(args)

    assert defn.params_model_schema == {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "query": {"type": "string", "description": "Research query."}
        },
        "required": ["query"],
    }
    assert defn.tools == ("file_read", "search")
    assert defn.output_contract.path_template == "outputs/{query}.md"
