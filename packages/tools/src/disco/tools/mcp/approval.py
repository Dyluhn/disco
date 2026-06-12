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
    tools: list[dict[str, str]],  # [{"name": ..., "description": ...}, ...]
) -> str:
    """SHA-256 of a canonicalized JSON blob of sorted tool descriptions.

    Deterministic regardless of the order of tools in the input list or
    the order of keys in each tool dict.
    """
    canonical = [
        {"name": t["name"], "description": t["description"]} for t in tools
    ]
    canonical.sort(key=lambda t: t["name"])
    blob = json.dumps(canonical, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()
