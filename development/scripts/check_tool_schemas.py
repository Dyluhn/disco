#!/usr/bin/env python3
"""Tool schema fitness gate.

Fails if an advertised built-in tool argument leaves the model blind:

* a bare object parameter: ``{"type": "object"}`` with no properties and permissive
  ``additionalProperties``;
* an array whose ``items`` schema is absent or empty.

The scan starts from each tool's ``args_model.model_json_schema()`` and inlines refs
the same way ``ToolDef.to_spec()`` does, so nested model fields are audited in the
shape the model sees.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any

from disco.core.workflow import WorkflowInstance
from disco.tools.anatomy import ToolDef, _inline_schema_refs
from disco.tools.builtin import build_default_registry
from disco.tools.builtin.app_kit import APPKIT_V2_TOOLS
from disco.tools.builtin.workflow_tools import (
    StoredWorkflowInstance,
    WorkflowStore,
    workflow_router_tools,
)
from disco.tools.registry import ToolScope
from disco.tools.workflow_scope import WorkflowPhaseState

# Exact schema-location -> reason. Keep this painful: every truly free-form escape
# must explain why a bounded object/array schema would be dishonest.
_ADD_PRIMITIVE_SPEC_REASON = (
    "genuinely per-primitive JSON: the spec's real shape is the target "
    "primitive's declared spec_schema, known only at runtime and validated "
    "there (a refusal carries the expected schema). A single static schema "
    "for all primitives would be dishonest."
)
ALLOW_SCHEMA_HOLES: dict[str, str] = {
    # the tool registers via BOTH the default registry and APPKIT_V2_TOOLS,
    # so the same argument is scanned under two labels.
    "default:app_add_primitive $.spec": _ADD_PRIMITIVE_SPEC_REASON,
    "appkit_v2:app_add_primitive $.spec": _ADD_PRIMITIVE_SPEC_REASON,
}


@dataclass(frozen=True)
class Violation:
    location: str
    kind: str
    detail: str


class EmptyWorkflowStore(WorkflowStore):
    def list_instances(self) -> list[StoredWorkflowInstance]:
        return []

    def get_instance(self, instance_id: str) -> WorkflowInstance | None:
        return None


def _is_permissive_additional_properties(value: object) -> bool:
    return value is True or value == {} or value is None


def _schema_type(schema: dict[str, Any]) -> str | None:
    schema_type = schema.get("type")
    return schema_type if isinstance(schema_type, str) else None


def _direct_violation(node: dict[str, Any], *, location: str) -> Violation | None:
    """Decide whether ``node`` itself is a blind schema (bare object or
    untyped-array-items). Does not look at children."""
    schema_type = _schema_type(node)
    if schema_type == "object":
        properties = node.get("properties")
        additional = node.get("additionalProperties", True)
        if not properties and _is_permissive_additional_properties(additional):
            return Violation(
                location=location,
                kind="bare-object",
                detail="object has no properties and allows arbitrary keys",
            )
    elif schema_type == "array" and not node.get("items"):
        return Violation(
            location=location,
            kind="untyped-array-items",
            detail="array has no item schema",
        )
    return None


def _child_violations(node: dict[str, Any], *, label: str, path: str) -> list[Violation]:
    """Recurse into a dict node's children: ``properties``, ``items``, other
    list-valued keys, and other scalar-valued keys, in that order."""
    violations: list[Violation] = []
    for key, value in node.items():
        if key in {"description", "title", "default", "examples"}:
            continue
        if key == "properties" and isinstance(value, dict):
            for prop_name, prop_schema in value.items():
                violations.extend(
                    _walk_schema(
                        prop_schema,
                        label=label,
                        path=f"{path}.{prop_name}",
                        is_root=False,
                    )
                )
            continue
        if key == "items":
            violations.extend(
                _walk_schema(
                    value,
                    label=label,
                    path=f"{path}[]",
                    is_root=False,
                )
            )
            continue
        if isinstance(value, list):
            for index, item in enumerate(value):
                violations.extend(
                    _walk_schema(
                        item,
                        label=label,
                        path=f"{path}.{key}[{index}]",
                        is_root=False,
                    )
                )
        else:
            violations.extend(
                _walk_schema(
                    value,
                    label=label,
                    path=f"{path}.{key}",
                    is_root=False,
                )
            )
    return violations


def _walk_schema(
    node: object,
    *,
    label: str,
    path: str,
    is_root: bool,
) -> list[Violation]:
    if isinstance(node, dict):
        violations: list[Violation] = []
        location = f"{label} {path}"
        if not is_root and location not in ALLOW_SCHEMA_HOLES:
            direct = _direct_violation(node, location=location)
            if direct is not None:
                violations.append(direct)
        violations.extend(_child_violations(node, label=label, path=path))
        return violations
    if isinstance(node, list):
        violations = []
        for index, item in enumerate(node):
            violations.extend(
                _walk_schema(
                    item,
                    label=label,
                    path=f"{path}[{index}]",
                    is_root=False,
                )
            )
        return violations
    return []


def _tool_defs() -> list[tuple[str, ToolDef]]:
    registry = build_default_registry()
    scope = ToolScope(allowed_tools=registry.names())
    defs: list[tuple[str, ToolDef]] = []
    for name in sorted(registry.names()):
        tool = registry.get(name, scope=scope)
        if tool is None:
            raise RuntimeError(f"registered tool {name!r} was not resolvable")
        defs.append((f"default:{name}", tool.definition))

    for tool_cls in APPKIT_V2_TOOLS:
        tool = tool_cls()
        defs.append((f"appkit_v2:{tool.definition.name}", tool.definition))

    for tool in workflow_router_tools(
        store=EmptyWorkflowStore(),
        phase_state=WorkflowPhaseState(),
        mcp_tool_names_getter=lambda: frozenset(),
    ):
        defs.append((f"workflow_router:{tool.definition.name}", tool.definition))
    return defs


def main() -> int:
    violations: list[Violation] = []
    definitions = _tool_defs()
    for label, definition in definitions:
        schema = _inline_schema_refs(definition.args_model.model_json_schema())
        violations.extend(_walk_schema(schema, label=label, path="$", is_root=True))

    blocked = [v for v in violations if v.location not in ALLOW_SCHEMA_HOLES]
    if blocked:
        print("TOOL SCHEMA FAIL - blind tool argument schema(s) found:\n")
        for violation in blocked:
            print(f"  - {violation.location}: {violation.kind} ({violation.detail})")
        print(
            "\nFix: replace bare dict/list[Any] surfaces with typed Pydantic models or "
            "typed item schemas. Only add ALLOW_SCHEMA_HOLES entries for genuinely "
            "free-form JSON, with a concrete reason."
        )
        return 1

    print(
        "TOOL SCHEMA OK - checked "
        f"{len(definitions)} args_model schema(s); no bare-object params or "
        "untyped array items."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
