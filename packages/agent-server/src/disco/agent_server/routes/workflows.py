"""Workflow draft/review/approval routes."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from disco.core import DEFAULT_OWNER_ID, SkillStore
from disco.core.store.sqlite import SqliteEventStore
from disco.core.workflow import (
    WORKFLOW_CONTROL_TOOLS,
    WorkflowApproval,
    WorkflowDefinition,
    WorkflowInstance,
    WorkflowValidationFinding,
    compile_workflow_scope,
    simulate_definition,
    validate_definition,
)
from disco.tools import ToolDef, ToolScope, build_default_registry
from disco.tools.builtin.workflow_tools import JsonDirWorkflowStore
from disco.tools.projects import StorageStatus
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from ..runtime import ConversationRuntime

_SAFE_ID_FRAGMENT = re.compile(r"[^A-Za-z0-9_.-]+")

_FINISH_DESCRIPTION = (
    "Declare the task COMPLETE and end the run. Call this ONLY when every plan "
    "step is done and verified. Provide a short summary and optionally a verify "
    "command; failed verification refuses the finish until fixed or capped."
)
_FINISH_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "Short summary of what was accomplished.",
        },
        "verify": {
            "type": "string",
            "description": "Optional completion check; exit 0 means success.",
        },
    },
    "required": [],
}


class DraftWorkflowBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    definition: WorkflowDefinition
    params: dict[str, Any] = Field(default_factory=dict)
    instance_id: str | None = Field(default=None, max_length=120)


class ApproveWorkflowBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved_by: str = Field(default=DEFAULT_OWNER_ID, min_length=1, max_length=240)
    surface_shown_digest: str | None = Field(default=None, min_length=1, max_length=80)


@dataclass(frozen=True)
class _SurfaceEnvironment:
    builtin_defs: dict[str, ToolDef]
    builtin_names: frozenset[str]
    mcp_defs: dict[str, ToolDef]
    mcp_names: frozenset[str]
    skill_names: frozenset[str]
    skill_payloads: dict[str, dict[str, object]]


def make_workflows_router(
    store: SqliteEventStore, runtime: ConversationRuntime | None  # noqa: ARG001
) -> APIRouter:
    router = APIRouter()

    async def _list_workflows() -> dict:
        if runtime is None:
            return {"workflows": [], "status": "no_runtime"}
        workflow_store = _workflow_store(runtime)
        env = _surface_environment(runtime)
        workflows = [
            _workflow_review_payload(row.instance_id, row.instance, env)
            for row in workflow_store.list_instances()
        ]
        return {"workflows": workflows, "status": "ok"}

    async def _draft_workflow(body: DraftWorkflowBody) -> dict:
        if runtime is None:
            raise HTTPException(status_code=503, detail={"reason": "no_runtime"})
        workflow_store = _workflow_store(runtime)
        env = _surface_environment(runtime)
        instance_id = body.instance_id or _generated_instance_id(body.definition)
        findings = validate_definition(
            body.definition,
            env.builtin_names,
            env.mcp_names,
            env.skill_names,
        )
        with tempfile.TemporaryDirectory(prefix="disco-workflow-sim-") as tmp:
            simulation = simulate_definition(
                body.definition,
                params=body.params,
                workspace_root=tmp,
                available_builtin_names=env.builtin_names,
                available_mcp_names=env.mcp_names,
            )
        all_findings = _dedupe_findings([*findings, *simulation.findings])
        instance = WorkflowInstance(
            definition_digest=body.definition.digest(),
            definition=body.definition,
            params=body.params,
            enabled=False,
            approval=None,
            validation_findings=tuple(all_findings),
        )
        workflow_store.save_instance(instance_id, instance)
        return {
            "workflow": _workflow_review_payload(instance_id, instance, env),
            "simulation": simulation.model_dump(mode="json"),
        }

    async def _approve_workflow(instance_id: str, body: ApproveWorkflowBody) -> dict:
        if runtime is None:
            raise HTTPException(status_code=503, detail={"reason": "no_runtime"})
        workflow_store = _workflow_store(runtime)
        try:
            instance = workflow_store.get_instance(instance_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={"reason": "workflow_invalid_id", "message": str(exc)},
            ) from exc
        if instance is None:
            raise HTTPException(
                status_code=404,
                detail={"reason": "workflow_not_found"},
            )

        env = _surface_environment(runtime)
        review = _workflow_review_payload(instance_id, instance, env)
        surface_digest = str(review["surface_shown_digest"])
        if body.surface_shown_digest is not None and body.surface_shown_digest != surface_digest:
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": "workflow_surface_digest_mismatch",
                    "expected": surface_digest,
                    "received": body.surface_shown_digest,
                },
            )

        findings = [
            WorkflowValidationFinding.model_validate(item)
            for item in cast(list[dict[str, object]], review["validation_findings"])
        ]
        if any(f.severity == "error" for f in findings):
            refreshed = instance.model_copy(update={"validation_findings": tuple(findings)})
            workflow_store.save_instance(instance_id, refreshed)
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": "workflow_validation_failed",
                    "findings": [f.model_dump(mode="json") for f in findings],
                },
            )

        approved = instance.model_copy(
            update={
                "enabled": True,
                "approval": WorkflowApproval(
                    approved_at=_now_iso(),
                    approved_by=body.approved_by,
                    surface_shown_digest=surface_digest,
                ),
                "validation_findings": tuple(findings),
            }
        )
        workflow_store.save_instance(instance_id, approved)
        return {"workflow": _workflow_review_payload(instance_id, approved, env)}

    router.add_api_route("/api/workflows", _list_workflows, methods=["GET"])
    router.add_api_route("/workflows", _list_workflows, methods=["GET"])
    router.add_api_route("/api/workflows/draft", _draft_workflow, methods=["POST"])
    router.add_api_route("/workflows/draft", _draft_workflow, methods=["POST"])
    router.add_api_route(
        "/api/workflows/{instance_id}/approve",
        _approve_workflow,
        methods=["POST"],
    )
    router.add_api_route(
        "/workflows/{instance_id}/approve",
        _approve_workflow,
        methods=["POST"],
    )
    return router


def _workflow_store(runtime: ConversationRuntime) -> JsonDirWorkflowStore:
    project_store = runtime.project_store()
    if project_store.status() != StorageStatus.OK:
        raise HTTPException(
            status_code=409,
            detail={"reason": "project_storage_unavailable", "status": project_store.status().value},
        )
    root = project_store.root
    if root is None:
        raise HTTPException(
            status_code=409,
            detail={"reason": "project_storage_unavailable", "status": "unset"},
        )
    return JsonDirWorkflowStore(root)


def _surface_environment(runtime: ConversationRuntime) -> _SurfaceEnvironment:
    registry = build_default_registry()
    builtin_tools = registry.in_scope(ToolScope(allowed_tools=registry.names()))
    builtin_defs = {tool.definition.name: tool.definition for tool in builtin_tools}
    builtin_names = frozenset(builtin_defs) | WORKFLOW_CONTROL_TOOLS

    mcp_defs: dict[str, ToolDef] = {}
    pool = getattr(runtime, "_mcp_pool", None)
    if pool is not None:
        for tool_def in pool.snapshot():
            mcp_defs[tool_def.name] = tool_def
    for name, tool_def in getattr(runtime, "_mcp_http_tools", {}).items():
        if isinstance(name, str) and isinstance(tool_def, ToolDef):
            mcp_defs[name] = tool_def

    skill_store = cast(SkillStore, getattr(runtime, "_skill_store", SkillStore()))
    skills = skill_store.list()
    skill_payloads: dict[str, dict[str, object]] = {}
    for skill in skills:
        payload: dict[str, object] = {
            "id": skill.id,
            "name": skill.name,
            "description": skill.description,
            "enabled": skill.enabled,
            "surfaces": list(skill.surfaces),
        }
        skill_payloads[skill.id] = payload
        skill_payloads[skill.name] = payload

    return _SurfaceEnvironment(
        builtin_defs=builtin_defs,
        builtin_names=builtin_names,
        mcp_defs=mcp_defs,
        mcp_names=frozenset(mcp_defs),
        skill_names=frozenset(skill_payloads),
        skill_payloads=skill_payloads,
    )


def _workflow_review_payload(
    instance_id: str,
    instance: WorkflowInstance,
    env: _SurfaceEnvironment,
) -> dict[str, object]:
    findings = validate_definition(
        instance.definition,
        env.builtin_names,
        env.mcp_names,
        env.skill_names,
    )
    if instance.validation_findings:
        findings = _dedupe_findings([*instance.validation_findings, *findings])

    compiled_surface = _compiled_surface_payload(instance, env)
    surface_digest = _digest_payload(compiled_surface)
    return {
        "instance_id": instance_id,
        "name": instance.definition.name,
        "card": instance.definition.card,
        "definition_digest": instance.definition_digest,
        "enabled": instance.enabled,
        "approved": instance.approval is not None,
        "approval": (
            instance.approval.model_dump(mode="json")
            if instance.approval is not None
            else None
        ),
        "params": instance.params,
        "definition": instance.definition.model_dump(mode="json"),
        "validation_findings": [f.model_dump(mode="json") for f in findings],
        "compiled_surface": compiled_surface,
        "surface_shown_digest": surface_digest,
    }


def _compiled_surface_payload(
    instance: WorkflowInstance,
    env: _SurfaceEnvironment,
) -> dict[str, object]:
    compiled_ok = True
    compile_error: str | None = None
    try:
        compiled = compile_workflow_scope(instance.definition, env.mcp_names)
        allowed = sorted(compiled.allowed_tools)
        advertised = sorted(compiled.advertised)
    except ValueError as exc:
        compiled_ok = False
        compile_error = str(exc)
        allowed = []
        advertised = []

    return {
        "compiled": compiled_ok,
        "compile_error": compile_error,
        "allowed_tools": allowed,
        "advertised_tools": advertised,
        "tool_definitions": [_surface_tool_payload(name, env) for name in allowed],
        "mcp_mounts": [
            {
                **mount.model_dump(mode="json"),
                "qualified_tool_names": [
                    f"mcp__{mount.server}__{tool}" for tool in mount.tool_names
                ],
            }
            for mount in instance.definition.mcp_mounts
        ],
        "skills": [
            {
                "requested": name,
                "available": name in env.skill_payloads,
                "skill": env.skill_payloads.get(name),
            }
            for name in instance.definition.skills
        ],
        "policies": instance.definition.policies.model_dump(mode="json"),
        "params_model_schema": instance.definition.params_model_schema,
        "output_contract": instance.definition.output_contract.model_dump(mode="json"),
        "verify": instance.definition.verify.model_dump(mode="json"),
    }


def _surface_tool_payload(name: str, env: _SurfaceEnvironment) -> dict[str, object]:
    if name == "finish":
        return {
            "name": "finish",
            "description": _FINISH_DESCRIPTION,
            "parameters_schema": _FINISH_SCHEMA,
            "read_only": True,
            "runs_in": "in_process",
            "base_risk": "LOW",
            "needs": [],
            "source": "workflow_control",
        }

    tool_def = env.builtin_defs.get(name)
    source = "builtin"
    if tool_def is None:
        tool_def = env.mcp_defs.get(name)
        source = "mcp"
    if tool_def is None:
        return {"name": name, "available": False, "source": "missing"}

    spec = tool_def.to_spec()
    return {
        "name": tool_def.name,
        "description": tool_def.description,
        "parameters_schema": spec.parameters_schema,
        "read_only": tool_def.read_only,
        "runs_in": tool_def.runs_in,
        "base_risk": tool_def.base_risk.value if tool_def.base_risk is not None else None,
        "needs": sorted(capability.value for capability in tool_def.needs),
        "source": source,
    }


def _generated_instance_id(defn: WorkflowDefinition) -> str:
    slug = _SAFE_ID_FRAGMENT.sub("-", defn.name.strip()).strip("-._").lower()
    slug = slug or "workflow"
    digest = defn.digest().split(":", 1)[-1][:12]
    return f"wf_{slug[:60]}_{digest}"


def _digest_payload(payload: object) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _dedupe_findings(
    findings: list[WorkflowValidationFinding],
) -> list[WorkflowValidationFinding]:
    seen: set[tuple[str, str, str, str]] = set()
    deduped: list[WorkflowValidationFinding] = []
    for finding in findings:
        key = (finding.severity, finding.code, finding.path, finding.message)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(finding)
    return deduped


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


__all__ = ["make_workflows_router"]
