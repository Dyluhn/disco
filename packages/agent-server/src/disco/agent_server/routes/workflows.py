"""Workflow draft/review/approval routes."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast, get_args

from disco.core import DEFAULT_OWNER_ID, SkillStore
from disco.core.store.sqlite import SqliteEventStore
from disco.core.workflow import (
    ScheduleSpec,
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
from disco.tools.builtin.workflow_tools import (
    DraftParamType,
    DraftWorkflowArgs,
    DraftWorkflowParam,
    JsonDirWorkflowStore,
    build_workflow_definition,
)
from disco.tools.projects import StorageStatus
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..runtime import ConversationRuntime
from ..workflow_schedule import JsonWorkflowScheduleStore

_SAFE_ID_FRAGMENT = re.compile(r"[^A-Za-z0-9_.-]+")
_MCP_NAME_RE = re.compile(r"^mcp__([^_][A-Za-z0-9_]*)__([^_].+)$")
_AUTHORING_OUTPUT_FORMATS = ("markdown", "html", "json", "csv", "pptx", "pdf", "text")
_ONE_SHOT_CRON = "* * * * *"

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

    async def _authoring_context() -> dict:
        if runtime is None:
            return {
                "builtin_tools": [],
                "mcp_servers": [],
                "skills": [],
                "param_types": list(get_args(DraftParamType)),
                "output_formats": list(_AUTHORING_OUTPUT_FORMATS),
                "status": "no_runtime",
            }
        env = _surface_environment(runtime)
        return _authoring_context_payload(env)

    async def _draft_workflow(body: DraftWorkflowBody) -> dict:
        if runtime is None:
            raise HTTPException(status_code=503, detail={"reason": "no_runtime"})
        workflow_store = _workflow_store(runtime)
        env = _surface_environment(runtime)
        instance_id = body.instance_id or _generated_instance_id(body.definition)
        return _draft_definition(
            workflow_store=workflow_store,
            env=env,
            definition=body.definition,
            params=body.params,
            instance_id=instance_id,
        )

    async def _author_workflow(body: DraftWorkflowArgs) -> Any:
        if runtime is None:
            raise HTTPException(status_code=503, detail={"reason": "no_runtime"})
        try:
            definition = build_workflow_definition(body)
        except ValueError as exc:
            return JSONResponse(
                status_code=422,
                content={"reason": "workflow_definition_invalid", "detail": str(exc)},
            )

        workflow_store = _workflow_store(runtime)
        env = _surface_environment(runtime)
        return _draft_definition(
            workflow_store=workflow_store,
            env=env,
            definition=definition,
            params=_fixture_params(body.params),
            instance_id=_generated_instance_id(definition),
        )

    async def _run_workflow(instance_id: str) -> dict:
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
        if not instance.enabled or instance.approval is None:
            raise HTTPException(
                status_code=409,
                detail={"reason": "workflow_not_approved"},
            )

        try:
            conversation_id = await _fire_approved_workflow_once(
                runtime,
                instance_id=instance_id,
                instance=instance,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail={"reason": "workflow_run_failed", "message": str(exc)},
            ) from exc
        return {"conversation_id": conversation_id, "status": "started"}

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
    router.add_api_route(
        "/api/workflows/authoring-context", _authoring_context, methods=["GET"]
    )
    router.add_api_route("/workflows/authoring-context", _authoring_context, methods=["GET"])
    router.add_api_route("/api/workflows/draft", _draft_workflow, methods=["POST"])
    router.add_api_route("/workflows/draft", _draft_workflow, methods=["POST"])
    router.add_api_route("/api/workflows/author", _author_workflow, methods=["POST"])
    router.add_api_route("/workflows/author", _author_workflow, methods=["POST"])
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
    router.add_api_route(
        "/api/workflows/{instance_id}/run",
        _run_workflow,
        methods=["POST"],
    )
    router.add_api_route(
        "/workflows/{instance_id}/run",
        _run_workflow,
        methods=["POST"],
    )
    return router


def _draft_definition(
    *,
    workflow_store: JsonDirWorkflowStore,
    env: _SurfaceEnvironment,
    definition: WorkflowDefinition,
    params: dict[str, Any],
    instance_id: str,
) -> dict:
    findings = validate_definition(
        definition,
        env.builtin_names,
        env.mcp_names,
        env.skill_names,
    )
    with tempfile.TemporaryDirectory(prefix="disco-workflow-sim-") as tmp:
        simulation = simulate_definition(
            definition,
            params=params,
            workspace_root=tmp,
            available_builtin_names=env.builtin_names,
            available_mcp_names=env.mcp_names,
        )
    all_findings = _dedupe_findings([*findings, *simulation.findings])
    instance = WorkflowInstance(
        definition_digest=definition.digest(),
        definition=definition,
        params=params,
        enabled=False,
        approval=None,
        validation_findings=tuple(all_findings),
    )
    workflow_store.save_instance(instance_id, instance)
    return {
        "workflow": _workflow_review_payload(instance_id, instance, env),
        "simulation": simulation.model_dump(mode="json"),
    }


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


def _authoring_context_payload(env: _SurfaceEnvironment) -> dict[str, object]:
    mcp_tools_by_server: dict[str, set[str]] = {}
    for name in env.mcp_names:
        parsed = _split_mcp_name(name)
        if parsed is None:
            continue
        server, tool = parsed
        mcp_tools_by_server.setdefault(server, set()).add(tool)

    skill_names = {
        str(payload["name"])
        for payload in env.skill_payloads.values()
        if isinstance(payload.get("name"), str)
    }
    return {
        "builtin_tools": [
            {
                "name": tool_def.name,
                "description": tool_def.description,
                "read_only": bool(tool_def.read_only),
            }
            for tool_def in sorted(env.builtin_defs.values(), key=lambda item: item.name)
        ],
        "mcp_servers": [
            {"server": server, "tools": sorted(tools)}
            for server, tools in sorted(mcp_tools_by_server.items())
        ],
        "skills": sorted(skill_names),
        "param_types": list(get_args(DraftParamType)),
        "output_formats": list(_AUTHORING_OUTPUT_FORMATS),
    }


def _split_mcp_name(name: str) -> tuple[str, str] | None:
    match = _MCP_NAME_RE.match(name)
    if match is None:
        return None
    return match.group(1), match.group(2)


def _fixture_params(params: list[DraftWorkflowParam]) -> dict[str, object]:
    return {param.name: _fixture_param_value(param.type) for param in params}


def _fixture_param_value(param_type: DraftParamType) -> object:
    if param_type == "string":
        return "sample"
    if param_type == "integer":
        return 1
    if param_type == "number":
        return 1.0
    if param_type == "boolean":
        return True
    if param_type == "string_array":
        return ["sample"]
    if param_type == "integer_array":
        return [1]
    return [1.0]


async def _fire_approved_workflow_once(
    runtime: ConversationRuntime,
    *,
    instance_id: str,
    instance: WorkflowInstance,
) -> str:
    spec = ScheduleSpec(
        instance_id=instance_id,
        instance_digest=instance.definition_digest,
        cron=_ONE_SHOT_CRON,
        enabled=False,
    )
    row = runtime.create_workflow_schedule(spec, owner_id=DEFAULT_OWNER_ID)
    schedule_id = str(row.get("schedule_id") or "")
    if not schedule_id:
        raise ValueError("workflow schedule creation did not return a schedule_id")
    try:
        record = await runtime.fire_workflow_schedule_now(
            schedule_id,
            owner_id=DEFAULT_OWNER_ID,
        )
    finally:
        _delete_ephemeral_workflow_schedule(runtime, schedule_id)
    if record is None:
        raise ValueError("workflow schedule fire-now did not return a run record")
    conversation_id = str(record.get("run_cid") or "")
    if not conversation_id:
        raise ValueError(str(record.get("error") or "workflow run did not start"))
    return conversation_id


def _delete_ephemeral_workflow_schedule(
    runtime: ConversationRuntime,
    schedule_id: str,
) -> None:
    project_store = runtime.project_store()
    root = project_store.root
    if root is None:
        return
    path = JsonWorkflowScheduleStore(root).schedules_dir / f"{schedule_id}.json"
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return


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
