"""Draft-a-workflow-from-a-plain-language-description pipeline.

Extracted from :mod:`disco.agent_server.routes.workflows` to shrink that
module's width. This subsystem calls back into several small collaborators
that stay in the parent module (``_surface_environment``, ``_workflow_store``,
``_draft_definition``, ``_fixture_params``, ``_generated_instance_id``,
``_split_mcp_name``) because those are shared with the other workflow routes
(``draft``, ``author``, ``authoring-context``) that remain there. Rather than
``from ..workflows import _surface_environment`` (which binds the pre-patch
function object at import time — and would be a genuine import cycle besides,
since ``workflows.py`` imports this module's entry point at ITS top level),
this module imports the parent module itself and calls through it
(``workflows._surface_environment(...)``), a module-attribute lookup resolved
at call time once both modules have finished loading. This is the same
parent/parts convention used elsewhere in the campaign (see e.g.
``disco.retrieval.deep_research._engine_parts``).
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any, get_args

from disco.core import DEFAULT_OWNER_ID
from disco.core.llm.types import (
    CapabilityProfile,
    CompletionRequest,
    LLMMessage,
    ModelRole,
)
from disco.core.think import strip_think_spans
from disco.core.workflow import WorkflowValidationFinding
from disco.tools.builtin.workflow_tools import (
    DraftParamType,
    DraftWorkflowArgs,
    DraftWorkflowMcpMount,
    DraftWorkflowParam,
    build_workflow_definition,
)
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from .. import workflows

if TYPE_CHECKING:
    from ...runtime import ConversationRuntime
    from ..workflows import DraftWorkflowFromDescriptionBody, _SurfaceEnvironment


class _WorkflowDraftFailed(ValueError):
    pass


async def _draft_workflow_from_description_impl(
    runtime: ConversationRuntime | None,
    body: DraftWorkflowFromDescriptionBody,
    *,
    owner_id: str = DEFAULT_OWNER_ID,
) -> Any:
    if runtime is None:
        raise HTTPException(status_code=503, detail={"reason": "no_runtime"})

    env = workflows._surface_environment(runtime)
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

    workflow_store = workflows._workflow_store(runtime, owner_id=owner_id)
    drafted = workflows._draft_definition(
        workflow_store=workflow_store,
        env=env,
        definition=definition,
        params=workflows._fixture_params(draft_args.params),
        instance_id=workflows._generated_instance_id(definition),
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
    router = runtime.drivers.router()
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
        parsed = workflows._split_mcp_name(name)
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
        "output_formats": list(workflows._AUTHORING_OUTPUT_FORMATS),
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
