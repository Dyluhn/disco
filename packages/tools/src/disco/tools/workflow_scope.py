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

# Match AppKit's strict planning context tools: safe reads/probes only. This set
# intentionally excludes browser, shell/code execution, file writes, MCP names,
# and scheduling tools.
WORKFLOW_ROUTER_CONTEXT_TOOLS: frozenset[str] = APPKIT_READ_TOOLS

WORKFLOW_ROUTER_ALLOWED_TOOLS: frozenset[str] = (
    WORKFLOW_ROUTER_TOOLS | WORKFLOW_ROUTER_CONTEXT_TOOLS
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


def workflow_effective_scope(
    *,
    phase: WorkflowPhase,
    compiled_run_scope: CompiledWorkflowScope | None,
    base_scope: ToolScope,
) -> ToolScope:
    """The enforced ToolScope for the workflow router right now."""

    if phase == WorkflowPhase.RUN:
        if compiled_run_scope is None:
            return ToolScope(allowed_tools=frozenset(), preset="workflow_run_uncompiled")
        return ToolScope(
            allowed_tools=compiled_run_scope.allowed_tools,
            advertised_tools=compiled_run_scope.advertised,
            preset="workflow_run",
        )

    context_allowed = WORKFLOW_ROUTER_CONTEXT_TOOLS & base_scope.allowed_tools
    if base_scope.advertised_tools is None:
        context_advertised = context_allowed
    else:
        context_advertised = context_allowed & base_scope.advertised_tools
    return ToolScope(
        allowed_tools=WORKFLOW_ROUTER_TOOLS | context_allowed,
        advertised_tools=WORKFLOW_ROUTER_TOOLS | context_advertised,
        preset="workflow_router",
    )


__all__ = [
    "WORKFLOW_ROUTER_ALLOWED_TOOLS",
    "WORKFLOW_ROUTER_CONTEXT_TOOLS",
    "WORKFLOW_ROUTER_TOOLS",
    "WorkflowPhase",
    "WorkflowPhaseState",
    "workflow_effective_scope",
]
