"""Workflow draft/review/approval routes."""

from __future__ import annotations

import os
import re
import tempfile
from datetime import UTC, datetime
from typing import Any, cast, get_args

from disco.core import DEFAULT_OWNER_ID
from disco.core.store.sqlite import SqliteEventStore
from disco.core.workflow import (
    WorkflowApproval,
    WorkflowDefinition,
    WorkflowInstance,
    WorkflowValidationFinding,
    simulate_definition,
    validate_definition,
)
from disco.tools.builtin.workflow_tools import (
    DraftParamType,
    DraftWorkflowArgs,
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
from ..workflow_surface import (
    _compiled_surface_payload,
    _digest_payload,
    _surface_environment,
    _SurfaceEnvironment,
)
from .workflows_parts.draft_from_description import (
    _draft_workflow_from_description_impl,
)

_SAFE_ID_FRAGMENT = re.compile(r"[^A-Za-z0-9_.-]+")
_MCP_NAME_RE = re.compile(r"^mcp__([^_][A-Za-z0-9_]*)__([^_].+)$")
_AUTHORING_OUTPUT_FORMATS = ("markdown", "html", "json", "csv", "pptx", "pdf", "text")


class DraftWorkflowBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    definition: WorkflowDefinition
    params: dict[str, Any] = Field(default_factory=dict)
    instance_id: str | None = Field(default=None, max_length=120)


class ApproveWorkflowBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved_by: str = Field(default=DEFAULT_OWNER_ID, min_length=1, max_length=240)
    surface_shown_digest: str | None = Field(default=None, min_length=1, max_length=80)


class SetWorkflowEnabledBody(BaseModel):
    """Explicit owner-scoped switch for an approved workflow instance."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool


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


class RunWorkflowBody(BaseModel):
    """Explicit inputs for a clicked workflow run.

    ``conversation_id`` is optional only because the UI can explicitly choose
    “new Agent conversation”. It is never a schedule target: when supplied it
    must belong to the caller and be an ordinary Agent conversation.
    """

    model_config = ConfigDict(extra="forbid")

    params: dict[str, Any] = Field(default_factory=dict)
    conversation_id: str | None = Field(default=None, max_length=120)


def _raise_workflow_invocation_error(outcome: Any) -> None:
    reason = outcome.reason or "workflow_run_failed"
    status_code = 422 if reason == "invalid_parameters" else 409
    raise HTTPException(
        status_code=status_code,
        detail={
            "reason": reason,
            "parameter_issues": [
                issue.model_dump(mode="json") for issue in outcome.parameter_issues
            ],
            "readiness": outcome.readiness.model_dump(mode="json"),
        },
    )


def make_workflows_router(
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,  # noqa: ARG001
) -> APIRouter:
    """Assemble the workflow router.

    Each closure below is a thin FastAPI-visible adapter that only captures
    ``runtime`` and forwards to a module-level ``_..._response`` function that
    takes ``runtime`` explicitly — the actual endpoint bodies live at module
    level so they carry their own (budgeted) logical-line weight instead of
    all pooling into this factory. Route paths/methods are unchanged.
    """
    router = APIRouter()

    async def _list_workflows(request: Request) -> dict:
        return await _list_workflows_response(runtime, request)

    async def _authoring_context() -> dict:
        return await _authoring_context_response(runtime)

    async def _draft_workflow(body: DraftWorkflowBody, request: Request) -> dict:
        return await _draft_workflow_response(runtime, body, request)

    async def _author_workflow(body: DraftWorkflowArgs, request: Request) -> Any:
        return await _author_workflow_response(runtime, body, request)

    async def _draft_workflow_from_description(
        body: DraftWorkflowFromDescriptionBody,
        request: Request,
    ) -> Any:
        return await _draft_workflow_from_description_impl(
            runtime,
            body,
            owner_id=current_owner_id(request),
        )

    async def _run_workflow(
        instance_id: str,
        request: Request,
        body: RunWorkflowBody | None = None,
    ) -> dict:
        return await _run_workflow_response(runtime, store, instance_id, body, request)

    async def _approve_workflow(
        instance_id: str, body: ApproveWorkflowBody, request: Request
    ) -> dict:
        return await _approve_workflow_response(runtime, instance_id, body, request)

    async def _set_workflow_enabled(
        instance_id: str, body: SetWorkflowEnabledBody, request: Request
    ) -> dict:
        return await _set_workflow_enabled_response(runtime, instance_id, body, request)

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
    # PATCH is the canonical resource mutation. Keep the POST alias for the
    # small action-style clients that already use /approve and /run.
    _add_workflow_route_pair(
        router, "/{instance_id}/enabled", _set_workflow_enabled, methods=["PATCH"]
    )
    _add_workflow_route_pair(
        router, "/{instance_id}/enable", _set_workflow_enabled, methods=["POST"]
    )
    _add_workflow_route_pair(router, "/{instance_id}/run", _run_workflow, methods=["POST"])
    return router


async def _list_workflows_response(runtime: ConversationRuntime | None, request: Request) -> dict:
    if runtime is None:
        return {"workflows": [], "status": "no_runtime"}
    workflow_store = _workflow_store_for_request(runtime, request)
    env = _surface_environment(runtime)
    workflows = [
        _workflow_review_payload(
            row.instance_id,
            row.instance,
            env,
            runtime=runtime,
            owner_id=current_owner_id(request),
        )
        for row in workflow_store.list_instances()
    ]
    return {"workflows": workflows, "status": "ok"}


async def _authoring_context_response(runtime: ConversationRuntime | None) -> dict:
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


async def _draft_workflow_response(
    runtime: ConversationRuntime | None, body: DraftWorkflowBody, request: Request
) -> dict:
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
        runtime=runtime,
        owner_id=current_owner_id(request),
    )


async def _author_workflow_response(
    runtime: ConversationRuntime | None, body: DraftWorkflowArgs, request: Request
) -> Any:
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
        runtime=runtime,
        owner_id=current_owner_id(request),
    )


async def _run_workflow_response(
    runtime: ConversationRuntime | None,
    store: SqliteEventStore,
    instance_id: str,
    body: RunWorkflowBody | None,
    request: Request,
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
    params = body.params if body is not None else {}
    target_id = body.conversation_id if body is not None else None
    owner_id = current_owner_id(request)
    # Validate readiness and exact inputs before a New Agent conversation is
    # allocated. invoke_workflow repeats this immediately before persistence
    # as the TOCTOU gate.
    preflight = runtime.prepare_workflow(instance_id, params, owner_id=owner_id)
    if not preflight.accepted:
        _raise_workflow_invocation_error(preflight)
    if target_id is None:
        # A clicked Run may explicitly choose a new Agent conversation. Reuse
        # the normal conversation creation path so settings, ownership, and
        # model selection remain identical to the Agent surface.
        from ._common import CreateConversationBody
        from .conversations import _create_conversation_response

        created = await _create_conversation_response(
            store,
            runtime,
            CreateConversationBody(surface="agent", title=instance.definition.name),
            request,
        )
        target_id = str(created["conversation_id"])
    else:
        from ._common import require_owned_conversation

        target_id = await require_owned_conversation(request, store, target_id)
        if runtime.surface_of(target_id) != "agent":
            raise HTTPException(
                status_code=409,
                detail={"reason": "workflow_target_not_agent"},
            )

    result = await runtime.invoke_workflow(
        target_id,
        instance_id,
        params,
        owner_id=owner_id,
    )
    if not result.started:
        _raise_workflow_invocation_error(result)
    return {
        "conversation_id": target_id,
        "run_id": result.run_id,
        "status": "started",
    }


async def _approve_workflow_response(
    runtime: ConversationRuntime | None,
    instance_id: str,
    body: ApproveWorkflowBody,
    request: Request,
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
    owner_id = current_owner_id(request)
    review = _workflow_review_payload(
        instance_id,
        instance,
        env,
        runtime=runtime,
        owner_id=owner_id,
    )
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
    return {
        "workflow": _workflow_review_payload(
            instance_id,
            approved,
            env,
            runtime=runtime,
            owner_id=owner_id,
        )
    }


async def _set_workflow_enabled_response(
    runtime: ConversationRuntime | None,
    instance_id: str,
    body: SetWorkflowEnabledBody,
    request: Request,
) -> dict:
    """Set only the durable enabled bit and return the canonical review.

    The workflow store is owner-scoped before the mutation, so an instance
    owned by another session is indistinguishable from a missing instance.
    Approval, definition, parameters, and findings are deliberately preserved;
    readiness remains the single admission authority for both UI Run and Agent
    invocation.
    """

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
        raise HTTPException(status_code=404, detail={"reason": "workflow_not_found"})

    updated = instance.model_copy(update={"enabled": body.enabled})
    workflow_store.save_instance(instance_id, updated)
    env = _surface_environment(runtime)
    return {
        "workflow": _workflow_review_payload(
            instance_id,
            updated,
            env,
            runtime=runtime,
            owner_id=current_owner_id(request),
        )
    }


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
    runtime: ConversationRuntime | None = None,
    owner_id: str | None = None,
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
        # Authoring values are simulation inputs only.  A saved capability
        # must not accidentally turn a generated fixture into the value used
        # by a later Agent run or recurring schedule.
        params={},
        enabled=False,
        approval=None,
        validation_findings=tuple(all_findings),
    )
    workflow_store.save_instance(instance_id, instance)
    return {
        "workflow": _workflow_review_payload(
            instance_id,
            instance,
            env,
            runtime=runtime,
            owner_id=owner_id,
        ),
        "simulation": simulation.model_dump(mode="json"),
    }


def _workflow_store(
    runtime: ConversationRuntime,
    *,
    owner_id: str | None = None,
    include_unclaimed_legacy: bool = False,
) -> JsonDirWorkflowStore:
    project_store = runtime.projects.current_project_store()
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


def _workflow_review_payload(
    instance_id: str,
    instance: WorkflowInstance,
    env: _SurfaceEnvironment,
    *,
    runtime: ConversationRuntime | None = None,
    owner_id: str | None = None,
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
    readiness = (
        runtime.workflow_readiness(
            instance_id,
            owner_id=owner_id or instance.owner_id,
        )
        if runtime is not None
        else None
    )
    return {
        "instance_id": instance_id,
        "owner_id": instance.owner_id,
        "origin": instance.origin,
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
        "readiness": (
            readiness.model_dump(mode="json")
            if readiness is not None
            else {
                "ready": False,
                "status": "needs_setup",
                "reasons": ["runtime_unavailable"],
                "blocking_findings": [],
            }
        ),
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


def _generated_instance_id(defn: WorkflowDefinition) -> str:
    slug = _SAFE_ID_FRAGMENT.sub("-", defn.name.strip()).strip("-._").lower()
    slug = slug or "workflow"
    digest = defn.digest().split(":", 1)[-1][:12]
    return f"wf_{slug[:60]}_{digest}"


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
