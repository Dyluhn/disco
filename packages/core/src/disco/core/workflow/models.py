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
import string
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from typing import Literal

from cronsim import CronSim, CronSimError

from ..owners import install_owner_id
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
_HOST_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

_NameStr = Annotated[str, StringConstraints(min_length=1, max_length=120)]
_CardStr = Annotated[str, StringConstraints(min_length=1, max_length=1200)]
_ToolStr = Annotated[str, StringConstraints(min_length=1, max_length=160)]
_SmallStr = Annotated[str, StringConstraints(min_length=1, max_length=240)]
_DigestStr = Annotated[str, StringConstraints(min_length=1, max_length=80)]
_FindingCodeStr = Annotated[str, StringConstraints(min_length=1, max_length=120)]
_FindingPathStr = Annotated[str, StringConstraints(min_length=1, max_length=300)]
_FindingMessageStr = Annotated[str, StringConstraints(min_length=1, max_length=1000)]


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
        detail = "; ".join(f"{v.location}: {v.kind} ({v.detail})" for v in violations)
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


def _normalize_egress_allow_entry(value: str) -> str:
    if not value:
        raise ValueError("egress_allow entries must be non-empty")
    if any(ch.isspace() for ch in value):
        raise ValueError("egress_allow entries must not contain spaces")
    if "://" in value:
        raise ValueError("egress_allow entries must not include scheme://")
    if "/" in value:
        raise ValueError("egress_allow entries must not include /path")

    normalized = value.lower()
    if normalized.startswith("*."):
        normalized = f".{normalized[2:]}"
    suffix = normalized.startswith(".")
    host = normalized[1:] if suffix else normalized
    if not host or host.startswith(".") or host.endswith("."):
        raise ValueError("egress_allow entries must be bare hostnames or *.suffix patterns")
    labels = host.split(".")
    if not all(_HOST_LABEL_RE.match(label) for label in labels):
        raise ValueError("egress_allow entries must be bare hostnames or *.suffix patterns")
    return normalized


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
                    raise ValueError(f"MCP tool {raw!r} does not match mount server {raw_server!r}")
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
    egress_allow: tuple[str, ...] = ()

    @field_validator("egress_allow")
    @classmethod
    def _egress_allow_entries(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_normalize_egress_allow_entry(entry) for entry in value)
        return _dedupe(normalized, field="egress_allow")


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


class WorkflowValidationFinding(BaseModel):
    model_config = _STRICT

    severity: Literal["error", "warning"]
    code: _FindingCodeStr
    path: _FindingPathStr
    message: _FindingMessageStr


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
                f"writable MCP mounts require policies.allows_writes=True (servers: {servers})"
            )
        return self

    def digest(self) -> str:
        """Stable definition identity over canonical JSON."""
        return _hash_text(_canonical_json(self.model_dump(mode="json")))


def workflow_finish_supports_verify(defn: WorkflowDefinition) -> bool:
    """Whether a sealed workflow may execute a model-authored finish probe.

    ``finish.verify`` is implemented by running the supplied command through the
    first-party ``shell`` tool.  A workflow that did not explicitly seal
    ``shell`` into its approved tool set must therefore neither advertise nor
    execute that parameter.
    """

    return "shell" in defn.tools


def workflow_finish_tool_description(defn: WorkflowDefinition) -> str:
    base = (
        "Declare the workflow COMPLETE and end the run. Call this only after the "
        "sealed output contract has been satisfied. Provide a short summary."
    )
    if workflow_finish_supports_verify(defn):
        return (
            f"{base} You may optionally provide a shell `verify` command; a failed "
            "verification refuses completion until fixed or capped."
        )
    return (
        f"{base} Completion is checked by the workflow's host-owned output-contract "
        "gates; this workflow does not grant shell verification."
    )


def workflow_finish_tool_schema(defn: WorkflowDefinition) -> dict[str, object]:
    properties: dict[str, object] = {
        "summary": {
            "type": "string",
            "description": "Short summary of what was accomplished.",
        }
    }
    if workflow_finish_supports_verify(defn):
        properties["verify"] = {
            "type": "string",
            "description": "Optional shell completion check; exit 0 means success.",
        }
    return {"type": "object", "properties": properties, "required": []}


class WorkflowApproval(BaseModel):
    model_config = _STRICT

    approved_at: _SmallStr
    approved_by: _SmallStr
    surface_shown_digest: _DigestStr


class WorkflowInstance(BaseModel):
    model_config = _STRICT

    owner_id: str = Field(default_factory=install_owner_id)
    definition_digest: _DigestStr
    definition: WorkflowDefinition
    params: dict[str, Any]
    connector_bindings: dict[str, str] = Field(default_factory=dict)
    enabled: bool = False
    approval: WorkflowApproval | None = None
    validation_findings: tuple[WorkflowValidationFinding, ...] = ()

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
        return self


