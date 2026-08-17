"""Pure MCP-qualified-name and hostname-shape helpers for workflow models.

Extracted from :mod:`disco.core.workflow.models` — string/tuple utilities that
have no dependency on the workflow pydantic models themselves, so this module
never imports back to :mod:`..models`.
"""

from __future__ import annotations

import re

_SERVER_RE = re.compile(r"^[a-z0-9_]+$")
_DOUBLE_UNDER_RE = re.compile(r"__")
_MCP_PREFIX = "mcp__"
_HOST_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def _split_mcp_qualified(name: str) -> tuple[str, str] | None:
    if not name.startswith(_MCP_PREFIX):
        return None
    rest = name[len(_MCP_PREFIX) :]
    parts = rest.split("__", 1)
    if len(parts) != 2:
        return None
    server, tool = parts
    if not server or not tool:
        return None
    if _DOUBLE_UNDER_RE.search(server) or _DOUBLE_UNDER_RE.search(tool):
        return None
    return server, tool


def _qualified_mcp_name(server: str, tool: str) -> str:
    return f"{_MCP_PREFIX}{server}__{tool}"


def _validate_mcp_component(value: str, *, field: str) -> str:
    if not value:
        raise ValueError(f"{field} must be non-empty")
    if _DOUBLE_UNDER_RE.search(value):
        raise ValueError(f"{field} must not contain double underscores")
    return value


def _dedupe(values: tuple[str, ...], *, field: str) -> tuple[str, ...]:
    if len(set(values)) != len(values):
        raise ValueError(f"{field} must not contain duplicates")
    return values


def _normalize_egress_allow_entry(value: str) -> str:
    if not value:
        raise ValueError("egress_allow entries must be non-empty")
    if any(ch.isspace() for ch in value):
        raise ValueError("egress_allow entries must not contain spaces")
    if "://" in value:
        raise ValueError("egress_allow entries must not include scheme://")
    if "/" in value:
        raise ValueError("egress_allow entries must not include /path")

    normalized = value.lower()
    if normalized.startswith("*."):
        normalized = f".{normalized[2:]}"
    suffix = normalized.startswith(".")
    host = normalized[1:] if suffix else normalized
    if not host or host.startswith(".") or host.endswith("."):
        raise ValueError("egress_allow entries must be bare hostnames or *.suffix patterns")
    labels = host.split(".")
    if not all(_HOST_LABEL_RE.match(label) for label in labels):
        raise ValueError("egress_allow entries must be bare hostnames or *.suffix patterns")
    return normalized
