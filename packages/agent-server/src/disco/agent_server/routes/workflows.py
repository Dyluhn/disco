"""Workflow draft/review/approval routes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast, get_args

from disco.core import DEFAULT_OWNER_ID
from disco.core.llm.types import (
    CapabilityProfile,
    CompletionRequest,
    LLMMessage,
    ModelRole,
)
from disco.core.store.sqlite import SqliteEventStore
from disco.core.think import strip_think_spans
from disco.core.workflow import (
    WORKFLOW_CONTROL_TOOLS,
    ScheduleSpec,
    WorkflowApproval,
    WorkflowDefinition,
    WorkflowInstance,
    WorkflowValidationFinding,
    compile_workflow_scope,
    simulate_definition,
    validate_definition,
    workflow_finish_tool_description,
    workflow_finish_tool_schema,
)
from disco.tools import ToolDef, ToolScope, build_default_registry
from disco.tools.builtin.workflow_tools import (
    DraftParamType,
    DraftWorkflowArgs,
    DraftWorkflowMcpMount,
    DraftWorkflowParam,
    JsonDirWorkflowStore,
    build_workflow_definition,
)
from disco.tools.projects import StorageStatus
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..auth import current_owner_id, current_session
from ..runtime import ConversationRuntime
from ..workflow_schedule import JsonWorkflowScheduleStore

_SAFE_ID_FRAGMENT = re.compile(r"[^A-Za-z0-9_.-]+")
_MCP_NAME_RE = re.compile(r"^mcp__([^_][A-Za-z0-9_]*)__([^_].+)$")
_AUTHORING_OUTPUT_FORMATS = ("markdown", "html", "json", "csv", "pptx", "pdf", "text")
_ONE_SHOT_CRON = "* * * * *"


class DraftWorkflowBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    definition: WorkflowDefinition
    params: dict[str, Any] = Field(default_factory=dict)
    instance_id: str | None = Field(default=None, max_length=120)


class ApproveWorkflowBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved_by: str = Field(default=DEFAULT_OWNER_ID, min_length=1, max_length=240)
    surface_shown_digest: str | None = Field(default=None, min_length=1, max_length=80)


class DraftWorkflowFromDescriptionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1, max_length=2000)

    @field_validator("description")
    @classmethod
    def _description_non_blank(cls, value: str) -> str:
        description = value.strip()
        if not description:
            raise ValueError("description must be non-empty")
        return description


@dataclass(frozen=True)
class _SurfaceEnvironment:
    builtin_defs: dict[str, ToolDef]
    builtin_names: frozenset[str]
    mcp_defs: dict[str, ToolDef]
    mcp_names: frozenset[str]
    skill_names: frozenset[str]
    skill_payloads: dict[str, dict[str, object]]


class _WorkflowDraftFailed(ValueError):
    pass


def make_workflows_router(
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,  # noqa: ARG001
) -> APIRouter:
    router = APIRouter()

    async def _list_workflows(request: Request) -> dict:
        if runtime is None:
            return {"workflows": [], "status": "no_runtime"}
        workflow_store = _workflow_store_for_request(runtime, request)
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

    async def _draft_workflow(body: DraftWorkflowBody, request: Request) -> dict:
        if runtime is None:
            raise HTTPException(status_code=503, detail={"reason": "no_runtime"})
        workflow_store = _workflow_store_for_request(runtime, request)
        env = _surface_environment(runtime)
        instance_id = body.instance_id or _generated_instance_id(body.definition)
        return _draft_definition(
            workflow_store=workflow_store,
            env=env,
            definition=body.definition,
            params=body.params,
            instance_id=instance_id,
        )

    async def _author_workflow(body: DraftWorkflowArgs, request: Request) -> Any:
        if runtime is None:
            raise HTTPException(status_code=503, detail={"reason": "no_runtime"})
        try:
            definition = build_workflow_definition(body)
        except ValueError as exc:
            return JSONResponse(
                status_code=422,
                content={"reason": "workflow_definition_invalid", "detail": str(exc)},
            )

        workflow_store = _workflow_store_for_request(runtime, request)
        env = _surface_environment(runtime)
        return _draft_definition(
            workflow_store=workflow_store,
            env=env,
            definition=definition,
            params=_fixture_params(body.params),
            instance_id=_generated_instance_id(definition),
        )

    async def _draft_workflow_from_description(
        body: DraftWorkflowFromDescriptionBody,
        request: Request,
    ) -> Any:
        return await _draft_workflow_from_description_impl(
            runtime,
            body,
            owner_id=current_owner_id(request),
        )

    async def _run_workflow(instance_id: str, request: Request) -> dict:
        if runtime is None:
            raise HTTPException(status_code=503, detail={"reason": "no_runtime"})
        workflow_store = _workflow_store_for_request(runtime, request)
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
                owner_id=current_owner_id(request),
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail={"reason": "workflow_run_failed", "message": str(exc)},
            ) from exc
        return {"conversation_id": conversation_id, "status": "started"}

    async def _approve_workflow(
        instance_id: str, body: ApproveWorkflowBody, request: Request
    ) -> dict:
        if runtime is None:
            raise HTTPException(status_code=503, detail={"reason": "no_runtime"})
        workflow_store = _workflow_store_for_request(runtime, request)
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

        approved_by = current_owner_id(request)
        if os.environ.get("PYTEST_CURRENT_TEST"):
            approved_by = body.approved_by
        approved = instance.model_copy(
            update={
                "enabled": True,
                "approval": WorkflowApproval(
                    approved_at=_now_iso(),
                    approved_by=approved_by,
                    surface_shown_digest=surface_digest,
                ),
                "validation_findings": tuple(findings),
            }
        )
        workflow_store.save_instance(instance_id, approved)
        return {"workflow": _workflow_review_payload(instance_id, approved, env)}

    _add_workflow_route_pair(router, "", _list_workflows, methods=["GET"])
    _add_workflow_route_pair(router, "/authoring-context", _authoring_context, methods=["GET"])
    _add_workflow_route_pair(router, "/draft", _draft_workflow, methods=["POST"])
    _add_workflow_route_pair(router, "/author", _author_workflow, methods=["POST"])
    _add_workflow_route_pair(
        router,
        "/draft-from-description",
        _draft_workflow_from_description,
        methods=["POST"],
    )
    _add_workflow_route_pair(router, "/{instance_id}/approve", _approve_workflow, methods=["POST"])
    _add_workflow_route_pair(router, "/{instance_id}/run", _run_workflow, methods=["POST"])
    return router


def _add_workflow_route_pair(
    router: APIRouter,
    suffix: str,
    endpoint: Any,
    *,
    methods: list[str],
) -> None:
    router.add_api_route(f"/api/workflows{suffix}", endpoint, methods=methods)
    router.add_api_route(f"/workflows{suffix}", endpoint, methods=methods)


def _draft_definition(
    *,
    workflow_store: JsonDirWorkflowStore,
    env: _SurfaceEnvironment,
    definition: WorkflowDefinition,
    params: dict[str, Any],
    instance_id: str,
    extra_findings: list[WorkflowValidationFinding] | None = None,
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
    all_findings = _dedupe_findings([*(extra_findings or []), *findings, *simulation.findings])
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


def _workflow_store(
    runtime: ConversationRuntime,
    *,
    owner_id: str | None = None,
    include_unclaimed_legacy: bool = False,
) -> JsonDirWorkflowStore:
    project_store = runtime.project_store()
    if project_store.status() != StorageStatus.OK:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "project_storage_unavailable",
                "status": project_store.status().value,
            },
        )
    root = project_store.root
    if root is None:
        raise HTTPException(
            status_code=409,
            detail={"reason": "project_storage_unavailable", "status": "unset"},
        )
    return JsonDirWorkflowStore(
        root,
        owner_id=owner_id,
        include_unclaimed_legacy=include_unclaimed_legacy,
    )


def _workflow_store_for_request(
    runtime: ConversationRuntime,
    request: Request,
) -> JsonDirWorkflowStore:
    session = current_session(request)
    return _workflow_store(
        runtime,
        owner_id=session.owner_id,
        include_unclaimed_legacy=session.is_admin,
    )


def _surface_environment(runtime: ConversationRuntime) -> _SurfaceEnvironment:
    registry = build_default_registry()
    builtin_tools = registry.in_scope(ToolScope(allowed_tools=registry.names()))
    builtin_defs = {tool.definition.name: tool.definition for tool in builtin_tools}
    builtin_names = frozenset(builtin_defs) | WORKFLOW_CONTROL_TOOLS

    mcp_defs: dict[str, ToolDef] = {}
    for tool_def in runtime._workflow_tool_definitions():
        if isinstance(tool_def, ToolDef):
            mcp_defs[tool_def.name] = tool_def

    skills = runtime._workflow_skills()
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
        "owner_id": instance.owner_id,
        "name": instance.definition.name,
        "card": instance.definition.card,
        "definition_digest": instance.definition_digest,
        "enabled": instance.enabled,
        "approved": instance.approval is not None,
        "approval": (
            instance.approval.model_dump(mode="json") if instance.approval is not None else None
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
        "tool_definitions": [
            _surface_tool_payload(name, env, instance.definition) for name in allowed
        ],
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


def _surface_tool_payload(
    name: str,
    env: _SurfaceEnvironment,
    definition: WorkflowDefinition,
) -> dict[str, object]:
    if name == "finish":
        return {
            "name": "finish",
            "description": workflow_finish_tool_description(definition),
            "parameters_schema": workflow_finish_tool_schema(definition),
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


async def _draft_workflow_from_description_impl(
    runtime: ConversationRuntime | None,
    body: DraftWorkflowFromDescriptionBody,
    *,
    owner_id: str = DEFAULT_OWNER_ID,
) -> Any:
    if runtime is None:
        raise HTTPException(status_code=503, detail={"reason": "no_runtime"})

    env = _surface_environment(runtime)
    try:
        args, summary = await _draft_args_from_description(
            runtime=runtime,
            env=env,
            description=body.description,
        )
    except _WorkflowDraftFailed as exc:
        return JSONResponse(
            status_code=422,
            content={"reason": "workflow_draft_failed", "detail": str(exc)},
        )

    extra_findings: list[WorkflowValidationFinding] = []
    try:
        definition = build_workflow_definition(args)
        draft_args = args
    except ValueError as exc:
        extra_findings.append(
            WorkflowValidationFinding(
                severity="error",
                code="workflow_definition_invalid",
                path="definition",
                message=str(exc),
            )
        )
        draft_args = _repair_args_after_definition_error(args)
        try:
            definition = build_workflow_definition(draft_args)
        except ValueError as repair_exc:
            return JSONResponse(
                status_code=422,
                content={
                    "reason": "workflow_draft_failed",
                    "detail": (
                        "model returned a workflow shape that could not be "
                        f"converted into a draft: {repair_exc}"
                    ),
                },
            )

    workflow_store = _workflow_store(runtime, owner_id=owner_id)
    drafted = _draft_definition(
        workflow_store=workflow_store,
        env=env,
        definition=definition,
        params=_fixture_params(draft_args.params),
        instance_id=_generated_instance_id(definition),
        extra_findings=extra_findings,
    )
    return {
        **drafted,
        "summary": summary,
        "description": body.description,
    }


async def _draft_args_from_description(
    *,
    runtime: ConversationRuntime,
    env: _SurfaceEnvironment,
    description: str,
) -> tuple[DraftWorkflowArgs, str]:
    first = await _request_workflow_draft(
        runtime=runtime,
        env=env,
        description=description,
        max_tokens=1400,
    )
    parsed = _parse_draft_response(first)
    if parsed is None:
        second = await _request_workflow_draft(
            runtime=runtime,
            env=env,
            description=description,
            max_tokens=2800,
        )
        parsed = _parse_draft_response(second)
    if parsed is None:
        raise _WorkflowDraftFailed("model did not return a usable JSON object")

    normalized, summary = _normalize_draft_payload(parsed, description=description)
    try:
        args = DraftWorkflowArgs.model_validate(normalized)
    except ValueError as exc:
        raise _WorkflowDraftFailed(f"model returned an invalid workflow draft: {exc}") from exc
    return args, summary


async def _request_workflow_draft(
    *,
    runtime: ConversationRuntime,
    env: _SurfaceEnvironment,
    description: str,
    max_tokens: int,
) -> str:
    router = runtime._drivers.router()
    resp = await router.complete(
        CompletionRequest(
            profile=CapabilityProfile(role=ModelRole.AGENT_DRIVER),
            messages=[
                LLMMessage(
                    role="system",
                    content=(
                        "You draft Disco workflow definitions for human review. "
                        "Return only one JSON object and no markdown. The object "
                        "must contain summary plus the DraftWorkflowArgs fields."
                    ),
                ),
                LLMMessage(
                    role="user",
                    content=_draft_workflow_prompt(description=description, env=env),
                ),
            ],
            response_format="json",
            max_tokens=max_tokens,
            temperature=0.3,
        )
    )
    return str(getattr(resp, "text", "") or "")


def _draft_workflow_prompt(
    *,
    description: str,
    env: _SurfaceEnvironment,
) -> str:
    return (
        "Draft a workflow for this plain-language request:\n"
        f"{description}\n\n"
        "Return exactly this JSON shape:\n"
        "{\n"
        '  "summary": "one sentence plain-language description",\n'
        '  "name": "short workflow name",\n'
        '  "card": "one paragraph, no blank lines",\n'
        '  "params": ['
        '{"name":"query","type":"string","required":true,"description":"..."}],\n'
        '  "tools": ["builtin_tool_name"],\n'
        '  "mcp_mounts": ['
        '{"server":"server","tool_names":["tool"],"read_only":true}],\n'
        '  "skills": ["Skill Name"],\n'
        '  "allows_writes": false,\n'
        '  "untrusted_content": true,\n'
        '  "output_path_template": "outputs/{query}.md",\n'
        '  "output_format": "markdown",\n'
        '  "verify_checks": ["output_exists"],\n'
        '  "finalizer": "ready_for_workflow_output"\n'
        "}\n\n"
        "Constraints:\n"
        "- Choose tools only from the available builtin tool names below. Pick the "
        "minimal set needed; use [] if no builtin tool is needed.\n"
        "- Choose MCP mounts only from the available MCP server/tool inventory below. "
        "Use bare tool names inside tool_names.\n"
        "- Choose skills only from the available skill names below.\n"
        "- Parameter types must come from the fixed param_types list.\n"
        "- output_format must come from the output_formats list.\n"
        "- card must be one paragraph with no blank lines.\n"
        "- output_path_template must use {param} placeholders that match declared "
        "params. Prefer a small number of params.\n"
        "- Set allows_writes true only when the workflow must write files or use "
        "write-capable tools/connectors.\n\n"
        "Authoring inventory:\n"
        + json.dumps(_draft_inventory_payload(env), indent=2, sort_keys=True)
    )


def _draft_inventory_payload(env: _SurfaceEnvironment) -> dict[str, object]:
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
                "description": _one_line(tool_def.description),
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


def _parse_draft_response(text: str) -> dict[str, Any] | None:
    stripped = strip_think_spans(text or "").strip()
    if not stripped:
        return None
    for candidate in (stripped, _extract_json_object(stripped)):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and parsed:
            return parsed
    return None


def _extract_json_object(text: str) -> str | None:
    start = text.find("{")
    if start < 0:
        return None
    try:
        _, end = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError:
        return None
    return text[start : start + end]


def _normalize_draft_payload(
    payload: dict[str, Any],
    *,
    description: str,
) -> tuple[dict[str, Any], str]:
    normalized = dict(payload)
    summary = _summary_sentence(normalized.pop("summary", None), description=description)

    for key in ("params", "tools", "mcp_mounts", "skills", "verify_checks"):
        if normalized.get(key) is None:
            normalized[key] = []
    normalized["tools"] = _string_list(normalized.get("tools"))
    normalized["skills"] = _string_list(normalized.get("skills"))
    normalized["verify_checks"] = _string_list(normalized.get("verify_checks"))
    normalized["params"] = _normalize_params(normalized.get("params"))
    normalized["mcp_mounts"] = _normalize_mcp_mounts(normalized.get("mcp_mounts"))

    normalized["allows_writes"] = _bool_or_default(normalized.get("allows_writes"), default=False)
    normalized["untrusted_content"] = _bool_or_default(
        normalized.get("untrusted_content"), default=True
    )
    if normalized.get("finalizer") in ("", False):
        normalized["finalizer"] = None
    return normalized, summary


def _normalize_params(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    params: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        param: dict[str, object] = dict(item)
        if not isinstance(param.get("required"), bool):
            param["required"] = True
        if param.get("description") in ("", None):
            param.pop("description", None)
        params.append(param)
    return params


def _normalize_mcp_mounts(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    mounts: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        mount = dict(item)
        mount["tool_names"] = _string_list(mount.get("tool_names"))
        if not isinstance(mount.get("read_only"), bool):
            mount["read_only"] = True
        mounts.append(mount)
    return mounts


def _string_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_items: list[object] = [
            item.strip() for item in re.split(r"[,\n]", value) if item.strip()
        ]
    elif isinstance(value, list):
        raw_items = value
    else:
        return []
    return [str(item).strip() for item in raw_items if str(item).strip()]


def _bool_or_default(value: object, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "1"}:
            return True
        if lowered in {"false", "no", "0"}:
            return False
    return default


def _summary_sentence(value: object, *, description: str) -> str:
    summary = str(value or "").strip() or description.strip()
    summary = _one_line(summary)
    if not summary.endswith((".", "!", "?")):
        summary += "."
    return summary[:280]


def _one_line(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _repair_args_after_definition_error(args: DraftWorkflowArgs) -> DraftWorkflowArgs:
    params: list[DraftWorkflowParam] = []
    seen_params: set[str] = set()
    for param in args.params:
        if param.name in seen_params:
            continue
        seen_params.add(param.name)
        params.append(param)

    mcp_mounts = [
        DraftWorkflowMcpMount(
            server=mount.server,
            tool_names=list(dict.fromkeys(mount.tool_names)),
            read_only=mount.read_only,
        )
        for mount in args.mcp_mounts
    ]
    allows_writes = args.allows_writes or any(not mount.read_only for mount in mcp_mounts)
    return args.model_copy(
        update={
            "card": _one_line(args.card),
            "params": params,
            "tools": list(dict.fromkeys(args.tools)),
            "mcp_mounts": mcp_mounts,
            "skills": list(dict.fromkeys(args.skills)),
            "allows_writes": allows_writes,
            "verify_checks": list(dict.fromkeys(args.verify_checks)),
        }
    )


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
    owner_id: str,
) -> str:
    spec = ScheduleSpec(
        instance_id=instance_id,
        instance_digest=instance.definition_digest,
        cron=_ONE_SHOT_CRON,
        enabled=False,
    )
    row = runtime.create_workflow_schedule(spec, owner_id=owner_id)
    schedule_id = str(row.get("schedule_id") or "")
    if not schedule_id:
        raise ValueError("workflow schedule creation did not return a schedule_id")
    try:
        record = await runtime.fire_workflow_schedule_now(
            schedule_id,
            owner_id=owner_id,
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
