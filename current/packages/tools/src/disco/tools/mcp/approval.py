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
from enum import Enum
from typing import Any


class ApprovalRequired(Exception):
    """Raised when a server's tool descriptions changed since last approval."""

    def __init__(self, server: str, old_hash: str, new_hash: str) -> None:
        super().__init__(
            f"MCP server {server!r}: description_hash changed "
            f"({old_hash[:12]}… → {new_hash[:12]}…) — re-approval required"
        )
        self.server = server
        self.old_hash = old_hash
        self.new_hash = new_hash


class ConfigApprovalRequired(Exception):
    """Raised before connect when an MCP server config is not approved."""

    def __init__(self, server: str, old_hash: str, new_hash: str) -> None:
        super().__init__(
            f"MCP server {server!r}: configuration approval required "
            f"({old_hash[:12] or 'none'} -> {new_hash[:12]})"
        )
        self.server = server
        self.old_hash = old_hash
        self.new_hash = new_hash


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
    """SHA-256 of canonical tool names, descriptions, and input schemas.

    Deterministic regardless of the order of tools in the input list or
    the order of keys in each tool dict.
    """
    canonical = [
        {
            "name": t["name"],
            "description": t.get("description", ""),
            "inputSchema": t.get("inputSchema", {}),
        }
        for t in tools
    ]
    canonical.sort(key=lambda t: t["name"])
    blob = json.dumps(canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def compute_config_hash(server: Any) -> str:
    """Fingerprint every security-relevant field before a server is touched.

    Values in ``env``/``headers`` are secret reference identifiers, never the
    resolved secrets. ``enabled`` is intentionally excluded: toggling the same
    approved server does not change what will execute when enabled.
    """
    raw = server.model_dump(mode="json") if hasattr(server, "model_dump") else dict(server)

    def _text(value: Any, *, upper: bool = False) -> str:
        if isinstance(value, Enum):
            value = value.value
        result = str(value or "")
        return result.upper() if upper else result

    def _ordered_text(value: Any) -> list[str]:
        if not isinstance(value, (list, tuple, set, frozenset)):
            return []
        return sorted({_text(item) for item in value})

    def _ordered_refs(value: Any) -> dict[str, str]:
        if not isinstance(value, dict):
            return {}
        return {str(key): _text(value[key]) for key in sorted(value, key=str)}

    # Normalize the loose app-server dictionary and the typed agent-server
    # model to exactly the same representation. In particular, Settings stores
    # risk tiers as lower-case strings while SecurityRisk serializes upper-case.
    # A mismatch here would make a legitimately approved config impossible to
    # start, so the cross-representation invariant is security-critical.
    canonical = {
        "name": _text(raw.get("name")),
        "transport": _text(raw.get("transport")).lower(),
        # Command/argument order changes what is executed, so preserve it.
        "command": [_text(item) for item in (raw.get("command") or [])],
        "args": [_text(item) for item in (raw.get("args") or [])],
        "url": _text(raw.get("url")),
        # Only secret-reference identifiers cross this boundary; resolved
        # secret values are deliberately never fingerprinted or surfaced.
        "env": _ordered_refs(raw.get("env")),
        "headers": _ordered_refs(raw.get("headers")),
        # These fields are set-like at execution time. Sorting/deduplicating
        # avoids spurious approval churn while still pinning their semantics.
        "allowed_hosts": _ordered_text(raw.get("allowed_hosts")),
        "allowed_tools": _ordered_text(raw.get("allowed_tools")),
        "risk_tier": _text(raw.get("risk_tier"), upper=True),
    }
    blob = json.dumps(canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()