class WorkflowRun(BaseModel):
    """A concrete execution of a workflow definition.

    The runtime can carry this small value into the agent loop so finish gates can
    enforce definition-level obligations without importing the tools/runtime layer.
    """

    model_config = _STRICT

    run_id: _NameStr
    definition: WorkflowDefinition
    params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("params")
    @classmethod
    def _params_are_json(cls, value: dict[str, Any]) -> dict[str, Any]:
        _validate_json_value(value, path="params")
        return value


class ScheduleSpec(BaseModel):
    """A sealed recurring workflow schedule.

    ``instance_digest`` pins the schedule to the approved workflow instance digest
    that was shown when the schedule was created. The server must refuse to run if
    the saved instance id now resolves to a different digest.
    """

    model_config = _STRICT

    instance_id: _SmallStr
    instance_digest: _DigestStr
    cron: _SmallStr
    enabled: bool = True

    @field_validator("cron")
    @classmethod
    def _cron_is_valid(cls, value: str) -> str:
        try:
            CronSim(value, datetime(2020, 1, 1, tzinfo=UTC))
        except (CronSimError, ValueError, TypeError) as exc:
            raise ValueError(f"invalid cron expression: {value!r}") from exc
        return value


class WorkflowScope(BaseModel):
    model_config = _STRICT

    allowed_tools: frozenset[str]
    advertised: frozenset[str]


class WorkflowSimulationResult(BaseModel):
    model_config = _STRICT

    ok: bool
    output_path: _SmallStr | None = None
    output_format: _SmallStr | None = None
    fixture_bytes: int = 0
    findings: tuple[WorkflowValidationFinding, ...] = ()


def _finding(
    severity: Literal["error", "warning"],
    code: str,
    path: str,
    message: str,
) -> WorkflowValidationFinding:
    return WorkflowValidationFinding(
        severity=severity,
        code=code,
        path=path,
        message=message,
    )


def _schema_findings(schema: object, *, path: str) -> list[WorkflowValidationFinding]:
    findings: list[WorkflowValidationFinding] = []
    if not isinstance(schema, dict):
        return [
            _finding(
                "error",
                "params_schema_invalid",
                path,
                "params_model_schema must be a JSON schema object",
            )
        ]
    try:
        _validate_json_value(schema, path=path)
    except ValueError as exc:
        findings.append(_finding("error", "params_schema_invalid_json", path, str(exc)))
    for violation in _walk_schema(schema, path=path):
        findings.append(
            _finding(
                "error",
                "params_schema_gate",
                violation.location,
                f"{violation.kind}: {violation.detail}",
            )
        )
    return findings


def _compile_findings(
    defn: WorkflowDefinition,
    *,
    available_builtin_names: frozenset[str],
    available_mcp_names: frozenset[str],
) -> list[WorkflowValidationFinding]:
    findings: list[WorkflowValidationFinding] = []
    unknown_builtin = sorted(name for name in defn.tools if name not in available_builtin_names)
    for name in unknown_builtin:
        findings.append(
            _finding(
                "error",
                "unknown_builtin_tool",
                "tools",
                f"unknown workflow builtin tool: {name}",
            )
        )

    missing_mcp: list[str] = []
    for mount in defn.mcp_mounts:
        for tool in mount.tool_names:
            qualified = _qualified_mcp_name(mount.server, tool)
            if not qualified.startswith(f"{_MCP_PREFIX}{mount.server}__"):
                findings.append(
                    _finding(
                        "error",
                        "mcp_server_mismatch",
                        "mcp_mounts",
                        (f"MCP tool {qualified!r} does not match server prefix {mount.server!r}"),
                    )
                )
            elif qualified not in available_mcp_names:
                missing_mcp.append(qualified)
    for qualified in sorted(missing_mcp):
        findings.append(
            _finding(
                "error",
                "missing_mcp_tool",
                "mcp_mounts",
                f"missing mounted MCP tool: {qualified}",
            )
        )
    return findings


def _policy_findings(defn: WorkflowDefinition) -> list[WorkflowValidationFinding]:
    findings: list[WorkflowValidationFinding] = []
    if "browser" in defn.tools and not defn.policies.egress_allow:
        findings.append(
            _finding(
                "warning",
                "browser_without_egress",
                "policies.egress_allow",
                "workflow uses browser but policies.egress_allow is empty; sealed runs cannot reach the network",
            )
        )
    writable_mounts = sorted(m.server for m in defn.mcp_mounts if not m.read_only)
    if writable_mounts and not defn.policies.allows_writes:
        servers = ", ".join(writable_mounts)
        findings.append(
            _finding(
                "error",
                "write_policy_inconsistent",
                "policies.allows_writes",
                (f"writable MCP mounts require policies.allows_writes=True (servers: {servers})"),
            )
        )
    return findings


def _skill_findings(
    defn: WorkflowDefinition, *, available_skill_names: frozenset[str]
) -> list[WorkflowValidationFinding]:
    unknown = sorted(name for name in defn.skills if name not in available_skill_names)
    return [
        _finding(
            "error",
            "unknown_skill",
            "skills",
            f"unknown workflow skill: {name}",
        )
        for name in unknown
    ]


