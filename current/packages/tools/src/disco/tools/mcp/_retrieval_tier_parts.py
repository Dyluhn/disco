"""Small shape-inspection helpers for MCP retrieval adapters."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def extract_text(raw: dict) -> str:
    """Extract text content from an MCP tool result dict."""
    parts = []
    for item in raw.get("content", []):
        if hasattr(item, "text"):
            parts.append(item.text)
        elif isinstance(item, dict) and "text" in item:
            parts.append(item["text"])
    return "\n".join(parts)


def tool_input_fields(tool_def: Any) -> frozenset[str]:
    """Return advertised argument names from either supported tool shape."""
    schema = getattr(tool_def, "inputSchema", None)
    if not isinstance(schema, Mapping):
        schema = getattr(tool_def, "input_schema", None)
    if isinstance(schema, Mapping):
        properties = schema.get("properties")
        if isinstance(properties, Mapping):
            return frozenset(str(name) for name in properties)

    args_model = getattr(tool_def, "args_model", None)
    model_fields = getattr(args_model, "model_fields", None)
    if isinstance(model_fields, Mapping):
        return frozenset(str(name) for name in model_fields)
    return frozenset()
