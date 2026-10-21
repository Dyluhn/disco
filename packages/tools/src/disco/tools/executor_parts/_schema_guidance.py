"""Bounded repair guidance from authored schema descriptions, never rejected values."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


def _resolve(node: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    for _ in range(8):
        reference = node.get("$ref", "")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            target = root.get("$defs", {}).get(reference.removeprefix("#/$defs/"))
            if not isinstance(target, dict):
                break
            node = {**target, **{key: value for key, value in node.items() if key != "$ref"}}
            continue
        choices = node.get("anyOf", node.get("oneOf", []))
        concrete = [
            item for item in choices if isinstance(item, dict) and item.get("type") != "null"
        ]
        if len(concrete) != 1:
            break  # Never guess which branch of an ambiguous union failed.
        node = {
            **concrete[0],
            **{key: value for key, value in node.items() if key not in {"anyOf", "oneOf"}},
        }
    return node


def _node_at(root: dict[str, Any], location: tuple) -> tuple[dict[str, Any], str] | None:
    node, path = root, []
    for part in location:
        node = _resolve(node, root)
        if isinstance(part, int) and isinstance(node.get("items"), dict):
            node = node["items"]
            path.append("[*]")
        elif isinstance(part, str) and part in node.get("properties", {}):
            node = node["properties"][part]
            path.append(part)
        else:
            return None
    return _resolve(node, root), ".".join(path).replace(".[*]", "[*]") or "(root)"


def _descriptions(node: dict[str, Any], path: str):
    yield path, node.get("description")
    for name, field in node.get("properties", {}).items():
        if isinstance(field, dict):
            yield f"{path}.{name}", field.get("description")


def _authored_schema_hints(args_model: type[BaseModel], errors: list[dict[str, Any]]) -> str:
    """Resolve only reported fields/models; collapse indices and cap guidance."""
    root = args_model.model_json_schema()
    hints: dict[str, str] = {}
    for error in errors[:8]:
        resolved = _node_at(root, tuple(error.get("loc", ())))
        if resolved is None:
            continue
        for path, description in _descriptions(*resolved):
            if not isinstance(description, str) or not description.strip():
                continue
            if path in hints:
                continue
            text = " ".join(description.split())
            hints[path] = f"{path[:120]}: {text[:380]}{'…' if len(text) > 380 else ''}"
            if len(hints) == 4:
                return " Schema guidance: " + " ".join(hints.values())
    return " Schema guidance: " + " ".join(hints.values()) if hints else ""
