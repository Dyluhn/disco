"""Workflow router phase scope enforcement.

Workflow-enabled agent conversations start in a narrow ROUTER phase. The model can
inspect saved workflow cards, draft a definition for review, or enter an approved
workflow. Only after ``enter_workflow`` succeeds does the resolver expose the
compiled workflow run scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from disco.core.workflow import WorkflowScope as CompiledWorkflowScope

from .appkit_scope import APPKIT_READ_TOOLS
from .registry import ToolScope

WORKFLOW_ROUTER_TOOLS: frozenset[str] = frozenset(
    {"list_workflows", "read_workflow_card", "enter_workflow", "draft_workflow"}
)
WORKFLOW_ROUTER_CONTROL_TOOLS: frozenset[str] = frozenset({"needs_input"})
WORKFLOW_RUN_CONTROL_TOOLS: frozenset[str] = frozenset({"finish", "workflow_abort"})

# Match AppKit's strict planning context tools: safe reads/probes only. This set
# intentionally excludes browser, shell/code execution, file writes, MCP names,
# and scheduling tools.
WORKFLOW_ROUTER_CONTEXT_TOOLS: frozenset[str] = APPKIT_READ_TOOLS

WORKFLOW_ROUTER_ALLOWED_TOOLS: frozenset[str] = (
    WORKFLOW_ROUTER_TOOLS
    | WORKFLOW_ROUTER_CONTROL_TOOLS
    | WORKFLOW_ROUTER_CONTEXT_TOOLS
)

_GENERAL_WORKSPACE_TASK_INSTANCE_ID = "general_workspace_task"
_FILE_WORKSPACE_TOOL_PREFIXES: tuple[str, ...] = ("file_",)
_FILE_WORKSPACE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "exact_replace",
        "safe_write_file",
    }
)


class WorkflowPhase(str, Enum):
    """The workflow lifecycle phase for a single conversation."""

    ROUTER = "router"
    RUN = "run"


@dataclass
class WorkflowPhaseState:
    """Mutable per-conversation workflow phase.

    ``enter_workflow`` validates the selected instance, compiles its hard scope,
    and advances this state from ROUTER to RUN(instance).
    """

    phase: WorkflowPhase = WorkflowPhase.ROUTER
    instance_id: str | None = None
    compiled_run_scope: CompiledWorkflowScope | None = None
    output_path_template: str | None = None


def workflow_effective_scope(
    *,
    phase: WorkflowPhase,
    compiled_run_scope: CompiledWorkflowScope | None,
    base_scope: ToolScope,
    output_path_template: str | None = None,
) -> ToolScope:
    """The enforced ToolScope for the workflow router right now."""

    if phase == WorkflowPhase.RUN:
        if compiled_run_scope is None:
            return ToolScope(allowed_tools=frozenset(), preset="workflow_run_uncompiled")
        allowed_tools = compiled_run_scope.allowed_tools | WORKFLOW_RUN_CONTROL_TOOLS
        advertised = compiled_run_scope.advertised | WORKFLOW_RUN_CONTROL_TOOLS
        return ToolScope(
            allowed_tools=allowed_tools,
            advertised_tools=advertised,
            preset="workflow_run",
            workflow_output_path_template=output_path_template,
        )

    context_allowed = WORKFLOW_ROUTER_CONTEXT_TOOLS & base_scope.allowed_tools
    if base_scope.advertised_tools is None:
        context_advertised = context_allowed
    else:
        context_advertised = context_allowed & base_scope.advertised_tools
    return ToolScope(
        allowed_tools=WORKFLOW_ROUTER_TOOLS
        | WORKFLOW_ROUTER_CONTROL_TOOLS
        | context_allowed,
        advertised_tools=WORKFLOW_ROUTER_TOOLS
        | WORKFLOW_ROUTER_CONTROL_TOOLS
        | context_advertised,
        preset="workflow_router",
    )


def workflow_router_denial_message(tool_name: str, available: list[str]) -> str:
    """Model-facing recovery hint for registered tools withheld in ROUTER phase."""

    message = (
        f"unknown or out-of-scope tool {tool_name!r}; available: {available}. "
        "This conversation routes work through workflows. In ROUTER phase you have no "
        "build/workspace tools until you enter a workflow. Call list_workflows, then "
        f"enter_workflow(instance_id) to get a scope that includes {tool_name!r}."
    )
    if _is_file_workspace_tool(tool_name):
        message += (
            " For small file/workspace tasks, use "
            f"{_GENERAL_WORKSPACE_TASK_INSTANCE_ID} if it is listed."
        )
    return message


def workflow_run_denial_message(
    tool_name: str,
    available: list[str],
    *,
    output_path_template: str | None,
) -> str:
    """Model-facing recovery hint for registered tools withheld in RUN phase."""

    output_path = output_path_template or "<workflow output path>"
    if tool_name == "finish":
        return (
            "finish refused: this workflow completes by writing "
            f"{output_path}. Write it (file_write), then call finish."
        )
    return (
        f"unknown or out-of-scope tool {tool_name!r}; available: {available}. "
        f"This workflow completes by writing {output_path} and then calling finish; "
        "workflow_abort returns to the router if the goal needs tools outside this seal."
    )


def _is_file_workspace_tool(tool_name: str) -> bool:
    return tool_name in _FILE_WORKSPACE_TOOL_NAMES or tool_name.startswith(
        _FILE_WORKSPACE_TOOL_PREFIXES
    )


__all__ = [
    "WORKFLOW_ROUTER_ALLOWED_TOOLS",
    "WORKFLOW_ROUTER_CONTROL_TOOLS",
    "WORKFLOW_ROUTER_CONTEXT_TOOLS",
    "WORKFLOW_ROUTER_TOOLS",
    "WORKFLOW_RUN_CONTROL_TOOLS",
    "WorkflowPhase",
    "WorkflowPhaseState",
    "workflow_router_denial_message",
    "workflow_run_denial_message",
    "workflow_effective_scope",
]
