"""Workflow router tools.

These tools are the narrow entry surface for workflow-enabled agent runs. Saved
instances are read from ``<projects_root>/workflows/*.json`` as
``WorkflowInstance`` documents; approval/persistence of new enabled instances
is handled by the agent-server review flow, so ``draft_workflow`` only returns
a draft definition.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from disco.core import SecurityRisk
from disco.core.workflow import (
    McpMount,
    WorkflowDefinition,
    WorkflowInstance,
    WorkflowOutputContract,
    WorkflowPolicies,
    WorkflowVerify,
    compile_workflow_scope,
)
from pydantic import BaseModel, ConfigDict, Field

from ..anatomy import Tool, ToolContext, ToolDef, ToolOutcome
from ..workflow_scope import WorkflowPhase, WorkflowPhaseState

_SAFE_INSTANCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$")


@dataclass(frozen=True)
class StoredWorkflowInstance:
    instance_id: str
    instance: WorkflowInstance


class WorkflowStore(Protocol):
    def list_instances(self) -> list[StoredWorkflowInstance]: ...

    def get_instance(self, instance_id: str) -> WorkflowInstance | None: ...


class JsonDirWorkflowStore:
    """Read workflow instances from ``<projects_root>/workflows``."""

    def __init__(self, projects_root: str | Path) -> None:
        self._dir = Path(projects_root).expanduser() / "workflows"

    @property
    def workflows_dir(self) -> Path:
        return self._dir

    def _path_for(self, instance_id: str) -> Path:
        if not _SAFE_INSTANCE_ID.fullmatch(instance_id):
            raise ValueError(f"unsafe workflow instance_id: {instance_id!r}")
        return self._dir / f"{instance_id}.json"

    def list_instances(self) -> list[StoredWorkflowInstance]:
        if not self._dir.is_dir():
            return []
        rows: list[StoredWorkflowInstance] = []
        for path in sorted(self._dir.glob("*.json")):
            instance_id = path.stem
            if not _SAFE_INSTANCE_ID.fullmatch(instance_id):
                continue
            try:
                raw = path.read_text(encoding="utf-8")
                instance = WorkflowInstance.model_validate_json(raw)
            except (OSError, ValueError):
                continue
            rows.append(StoredWorkflowInstance(instance_id=instance_id, instance=instance))
        return rows

    def get_instance(self, instance_id: str) -> WorkflowInstance | None:
        path = self._path_for(instance_id)
        if not path.is_file():
            return None
        raw = path.read_text(encoding="utf-8")
        return WorkflowInstance.model_validate_json(raw)

    def save_instance(self, instance_id: str, instance: WorkflowInstance) -> Path:
        path = self._path_for(instance_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{id(instance)}.tmp")
        tmp.write_text(
            json.dumps(instance.model_dump(mode="json"), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp.replace(path)
        return path


class ListWorkflowsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ListWorkflowsTool:
    def __init__(self, store: WorkflowStore) -> None:
        self._store = store

    definition = ToolDef(
        name="list_workflows",
        description=(
            "List saved workflow instances available for this project. Use this "
            "before entering a workflow."
        ),
        args_model=ListWorkflowsArgs,
        base_risk=SecurityRisk.LOW,
        runs_in="in_process",
        read_only=True,
    )

    async def run(self, args: ListWorkflowsArgs, ctx: ToolContext) -> ToolOutcome:
        rows = self._store.list_instances()
        if not rows:
            return ToolOutcome(
                success=True,
                content="No saved workflow instances found.",
                structured={"workflows": []},
            )
        workflows = [_workflow_summary(row) for row in rows]
        lines = [
            f"- {item['instance_id']}: {item['name']} "
            f"(enabled={item['enabled']}, approved={item['approved']})"
            for item in workflows
        ]
        return ToolOutcome(
            success=True,
            content="Available workflows:\n" + "\n".join(lines),
            structured={"workflows": workflows},
        )


class ReadWorkflowCardArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instance_id: str = Field(description="Workflow instance id from list_workflows.")


class ReadWorkflowCardTool:
    def __init__(self, store: WorkflowStore) -> None:
        self._store = store

    definition = ToolDef(
        name="read_workflow_card",
        description=(
            "Read the human-facing card for one workflow instance, including its "
            "approval/enabled state and parameter schema."
        ),
        args_model=ReadWorkflowCardArgs,
        base_risk=SecurityRisk.LOW,
        runs_in="in_process",
        read_only=True,
    )

    async def run(self, args: ReadWorkflowCardArgs, ctx: ToolContext) -> ToolOutcome:
        try:
            instance = self._store.get_instance(args.instance_id)
        except ValueError as exc:
            return _failure("workflow_invalid_id", str(exc))
        if instance is None:
            return _failure(
                "workflow_not_found",
                f"No workflow instance found for {args.instance_id!r}.",
            )
        payload = {
            "instance_id": args.instance_id,
            **_instance_payload(instance),
        }
        return ToolOutcome(
            success=True,
            content=(
                f"{instance.definition.name}\n\n{instance.definition.card}\n\n"
                f"enabled={instance.enabled}; approved={instance.approval is not None}"
            ),
            structured=payload,
        )


class EnterWorkflowArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instance_id: str = Field(description="Enabled and approved workflow instance id.")


class EnterWorkflowTool:
    def __init__(
        self,
        store: WorkflowStore,
        phase_state: WorkflowPhaseState,
        mcp_tool_names_getter: Callable[[], frozenset[str]],
    ) -> None:
        self._store = store
        self._phase_state = phase_state
        self._mcp_tool_names_getter = mcp_tool_names_getter

    definition = ToolDef(
        name="enter_workflow",
        description=(
            "Enter an enabled and approved workflow instance. This compiles the "
            "instance's tool scope and switches the conversation from workflow "
            "router mode into workflow run mode."
        ),
        args_model=EnterWorkflowArgs,
        base_risk=SecurityRisk.LOW,
        runs_in="in_process",
        read_only=True,
    )

    async def run(self, args: EnterWorkflowArgs, ctx: ToolContext) -> ToolOutcome:
        try:
            instance = self._store.get_instance(args.instance_id)
        except ValueError as exc:
            return _failure("workflow_invalid_id", str(exc))
        if instance is None:
            return _failure(
                "workflow_not_found",
                f"No workflow instance found for {args.instance_id!r}.",
            )
        if not instance.enabled:
            return _failure(
                "workflow_not_enabled",
                f"Workflow {args.instance_id!r} is not enabled.",
            )
        if instance.approval is None:
            return _failure(
                "workflow_not_approved",
                f"Workflow {args.instance_id!r} has not been approved.",
            )
        try:
            compiled = compile_workflow_scope(
                instance.definition, self._mcp_tool_names_getter()
            )
        except ValueError as exc:
            return _failure("workflow_scope_compile_failed", str(exc))

        self._phase_state.phase = WorkflowPhase.RUN
        self._phase_state.instance_id = args.instance_id
        self._phase_state.compiled_run_scope = compiled
        return ToolOutcome(
            success=True,
            content=(
                f"Entered workflow {args.instance_id!r}: "
                f"{instance.definition.name}. Compiled {len(compiled.allowed_tools)} tools."
            ),
            structured={
                "instance_id": args.instance_id,
                "phase": self._phase_state.phase.value,
                "definition_digest": instance.definition_digest,
                "allowed_tools": sorted(compiled.allowed_tools),
                "advertised_tools": sorted(compiled.advertised),
            },
        )


DraftParamType = Literal[
    "string",
    "integer",
    "number",
    "boolean",
    "string_array",
    "integer_array",
    "number_array",
]


class DraftWorkflowParam(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Parameter name.", min_length=1, max_length=80)
    type: DraftParamType = Field(description="JSON-schema parameter type.")
    required: bool = Field(default=True, description="Whether this parameter is required.")
    description: str | None = Field(default=None, description="Optional parameter help text.")


class DraftWorkflowMcpMount(BaseModel):
    model_config = ConfigDict(extra="forbid")

    server: str = Field(description="MCP server name, e.g. github.")
    tool_names: list[str] = Field(description="Explicit bare or qualified MCP tool names.")
    read_only: bool = Field(description="Whether all mounted tools are read-only.")


class DraftWorkflowArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Workflow definition name.", min_length=1, max_length=120)
    card: str = Field(description="One-paragraph workflow card.", min_length=1)
    params: list[DraftWorkflowParam] = Field(
        default_factory=list,
        description="Typed workflow input parameters.",
    )
    tools: list[str] = Field(
        default_factory=list,
        description="First-party tool names this workflow needs while running.",
    )
    mcp_mounts: list[DraftWorkflowMcpMount] = Field(
        default_factory=list,
        description="Explicit MCP tool mounts this workflow needs while running.",
    )
    skills: list[str] = Field(
        default_factory=list,
        description="Optional skill names the workflow expects.",
    )
    allows_writes: bool = Field(
        default=False,
        description="Whether the workflow may use write-capable tools or MCP mounts.",
    )
    untrusted_content: bool = Field(
        default=True,
        description="Whether workflow inputs or mounted content are untrusted.",
    )
    output_path_template: str = Field(
        description="Output path template, e.g. outputs/{query}.md.",
        min_length=1,
    )
    output_format: str = Field(description="Output format label.", min_length=1)
    verify_checks: list[str] = Field(
        default_factory=list,
        description="Verification checks for the workflow output.",
    )
    finalizer: str | None = Field(
        default=None,
        description="Optional workflow finalizer name.",
    )


class DraftWorkflowTool:
    definition = ToolDef(
        name="draft_workflow",
        description=(
            "Draft a WorkflowDefinition JSON document for human review. This does "
            "not persist or enable a workflow instance; approval/persistence is a "
            "separate host flow."
        ),
        args_model=DraftWorkflowArgs,
        base_risk=SecurityRisk.LOW,
        runs_in="in_process",
        read_only=True,
    )

    async def run(self, args: DraftWorkflowArgs, ctx: ToolContext) -> ToolOutcome:
        try:
            params_schema = _params_schema(args.params)
            mcp_mounts = tuple(
                McpMount(
                    server=mount.server,
                    tool_names=tuple(mount.tool_names),
                    read_only=mount.read_only,
                )
                for mount in args.mcp_mounts
            )
            defn = WorkflowDefinition(
                name=args.name,
                card=args.card,
                params_model_schema=params_schema,
                tools=tuple(args.tools),
                mcp_mounts=mcp_mounts,
                skills=tuple(args.skills),
                policies=WorkflowPolicies(
                    untrusted_content=args.untrusted_content,
                    allows_writes=args.allows_writes,
                ),
                output_contract=WorkflowOutputContract(
                    path_template=args.output_path_template,
                    format=args.output_format,
                ),
                verify=WorkflowVerify(
                    checks=tuple(args.verify_checks),
                    finalizer=args.finalizer,
                ),
            )
        except ValueError as exc:
            return _failure("workflow_draft_invalid", str(exc))

        draft = defn.model_dump(mode="json")
        return ToolOutcome(
            success=True,
            content="DRAFT WorkflowDefinition:\n" + json.dumps(draft, indent=2),
            structured={
                "status": "draft",
                "persisted": False,
                "definition_digest": defn.digest(),
                "definition": draft,
            },
        )


def workflow_router_tools(
    *,
    store: WorkflowStore,
    phase_state: WorkflowPhaseState,
    mcp_tool_names_getter: Callable[[], frozenset[str]],
) -> tuple[Tool, ...]:
    return (
        ListWorkflowsTool(store),
        ReadWorkflowCardTool(store),
        EnterWorkflowTool(store, phase_state, mcp_tool_names_getter),
        DraftWorkflowTool(),
    )


def _params_schema(params: list[DraftWorkflowParam]) -> dict[str, object]:
    seen: set[str] = set()
    properties: dict[str, object] = {}
    required: list[str] = []
    for param in params:
        if param.name in seen:
            raise ValueError(f"duplicate parameter name {param.name!r}")
        seen.add(param.name)
        schema = _param_schema(param.type)
        if param.description:
            schema = {**schema, "description": param.description}
        properties[param.name] = schema
        if param.required:
            required.append(param.name)
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required,
    }


def _param_schema(param_type: DraftParamType) -> dict[str, object]:
    scalar: dict[str, str] = {
        "string": "string",
        "integer": "integer",
        "number": "number",
        "boolean": "boolean",
    }
    if param_type in scalar:
        return {"type": scalar[param_type]}
    item_type = param_type.removesuffix("_array")
    return {"type": "array", "items": {"type": item_type}}


def _workflow_summary(row: StoredWorkflowInstance) -> dict[str, object]:
    instance = row.instance
    return {
        "instance_id": row.instance_id,
        "name": instance.definition.name,
        "card": instance.definition.card,
        "definition_digest": instance.definition_digest,
        "enabled": instance.enabled,
        "approved": instance.approval is not None,
    }


def _instance_payload(instance: WorkflowInstance) -> dict[str, object]:
    return {
        "name": instance.definition.name,
        "card": instance.definition.card,
        "definition_digest": instance.definition_digest,
        "params_model_schema": instance.definition.params_model_schema,
        "tools": list(instance.definition.tools),
        "mcp_mounts": [m.model_dump(mode="json") for m in instance.definition.mcp_mounts],
        "enabled": instance.enabled,
        "approval": (
            instance.approval.model_dump(mode="json")
            if instance.approval is not None
            else None
        ),
    }


def _failure(reason: str, message: str) -> ToolOutcome:
    return ToolOutcome(
        success=False,
        content=message,
        error=reason,
        structured={"reason": reason, "message": message},
    )


__all__ = [
    "DraftWorkflowArgs",
    "DraftWorkflowMcpMount",
    "DraftWorkflowParam",
    "DraftWorkflowTool",
    "EnterWorkflowArgs",
    "EnterWorkflowTool",
    "JsonDirWorkflowStore",
    "ListWorkflowsArgs",
    "ListWorkflowsTool",
    "ReadWorkflowCardArgs",
    "ReadWorkflowCardTool",
    "StoredWorkflowInstance",
    "WorkflowStore",
    "workflow_router_tools",
]
