"""Search / extract tools — tool-sandbox-contract.md §9.

Thin wrappers over the retrieval providers (the retrieval contract, separate
doc). [CONTRACT] they reach providers via the orchestrator-mediated CAPABILITY
(§6) — the provider key never enters the tool/sandbox. They run `in_process`
(mediated calls, no agent code), so they need no raw guest egress.
"""

from __future__ import annotations

from typing import Any

from disco.core.effects import EffectCapability
from pydantic import BaseModel, Field

from ..anatomy import Capability, ToolContext, ToolDef, ToolOutcome
from ..behavior import declares

_NET = frozenset({Capability.NETWORK})

# B3 — the agent reads these results directly, so they must be CLEAN MARKDOWN, not
# a Python repr (`str(list_of_dicts)`), which pollutes the driver's context with
# braces/quotes and burns tokens. Extraction is also length-budgeted so one big
# page can't blow the window (the Observation layer snips at 8k too).
_EXTRACT_CHAR_BUDGET = 6000


def _search_markdown(query: str, results: Any) -> str:
    """A compact ranked list: `N. [title](url) — snippet`. Discovery only — the
    snippets are the provider's, not trusted as content."""
    if not isinstance(results, list) or not results:
        return f'No results for "{query}".'
    lines = [f'Search results for "{query}":', ""]
    for i, h in enumerate(results, 1):
        h = h if isinstance(h, dict) else {}
        url = str(h.get("url", "")).strip()
        title = str(h.get("title") or url or "untitled").strip()
        snippet = " ".join(str(h.get("snippet", "")).split())
        line = f"{i}. [{title}]({url})" if url else f"{i}. {title}"
        if snippet:
            line += f" — {snippet}"
        lines.append(line)
    return "\n".join(lines)


def _extract_markdown(doc: Any) -> tuple[str, bool, str | None]:
    """The page as markdown: `# title` + source URL + content (budgeted). Returns
    (markdown, ok, error)."""
    # A bare string (some providers/handlers return just the content) is the body.
    if isinstance(doc, str):
        body = doc.strip()
        if len(body) > _EXTRACT_CHAR_BUDGET:
            body = (
                body[:_EXTRACT_CHAR_BUDGET] + f"\n\n…[truncated at {_EXTRACT_CHAR_BUDGET:,} chars]"
            )
        return (body, True, None)
    d = doc if isinstance(doc, dict) else {}
    url = str(d.get("url", "")).strip()
    if d.get("fetched_ok") is False or d.get("status") not in (None, "ok"):
        err = str(d.get("error") or d.get("status") or "extraction failed")
        return (f"Could not extract <{url}>: {err}", False, err)
    title = str(d.get("title") or url or "Untitled").strip()
    content = str(d.get("content", "")).strip()
    if len(content) > _EXTRACT_CHAR_BUDGET:
        content = (
            content[:_EXTRACT_CHAR_BUDGET] + f"\n\n…[truncated at {_EXTRACT_CHAR_BUDGET:,} chars — "
            "re-extract a deeper section if you need more]"
        )
    body = f"# {title}\n<{url}>\n\n{content}" if url else f"# {title}\n\n{content}"
    return (body, True, None)


class SearchArgs(BaseModel):
    query: str = Field(description="Search query.")
    limit: int = Field(default=5, ge=1, le=50)


class SearchTool:
    definition = ToolDef(
        name="search",
        description=(
            "Search the web via the configured provider (SearXNG + optional APIs). "
            "Returns titles + URLs + snippets — follow up with `extract` on a chosen "
            "URL for full readable content."
        ),
        args_model=SearchArgs,
        needs=_NET,
        uses_capabilities=frozenset({"search"}),
        runs_in="in_process",  # orchestrator-mediated; provider key stays out of the box
        read_only=True,  # observes only — safe for the planner to gather context
        behavior=declares(EffectCapability.EXTERNAL_OBSERVE, planner_safe=True),
    )

    async def run(self, args: SearchArgs, ctx: ToolContext) -> ToolOutcome:
        results = await ctx.capabilities.call("search", query=args.query, limit=args.limit)
        return ToolOutcome(
            success=True,
            content=_search_markdown(args.query, results),  # clean markdown, not str()
            structured={"results": results} if isinstance(results, list | dict) else None,
        )


class ExtractArgs(BaseModel):
    url: str = Field(description="URL to extract clean content from.")


class ExtractTool:
    definition = ToolDef(
        name="extract",
        description=(
            "Extract clean, LLM-ready content from ONE URL (via Firecrawl) — the "
            "follow-up to `search` when a snippet is not enough."
        ),
        args_model=ExtractArgs,
        needs=_NET,
        uses_capabilities=frozenset({"extract"}),
        runs_in="in_process",
        read_only=True,  # observes only — safe for the planner to gather context
        behavior=declares(EffectCapability.EXTERNAL_OBSERVE, planner_safe=True),
    )

    async def run(self, args: ExtractArgs, ctx: ToolContext) -> ToolOutcome:
        doc = await ctx.capabilities.call("extract", url=args.url)
        md, ok, error = _extract_markdown(doc)
        return ToolOutcome(
            success=ok,
            content=md,  # clean markdown (title + url + budgeted body), not str()
            structured={"doc": doc} if isinstance(doc, dict) else None,
            error=error,
        )
