"""Pre-approval validation-finding collectors for workflow definitions.

Extracted from :mod:`disco.core.workflow.models`. ``WorkflowDefinition`` is only
ever read here via duck-typed attribute access, so it is imported ONLY under
``TYPE_CHECKING``. ``WorkflowValidationFinding`` instances ARE constructed at
runtime (in ``_finding``); rather than import that class directly (which would
be a real import cycle, since ``models.py`` imports ``validate_definition`` from
this module), this module imports the parent ``models`` module itself and
constructs findings through ``models.WorkflowValidationFinding(...)`` — a
module-attribute lookup resolved at call time, once both modules have finished
loading. This is the same parent/parts convention used elsewhere in the
campaign (see e.g. ``disco.retrieval.deep_research._engine_parts``), and it has
the same side benefit: a future monkeypatch of that name on
``disco.core.workflow.models`` would still be honored here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from .. import models
from .mcp_names import _MCP_PREFIX, _qualified_mcp_name
from .schema_walk import _validate_json_value, _walk_schema

if TYPE_CHECKING:
    from ..models import WorkflowDefinition, WorkflowValidationFinding


def _finding(
    severity: Literal["error", "warning"],
    code: str,
    path: str,
    message: str,
) -> WorkflowValidationFinding:
    return models.WorkflowValidationFinding(
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
                "error",
                "browser_without_egress",
                "policies.egress_allow",
                "workflow uses browser but policies.egress_allow is empty; "
                "sealed runs cannot reach the network",
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
