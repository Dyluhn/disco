"""MCP tool approval — RP-05 rung A.

ApprovalRecord stores the SHA-256 hash of a server's canonicalized tool
descriptions so the pool can detect drift (any change → re-approval required).
The approval is per-server: one hash covers ALL tools the server exposes.

The `ApprovalRequired` exception is raised by McpPool.start() when the
current pool's description hash mismatches the stored approval, refusing
to start the affected server until the operator re-approves.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .config import McpServerConfig


class ApprovalRequired(Exception):
    """Raised when an MCP server needs operator approval before use."""

    def __init__(
        self,
        server: str,
        old_hash: str,
        new_hash: str,
        *,
        kind: str = "tool_schema",
    ) -> None:
        label = "approval" if not old_hash else "description_hash changed"
        super().__init__(
            f"MCP server {server!r}: {label} "
            f"({old_hash[:12] or 'none'}… → {new_hash[:12]}…) — "
            f"{kind} re-approval required"
        )
        self.server = server
        self.old_hash = old_hash
        self.new_hash = new_hash
        self.kind = kind


class ApprovalRecord:
    """One server's approval row. Hashed by SHA-256 over canonicalized tool
    descriptions (sorted tool-names → sorted keys → JSON, deterministic)."""

    def __init__(
        self,
        server: str,
        tool_names: list[str],
        description_hash: str,
        approved_at: str,
        approved_by: str,
    ) -> None:
        self.server = server
        self.tool_names = list(tool_names)
        self.description_hash = description_hash
        self.approved_at = approved_at
        self.approved_by = approved_by


def compute_description_hash(
    tools: list[dict[str, Any]],
) -> str:
    """SHA-256 of sorted tool descriptions and canonical input schemas.

    Deterministic regardless of the order of tools in the input list or
    the order of keys in each tool dict/schema.
    """
    canonical = [
        {
            "name": str(t["name"]),
            "description": str(t.get("description") or ""),
            "inputSchema": _canonical_jsonable(t.get("inputSchema") or {}),
        }
        for t in tools
    ]
    canonical.sort(key=lambda t: t["name"])
    blob = json.dumps(canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def compute_server_config_hash(name: str, srv: McpServerConfig) -> str:
    """SHA-256 of the launch-relevant MCP server config.

    Secret values are deliberately excluded; only SecretRef names are pinned.
    """
    env_refs = srv.env or {}
    header_refs = srv.headers or {}
    canonical = {
        "name": name,
        "transport": srv.transport,
        "command": list(srv.command or []),
        "args": list(srv.args or []),
        "url": (srv.url or "") if srv.transport == "streamable_http" else "",
        "env": {str(k): str(env_refs[k]) for k in sorted(env_refs)},
        "headers": {str(k): str(header_refs[k]) for k in sorted(header_refs)},
        "allowed_hosts": sorted(str(v) for v in (srv.allowed_hosts or [])),
        "allowed_tools": sorted(str(v) for v in (srv.allowed_tools or [])),
        "risk_tier": srv.risk_tier.value,
    }
    blob = json.dumps(canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def _canonical_jsonable(value: Any) -> Any:
    """Return a deterministic JSON-compatible structure for schema hashing."""
    try:
        return json.loads(json.dumps(value, sort_keys=True, ensure_ascii=False))
    except TypeError:
        return json.loads(json.dumps(str(value), ensure_ascii=False))
