"""Search / extract tools — tool-sandbox-contract.md §9.

Thin wrappers over the retrieval providers (the retrieval contract, separate
doc). [CONTRACT] they reach providers via the orchestrator-mediated CAPABILITY
(§6) — the provider key never enters the tool/sandbox. They run `in_process`
(mediated calls, no agent code), so they need no raw guest egress.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..anatomy import Capability, ToolContext, ToolDef, ToolOutcome

_NET = frozenset({Capability.NETWORK})


class SearchArgs(BaseModel):
    query: str = Field(description="Search query.")
    limit: int = Field(default=5, ge=1, le=50)


class SearchTool:
    definition = ToolDef(
        name="search",
        description="Search the web via the configured provider (SearXNG + optional APIs).",
        args_model=SearchArgs,
        needs=_NET,
        uses_capabilities=frozenset({"search"}),
        runs_in="in_process",  # orchestrator-mediated; provider key stays out of the box
    )

    async def run(self, args: SearchArgs, ctx: ToolContext) -> ToolOutcome:
        results = await ctx.capabilities.call("search", query=args.query, limit=args.limit)
        return ToolOutcome(
            success=True,
            content=str(results),
            structured={"results": results} if isinstance(results, list | dict) else None,
        )


class ExtractArgs(BaseModel):
    url: str = Field(description="URL to extract clean content from.")


class ExtractTool:
    definition = ToolDef(
        name="extract",
        description="Extract clean, LLM-ready content from a URL (via Firecrawl).",
        args_model=ExtractArgs,
        needs=_NET,
        uses_capabilities=frozenset({"extract"}),
        runs_in="in_process",
    )

    async def run(self, args: ExtractArgs, ctx: ToolContext) -> ToolOutcome:
        content = await ctx.capabilities.call("extract", url=args.url)
        return ToolOutcome(success=True, content=str(content))