def validate_definition(
    defn: WorkflowDefinition,
    available_builtin_names: frozenset[str],
    available_mcp_names: frozenset[str],
    available_skill_names: frozenset[str] = frozenset(),
) -> list[WorkflowValidationFinding]:
    """Collect validation findings for a workflow definition.

    This is the pre-approval collector. It mirrors the checks that make
    ``compile_workflow_scope`` fail closed, but it reports every issue it can
    observe instead of raising at the first failing category.
    """

    findings: list[WorkflowValidationFinding] = []
    findings.extend(_schema_findings(defn.params_model_schema, path="params_model_schema"))
    findings.extend(
        _compile_findings(
            defn,
            available_builtin_names=available_builtin_names,
            available_mcp_names=available_mcp_names,
        )
    )
    findings.extend(_policy_findings(defn))
    findings.extend(_skill_findings(defn, available_skill_names=available_skill_names))
    return findings


def _stringify_path_param(value: object, *, key: str) -> str:
    if value is None or isinstance(value, list | dict):
        raise ValueError(f"output path parameter {key!r} must be a scalar JSON value")
    text = str(value).strip()
    if not text:
        raise ValueError(f"output path parameter {key!r} resolved to an empty string")
    return text


def _referenced_template_fields(template: str) -> set[str]:
    return {
        field.split(".")[0].split("[")[0]
        for _, field, _, _ in string.Formatter().parse(template)
        if field
    }


def render_workflow_output_path(template: str, params: dict[str, Any]) -> str:
    referenced = _referenced_template_fields(template)
    try:
        rendered = template.format(
            **{
                key: _stringify_path_param(value, key=key)
                for key, value in params.items()
                if key in referenced
            }
        )
    except KeyError as exc:
        missing = exc.args[0]
        raise ValueError(f"missing output path parameter: {missing!r}") from exc
    except (IndexError, ValueError) as exc:
        raise ValueError(f"invalid output path template: {exc}") from exc

    if not rendered.strip():
        raise ValueError("output path template resolved to an empty path")
    if "\x00" in rendered:
        raise ValueError("output path must not contain NUL bytes")
    path = Path(rendered)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("output path must be a relative path without dot segments")
    return path.as_posix()


def simulate_definition(
    defn: WorkflowDefinition,
    *,
    params: dict[str, Any],
    workspace_root: str | Path,
    available_builtin_names: frozenset[str],
    available_mcp_names: frozenset[str],
) -> WorkflowSimulationResult:
    """Dry-run a workflow definition without invoking a model.

    The seam compiles the scope, renders the declared output path, writes a small
    fixture file, and verifies that the file exists. It is a smoke test for the
    pre-approval contract, not a proof that a live agent can complete the work.
    """

    findings: list[WorkflowValidationFinding] = []
    findings.extend(
        _compile_findings(
            defn,
            available_builtin_names=available_builtin_names,
            available_mcp_names=available_mcp_names,
        )
    )
    try:
        compile_workflow_scope(defn, available_mcp_names)
    except ValueError as exc:
        findings.append(
            _finding(
                "error",
                "simulation_scope_compile_failed",
                "definition",
                str(exc),
            )
        )

    try:
        output_path = render_workflow_output_path(defn.output_contract.path_template, params)
    except ValueError as exc:
        findings.append(
            _finding(
                "error",
                "simulation_output_contract_failed",
                "output_contract.path_template",
                str(exc),
            )
        )
        return WorkflowSimulationResult(ok=False, findings=tuple(findings))

    root = Path(workspace_root)
    target = (root / output_path).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        findings.append(
            _finding(
                "error",
                "simulation_output_contract_failed",
                "output_contract.path_template",
                "rendered output path escaped the workspace root",
            )
        )
        return WorkflowSimulationResult(
            ok=False,
            output_path=output_path,
            output_format=defn.output_contract.format,
            findings=tuple(findings),
        )

    fixture = (
        f"# Workflow simulation fixture\n\n"
        f"format: {defn.output_contract.format}\n"
        f"path: {output_path}\n"
    )
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(fixture, encoding="utf-8")
        fixture_bytes = target.stat().st_size
    except OSError as exc:
        findings.append(
            _finding(
                "error",
                "simulation_fixture_write_failed",
                "output_contract.path_template",
                str(exc),
            )
        )
        fixture_bytes = 0

    return WorkflowSimulationResult(
        ok=not any(f.severity == "error" for f in findings),
        output_path=output_path,
        output_format=defn.output_contract.format,
        fixture_bytes=fixture_bytes,
        findings=tuple(findings),
    )


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
    "ScheduleSpec",
    "WORKFLOW_CONTROL_TOOLS",
    "WorkflowApproval",
    "WorkflowDefinition",
    "WorkflowInstance",
    "WorkflowRun",
    "WorkflowOutputContract",
    "WorkflowPolicies",
    "WorkflowSimulationResult",
    "WorkflowScope",
    "WorkflowValidationFinding",
    "WorkflowVerify",
    "compile_workflow_scope",
    "render_workflow_output_path",
    "simulate_definition",
    "validate_definition",
]
