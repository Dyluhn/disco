"""Built-in workflow definitions."""

from __future__ import annotations

from .models import (
    McpMount,
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

DAILY_EMAIL_BRIEF_TOOLS: tuple[str, ...] = ("file_write",)
DAILY_EMAIL_BRIEF_MCP_TOOL_NAMES: tuple[str, ...] = (
    "search_threads",
    "get_thread",
)
_GMAIL_READ_ONLY_TOOL_NAMES = frozenset({"search_threads", "get_thread"})
_GMAIL_MUTATING_TOOL_NAME_FRAGMENTS = ("send", "draft", "label")

assert set(DAILY_EMAIL_BRIEF_MCP_TOOL_NAMES) <= _GMAIL_READ_ONLY_TOOL_NAMES
assert not any(
    fragment in tool_name
    for tool_name in DAILY_EMAIL_BRIEF_MCP_TOOL_NAMES
    for fragment in _GMAIL_MUTATING_TOOL_NAME_FRAGMENTS
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

DAILY_EMAIL_BRIEF_DEFINITION = WorkflowDefinition(
    name="Daily Email Brief",
    card=(
        "Reads Gmail through a user-connected MCP connector using only the read-only "
        "search_threads and get_thread mounts, then writes a daily markdown brief to "
        "reports/email-brief-{date}.md. It NEVER sends, labels, or drafts email."
    ),
    params_model_schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "date": {
                "type": "string",
                "description": "Date for the brief, normally YYYY-MM-DD.",
            },
            "max_threads": {
                "type": "integer",
                "minimum": 1,
                "description": "Maximum Gmail threads to inspect for the brief.",
            },
        },
        "required": [],
    },
    tools=DAILY_EMAIL_BRIEF_TOOLS,
    mcp_mounts=(
        McpMount(
            server="gmail",
            tool_names=DAILY_EMAIL_BRIEF_MCP_TOOL_NAMES,
            read_only=True,
        ),
    ),
    skills=(),
    policies=WorkflowPolicies(untrusted_content=True, allows_writes=False),
    output_contract=WorkflowOutputContract(
        path_template="reports/email-brief-{date}.md",
        format="markdown",
    ),
    verify=WorkflowVerify(checks=("file_exists", "non_empty")),
)

__all__ = [
    "DAILY_EMAIL_BRIEF_DEFINITION",
    "DAILY_EMAIL_BRIEF_MCP_TOOL_NAMES",
    "DAILY_EMAIL_BRIEF_TOOLS",
    "GENERAL_WORKSPACE_TASK_DEFINITION",
    "GENERAL_WORKSPACE_TASK_TOOLS",
]
