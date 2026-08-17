"""Typed Build capability access to the research provider graph."""

from __future__ import annotations

from typing import Any

from disco.retrieval.wiring import retrieval_capability_handlers

from .deep_research_service import DeepResearchService
from .mcp_manager import McpManager


class BuildRetrievalCapabilities:
    """Own the lazy search/extract handlers advertised to Build tools."""

    def __init__(
        self,
        research: DeepResearchService,
        mcp: McpManager,
    ) -> None:
        self._research = research
        self._mcp = mcp

    def _resolve(self) -> dict[str, Any]:
        search, extraction = self._mcp._compose_mcp_retrieval(
            self._research._research()
        )
        return retrieval_capability_handlers(search, extraction)

    async def capability_search(self, *, query: str, limit: int = 8) -> object:
        return await self._resolve()["search"](query=query, limit=limit)

    async def capability_extract(self, *, url: str) -> object:
        return await self._resolve()["extract"](url=url)
