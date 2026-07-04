"""Built-in workflow definitions."""

from __future__ import annotations

from .models import (
    WorkflowDefinition,
    WorkflowOutputContract,
    WorkflowPolicies,
    WorkflowVerify,
)

GENERAL_WORKSPACE_TASK_TOOLS: tuple[str, ...] = (
    "file_read",
    "file_write",
    "file_edit",
    "file_list",
)

GENERAL_WORKSPACE_TASK_DEFINITION = WorkflowDefinition(
    name="General Workspace Task",
    card=(
        "A bounded file task with a contracted output: inspect and update workspace "
        "files using only the bounded file tools, then produce reports/task-summary.md."
    ),
    params_model_schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {},
        "required": [],
    },
    tools=GENERAL_WORKSPACE_TASK_TOOLS,
    mcp_mounts=(),
    skills=(),
    policies=WorkflowPolicies(untrusted_content=True, allows_writes=False),
    output_contract=WorkflowOutputContract(
        path_template="reports/task-summary.md",
        format="markdown",
    ),
    verify=WorkflowVerify(checks=("file_exists", "non_empty")),
)

__all__ = [
    "GENERAL_WORKSPACE_TASK_DEFINITION",
    "GENERAL_WORKSPACE_TASK_TOOLS",
]
