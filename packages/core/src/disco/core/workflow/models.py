"""Workflow domain models and scope compiler.

Pure, serializable Pydantic value objects for saved workflow definitions and
instances. This module intentionally imports only pydantic and the stdlib: the
core package is the leaf, so runtime tool registries and MCP clients supply their
current names to the compiler instead of being imported here.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

_STRICT = ConfigDict(frozen=True, extra="forbid")

_ALGO = "sha256"
_SERVER_RE = re.compile(r"^[a-z0-9_]+$")
_DOUBLE_UNDER_RE = re.compile(r"__")
_MCP_PREFIX = "mcp__"

_NameStr = Annotated[str, StringConstraints(min_length=1, max_length=120)]
_CardStr = Annotated[str, StringConstraints(min_length=1, max_length=1200)]
_ToolStr = Annotated[str, StringConstraints(min_length=1, max_length=160)]
_SmallStr = Annotated[str, StringConstraints(min_length=1, max_length=240)]
_DigestStr = Annotated[str, StringConstraints(min_length=1, max_length=80)]


# Snapshot of the registered first-party tool names. Kept local rather than
# importing disco.tools so workflow definitions remain a pure core contract.
BUILTIN_WORKFLOW_TOOLS: frozenset[str] = frozenset(
    {
        "app_add_section",
        "app_create",
        "app_set_design",
        "app_set_tweak",
        "app_snapshot_version",
        "app_update_content",
        "audio_overview",
        "browser",
        "code_exec",
        "context_memory",
        "deck_patch",
        "delegate_explore",
        "design_lint",
        "doc_export",
        "doc_set_section",
        "exact_replace",
        "extract",
        "file_append",
        "file_edit",
        "file_insert_lines",
        "file_list",
        "file_read",
        "file_replace_lines",
        "file_str_replace",
        "file_write",
        "image_generate",
        "plan_step",
        "preview_logs",
        "preview_start",
        "preview_status",
        "preview_stop",
        "run_project_script",
        "safe_write_file",
        "scaffold_starter",
        "search",
        "server_status",
        "sheet_generate",
        "shell",
        "shell_exec",
        "shell_kill_process",
        "shell_view",
        "shell_wait",
        "shell_write_to_process",
        "slides_generate",
        "submit_plan",
        "think",
        "update_plan_progress",
        "verify_web_app",
    }
)

WORKFLOW_CONTROL_TOOLS: frozenset[str] = frozenset({"finish", "skip", "needs_input"})
_KNOWN_WORKFLOW_TOOLS = BUILTIN_WORKFLOW_TOOLS | WORKFLOW_CONTROL_TOOLS


@dataclass(frozen=True)
class _SchemaViolation:
    location: str
    kind: str
    detail: str


def _hash_text(text: str) -> str:
    return f"{_ALGO}:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _is_permissive_additional_properties(value: object) -> bool:
    return value is True or value == {} or value is None


def _schema_type(schema: dict[str, Any]) -> str | None:
    schema_type = schema.get("type")
    return schema_type if isinstance(schema_type, str) else None


def _walk_schema(node: object, *, path: str) -> list[_SchemaViolation]:
    violations: list[_SchemaViolation] = []
    if isinstance(node, dict):
        schema_type = _schema_type(node)
        if schema_type == "object":
            properties = node.get("properties")
            additional = node.get("additionalProperties", True)
            if not properties and _is_permissive_additional_properties(additional):
                violations.append(
                    _SchemaViolation(
                        location=path,
                        kind="bare-object",
                        detail="object has no properties and allows arbitrary keys",
                    )
                )
        elif schema_type == "array" and not node.get("items"):
            violations.append(
                _SchemaViolation(
                    location=path,
                    kind="untyped-array-items",
                    detail="array has no item schema",
                )
            )

        for key, value in node.items():
            if key in {"description", "title", "default", "examples"}:
                continue
            if key == "properties" and isinstance(value, dict):
                for prop_name, prop_schema in value.items():
                    violations.extend(_walk_schema(prop_schema, path=f"{path}.{prop_name}"))
                continue
            if key == "items":
                violations.extend(_walk_schema(value, path=f"{path}[]"))
                continue
            if isinstance(value, list):
                for index, item in enumerate(value):
                    violations.extend(_walk_schema(item, path=f"{path}.{key}[{index}]"))
            else:
                violations.extend(_walk_schema(value, path=f"{path}.{key}"))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            violations.extend(_walk_schema(item, path=f"{path}[{index}]"))
    return violations


def _validate_json_value(value: object, *, path: str) -> None:
    if value is None or isinstance(value, str | bool):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path}: non-finite floats are not JSON values")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, path=f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path}: JSON object keys must be strings")
            _validate_json_value(item, path=f"{path}.{key}")
        return
    raise ValueError(f"{path}: value is not JSON-serializable")


def _validate_params_schema(schema: dict[str, Any]) -> dict[str, Any]:
    _validate_json_value(schema, path="$")
    violations = _walk_schema(schema, path="$")
    if violations:
        detail = "; ".join(
            f"{v.location}: {v.kind} ({v.detail})" for v in violations
        )
        raise ValueError(f"params_model_schema has schema holes: {detail}")
    return schema


def _split_mcp_qualified(name: str) -> tuple[str, str] | None:
    if not name.startswith(_MCP_PREFIX):
        return None
    rest = name[len(_MCP_PREFIX) :]
    parts = rest.split("__", 1)
    if len(parts) != 2:
        return None
    server, tool = parts
    if not server or not tool:
        return None
    if _DOUBLE_UNDER_RE.search(server) or _DOUBLE_UNDER_RE.search(tool):
        return None
    return server, tool


def _qualified_mcp_name(server: str, tool: str) -> str:
    return f"{_MCP_PREFIX}{server}__{tool}"


def _validate_mcp_component(value: str, *, field: str) -> str:
    if not value:
        raise ValueError(f"{field} must be non-empty")
    if _DOUBLE_UNDER_RE.search(value):
        raise ValueError(f"{field} must not contain double underscores")
    return value


def _dedupe(values: tuple[str, ...], *, field: str) -> tuple[str, ...]:
    if len(set(values)) != len(values):
        raise ValueError(f"{field} must not contain duplicates")
    return values


class McpMount(BaseModel):
    """Explicit workflow mount for a single MCP server.

    ``tool_names`` stores bare server-local tool names. Qualified names are
    accepted as input only when their server prefix matches, then normalized to
    bare names so the mount digest is stable.
    """

    model_config = _STRICT

    server: _NameStr
    tool_names: tuple[_ToolStr, ...] = Field(min_length=1)
    read_only: bool

    @field_validator("server")
    @classmethod
    def _server_name(cls, value: str) -> str:
        if not _SERVER_RE.match(value):
            raise ValueError("server must match [a-z0-9_]+")
        return _validate_mcp_component(value, field="server")

    @model_validator(mode="before")
    @classmethod
    def _normalize_explicit_tool_names(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        raw_server = data.get("server")
        raw_tool_names = data.get("tool_names")
        if not isinstance(raw_server, str) or not isinstance(raw_tool_names, list | tuple):
            return data

        normalized: list[str] = []
        for raw in raw_tool_names:
            if not isinstance(raw, str):
                return data
            if raw in {"*", "all", "catalog"}:
                raise ValueError("tool_names must list explicit tools, not a catalog")
            parts = _split_mcp_qualified(raw)
            if raw.startswith(_MCP_PREFIX):
                if parts is None:
                    raise ValueError(f"invalid MCP-qualified tool name {raw!r}")
                server, tool = parts
                if server != raw_server:
                    raise ValueError(
                        f"MCP tool {raw!r} does not match mount server {raw_server!r}"
                    )
                normalized.append(tool)
            else:
                normalized.append(_validate_mcp_component(raw, field="tool name"))
        normalized_tuple = tuple(normalized)
        _dedupe(normalized_tuple, field="tool_names")
        return {**data, "tool_names": normalized_tuple}


class WorkflowPolicies(BaseModel):
    model_config = _STRICT

    untrusted_content: bool = True
    allows_writes: bool = False


class WorkflowOutputContract(BaseModel):
    model_config = _STRICT

    path_template: _SmallStr
    format: _SmallStr


class WorkflowVerify(BaseModel):
    model_config = _STRICT

    checks: tuple[_SmallStr, ...] = ()
    finalizer: _SmallStr | None = None

    @field_validator("checks")
    @classmethod
    def _checks_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _dedupe(value, field="checks")


class WorkflowDefinition(BaseModel):
    model_config = _STRICT

    name: _NameStr
    card: _CardStr
    params_model_schema: dict[str, Any]
    tools: tuple[_ToolStr, ...] = ()
    mcp_mounts: tuple[McpMount, ...] = ()
    skills: tuple[_SmallStr, ...] = ()
    policies: WorkflowPolicies = Field(default_factory=WorkflowPolicies)
    output_contract: WorkflowOutputContract
    verify: WorkflowVerify

    @field_validator("card")
    @classmethod
    def _card_one_paragraph(cls, value: str) -> str:
        if "\n\n" in value:
            raise ValueError("card must be a one-paragraph description")
        return value

    @field_validator("params_model_schema")
    @classmethod
    def _params_schema_typed(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _validate_params_schema(value)

    @field_validator("tools", "skills")
    @classmethod
    def _tuple_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _dedupe(value, field="tuple field")

    @model_validator(mode="after")
    def _write_policy_covers_mcp_mounts(self) -> WorkflowDefinition:
        writable_mounts = [m.server for m in self.mcp_mounts if not m.read_only]
        if writable_mounts and not self.policies.allows_writes:
            servers = ", ".join(sorted(writable_mounts))
            raise ValueError(
                "writable MCP mounts require policies.allows_writes=True "
                f"(servers: {servers})"
            )
        return self

    def digest(self) -> str:
        """Stable definition identity over canonical JSON."""
        return _hash_text(_canonical_json(self.model_dump(mode="json")))


class WorkflowApproval(BaseModel):
    model_config = _STRICT

    approved_at: _SmallStr
    approved_by: _SmallStr
    surface_shown_digest: _DigestStr


class WorkflowInstance(BaseModel):
    model_config = _STRICT

    definition_digest: _DigestStr
    definition: WorkflowDefinition
    params: dict[str, Any]
    connector_bindings: dict[str, str] = Field(default_factory=dict)
    enabled: bool = False
    approval: WorkflowApproval | None = None

    @field_validator("params")
    @classmethod
    def _params_are_json(cls, value: dict[str, Any]) -> dict[str, Any]:
        _validate_json_value(value, path="params")
        return value

    @model_validator(mode="after")
    def _definition_digest_matches(self) -> WorkflowInstance:
        actual = self.definition.digest()
        if self.definition_digest != actual:
            raise ValueError(
                f"definition_digest {self.definition_digest!r} does not match embedded "
                f"definition digest {actual!r}"
            )
        if self.approval is not None and self.approval.surface_shown_digest != actual:
            raise ValueError("approval.surface_shown_digest must match the definition digest")
        return self


class WorkflowScope(BaseModel):
    model_config = _STRICT

    allowed_tools: frozenset[str]
    advertised: frozenset[str]


def compile_workflow_scope(
    defn: WorkflowDefinition, mcp_tool_names_available: frozenset[str]
) -> WorkflowScope:
    """Compile a workflow definition into its hard tool scope.

    Compilation is validation: unknown first-party tool names and unavailable
    mounted MCP tools fail closed instead of being silently dropped.
    """
    unknown_builtin = sorted(name for name in defn.tools if name not in _KNOWN_WORKFLOW_TOOLS)
    if unknown_builtin:
        raise ValueError(f"unknown workflow builtin tool(s): {', '.join(unknown_builtin)}")

    mcp_tools: set[str] = set()
    missing_mcp: list[str] = []
    for mount in defn.mcp_mounts:
        for tool in mount.tool_names:
            qualified = _qualified_mcp_name(mount.server, tool)
            if not qualified.startswith(f"{_MCP_PREFIX}{mount.server}__"):
                raise ValueError(
                    f"MCP tool {qualified!r} does not match server prefix {mount.server!r}"
                )
            if qualified not in mcp_tool_names_available:
                missing_mcp.append(qualified)
                continue
            mcp_tools.add(qualified)

    if missing_mcp:
        raise ValueError(f"missing mounted MCP tool(s): {', '.join(sorted(missing_mcp))}")

    allowed = frozenset(defn.tools) | frozenset(mcp_tools) | WORKFLOW_CONTROL_TOOLS
    return WorkflowScope(allowed_tools=allowed, advertised=allowed)


__all__ = [
    "BUILTIN_WORKFLOW_TOOLS",
    "McpMount",
    "WORKFLOW_CONTROL_TOOLS",
    "WorkflowApproval",
    "WorkflowDefinition",
    "WorkflowInstance",
    "WorkflowOutputContract",
    "WorkflowPolicies",
    "WorkflowScope",
    "WorkflowVerify",
    "compile_workflow_scope",
]
