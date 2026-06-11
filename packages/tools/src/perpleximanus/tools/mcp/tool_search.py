"""tool_search meta-tool — RP-05 (the big lever).

When the pool has more tools than `max_active_schemas` (default 20), this
single meta-tool is added to the conversation registry instead of the full
list. The LLM calls it to discover tools by keyword; the result is read-only
discovery — the active schema set is never mutated.

WIRING STATUS (rp-05a round-4): this module is the standalone, unit-tested
LIBRARY primitive (see tests/test_mcp_tool_search.py). It is intentionally NOT
yet registered by the agent-server runtime — the active-schema cap that wires
it in requires an advertised-set / callable-set split in ToolScope+executor and
is deferred to order rp-05c. Until then rung A registers all MCP tools eagerly
(every advertised tool stays callable); this primitive waits for rp-05c rather
than being half-wired into a false affordance.
"""

from __future__ import annotations

from typing import Any

from perpleximanus.core import SecurityRisk
from pydantic import BaseModel

from ..anatomy import Capability, ToolDef


class _ToolSearchArgs(BaseModel):
    """The arguments shape for tool_search — what the model sends."""

    model_config = {"frozen": True}

    query: str
    limit: int = 5


def _match_score(query_keywords: list[str], tool_desc: dict[str, Any]) -> int:
    """Cheap keyword-match score. Zero-cost; no embedding model needed."""
    text = f"{tool_desc.get('name','')} {tool_desc.get('description','')}".lower()
    return sum(1 for kw in query_keywords if kw in text)


async def _tool_search_handler(
    query: str,
    limit: int,
    all_tools: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Orchestrator-side search over the FULL tool set.

    Returns: [{name, description, server}, ...] — the qualified names
    + descriptions of the top N matches. Does NOT mutate the active schema.
    """
    keywords = [w.strip().lower() for w in query.split() if w.strip()]
    if not keywords:
        return []
    scored = [
        (tool, _match_score(keywords, tool)) for tool in all_tools
    ]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [
        {
            "name": t["name"],
            "description": t["description"],
            "server": t.get("server", ""),
        }
        for t, s in scored
        if s > 0
    ][:limit]


def meta_tool_search() -> ToolDef:
    """The tool_search meta-tool definition.

    base_risk=LOW, runs_in="in_process", capabilities={} — pure orchestrator-side
    retrieval; no sandbox involvement.
    """
    return ToolDef(
        name="tool_search",
        description=(
            "Return up to N MCP tools whose description matches the query. "
            "Use this when you need a tool not already in your active schema set. "
            "After discovering the right tool, call it by its qualified name "
            "in the next turn."
        ),
        args_model=_ToolSearchArgs,
        needs=frozenset(),
        base_risk=SecurityRisk.LOW,
        runs_in="in_process",
        read_only=True,
        uses_capabilities=frozenset(),
    )
