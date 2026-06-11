"""MCP tool naming — RP-05 rung A.

qualified_name(server, tool) -> "mcp__<server>__<tool>" with [a-z0-9_] enforcement.
split_qualified_name(qn) -> (server, tool) | None for the reverse mapping.
Both reject names that would not round-trip (no double underscores inside
components, etc.).
"""

from __future__ import annotations

import re

# A tool's qualified name: mcp__<server>__<tool>. The server name MUST match
# the [a-z0-9_] regex (enforced at McpServerConfig parse time). The tool name
# is whatever the server reports — we only reject tool names containing double
# underscores (which would make round-tripping ambiguous).
#
# Example: mcp__filesystem__read_file splits → ("filesystem", "read_file")
#
# The double-underscore separator was chosen over colon/period because those
# characters collide with LLM tool-name tokenisation (colons are often split
# into separate tokens) and the `__` convention mirrors Python's name-mangling
# which the model already understands.

_DOUBLE_UNDER_RE = re.compile(r"__")


def qualified_name(server: str, tool: str) -> str:
    """Build the qualified tool name: mcp__<server>__<tool>.

    Raises ValueError if either component contains double underscores
    (which would break the round-trip guarantee).
    """
    if _DOUBLE_UNDER_RE.search(server):
        raise ValueError(f"MCP server name {server!r} contains double underscores")
    if _DOUBLE_UNDER_RE.search(tool):
        raise ValueError(f"MCP tool name {tool!r} contains double underscores")
    return f"mcp__{server}__{tool}"


def split_qualified_name(qn: str) -> tuple[str, str] | None:
    """Reverse of qualified_name(): returns (server, tool) or None.

    Returns None for any string that is not a valid qualified MCP name
    (doesn't start with 'mcp__', doesn't have exactly three parts
    separated by '__', or any part is empty).
    """
    if not qn.startswith("mcp__"):
        return None
    # Strip the prefix and split on remaining __
    rest = qn[5:]  # remove "mcp__"
    parts = rest.split("__", 1)
    if len(parts) != 2:
        return None
    server, tool = parts
    if not server or not tool:
        return None
    # Double-underscores inside components are rejected at construction time;
    # here they'd already cause wrong splits, but we keep the guard.
    if _DOUBLE_UNDER_RE.search(server) or _DOUBLE_UNDER_RE.search(tool):
        return None
    return server, tool


def is_mcp_qualified(name: str) -> bool:
    """True iff *name* is an MCP-qualified tool name."""
    return name.startswith("mcp__") and split_qualified_name(name) is not None
