"""The canonical compiled workflow surface and its approval digest.

This module intentionally has no route dependencies.  Workflow review routes
and runtime composition both use the same environment discovery, compilation,
payload serialization, and digest implementation so approval bytes cannot
drift from the runtime admission check.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from disco.core.workflow import (
    WORKFLOW_CONTROL_TOOLS,
    WorkflowDefinition,
    WorkflowInstance,
    compile_workflow_scope,
    workflow_finish_tool_description,
    workflow_finish_tool_schema,
    workflow_surface_digest,
)
from disco.tools import ToolDef, ToolScope, build_default_registry

if TYPE_CHECKING:
    from .runtime import ConversationRuntime


@dataclass(frozen=True)
class _SurfaceEnvironment:
    builtin_defs: dict[str, ToolDef]
    builtin_names: frozenset[str]
    mcp_defs: dict[str, ToolDef]
    mcp_names: frozenset[str]
    skill_names: frozenset[str]
    skill_payloads: dict[str, dict[str, object]]


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


def _digest_payload(payload: object) -> str:
    return workflow_surface_digest(payload)


__all__ = [
    "_SurfaceEnvironment",
    "_compiled_surface_payload",
    "_digest_payload",
    "_surface_environment",
]
