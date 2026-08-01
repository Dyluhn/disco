"""JSON-schema-shape walking helpers for workflow ``params_model_schema``.

Extracted from :mod:`disco.core.workflow.models` to keep the walker's own
cyclomatic complexity within budget. The original single recursive
``_walk_schema`` folded object-shape checks, array-shape checks, and per-key
dispatch into one function; relocating it verbatim would carry the same branch
graph along, so each schema-node-kind concern now gets its own named helper.
``_walk_schema`` itself is left as a thin recurse-and-delegate dispatcher, with
the branch-heavy shape checks isolated in ``_schema_shape_violations`` and the
per-key dispatch isolated in ``_walk_schema_property``.

Every helper here is a pure function over plain JSON-ish values (``dict`` /
``list`` / ``Any``); nothing depends on the workflow pydantic models, so this
module never imports back to :mod:`..models`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class _SchemaViolation:
    location: str
    kind: str
    detail: str


def _is_permissive_additional_properties(value: object) -> bool:
    return value is True or value == {} or value is None


def _schema_type(schema: dict[str, Any]) -> str | None:
    schema_type = schema.get("type")
    return schema_type if isinstance(schema_type, str) else None


def _schema_shape_violations(node: dict[str, Any], *, path: str) -> list[_SchemaViolation]:
    """Check the node itself (not its children) for the two schema-hole shapes."""
    schema_type = _schema_type(node)
    if schema_type == "object":
        properties = node.get("properties")
        additional = node.get("additionalProperties", True)
        if not properties and _is_permissive_additional_properties(additional):
            return [
                _SchemaViolation(
                    location=path,
                    kind="bare-object",
                    detail="object has no properties and allows arbitrary keys",
                )
            ]
        return []
    if schema_type == "array" and not node.get("items"):
        return [
            _SchemaViolation(
                location=path,
                kind="untyped-array-items",
                detail="array has no item schema",
            )
        ]
    return []


def _walk_schema_properties_map(properties: dict[str, Any], *, path: str) -> list[_SchemaViolation]:
    violations: list[_SchemaViolation] = []
    for prop_name, prop_schema in properties.items():
        violations.extend(_walk_schema(prop_schema, path=f"{path}.{prop_name}"))
    return violations


def _walk_schema_property(key: str, value: object, *, path: str) -> list[_SchemaViolation]:
    """Recurse into a single ``key: value`` pair found on an object-schema node."""
    if key in {"description", "title", "default", "examples"}:
        return []
    if key == "properties" and isinstance(value, dict):
        return _walk_schema_properties_map(value, path=path)
    if key == "items":
        return _walk_schema(value, path=f"{path}[]")
    if isinstance(value, list):
        violations: list[_SchemaViolation] = []
        for index, item in enumerate(value):
            violations.extend(_walk_schema(item, path=f"{path}.{key}[{index}]"))
        return violations
    return _walk_schema(value, path=f"{path}.{key}")


def _walk_schema_object(node: dict[str, Any], *, path: str) -> list[_SchemaViolation]:
    violations = _schema_shape_violations(node, path=path)
    for key, value in node.items():
        violations.extend(_walk_schema_property(key, value, path=path))
    return violations


def _walk_schema_list(node: list[Any], *, path: str) -> list[_SchemaViolation]:
    violations: list[_SchemaViolation] = []
    for index, item in enumerate(node):
        violations.extend(_walk_schema(item, path=f"{path}[{index}]"))
    return violations


def _walk_schema(node: object, *, path: str) -> list[_SchemaViolation]:
    if isinstance(node, dict):
        return _walk_schema_object(node, path=path)
    if isinstance(node, list):
        return _walk_schema_list(node, path=path)
    return []


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
