"""MCP retrieval-tier registry — RP-05 rung B.

Wraps shape-matching MCP tools as SearchProvider / ExtractionProvider satisfying
the @runtime_checkable Protocols in retrieval/providers.py. Registers them with
the build broker at conversation start, so citations through the MCP tier flow
the SAME GroundingPipeline as bundled providers.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from disco.retrieval.models import ExtractedDoc, SearchHit

_LOG = logging.getLogger(__name__)

# Tool name patterns that match the search / fetch shape.
# The brief says "tools whose names match the OpenAI search(query)→..."
# We look for tools named exactly "search" or "fetch" (or prefixed variants),
# with input schemas containing "query" / "id" respectively.
_SEARCH_TOOL_NAMES: frozenset[str] = frozenset({"search", "web_search", "brave_search"})
_FETCH_TOOL_NAMES: frozenset[str] = frozenset({"fetch", "web_fetch", "fetch_url", "extract"})


class _MCPRetrievalSearchProvider:
    """Adapts a shape-compatible MCP tool into a SearchProvider.

    The MCP tool must accept `{"query": str}` and return results with
    `{results: [{id, title, url}, ...]}`.
    """
    name: str

    def __init__(
        self, server: str, tool_name: str, call_fn: Any, description: str = ""
    ) -> None:
        self.name = f"mcp__{server}__{tool_name}"
        self._server = server
        self._tool_name = tool_name
        self._call = call_fn
        self._description = description

    async def search(
        self,
        query: str,
        *,
        limit: int = 10,
        domains_allow: frozenset[str] | None = None,
        domains_deny: frozenset[str] | None = None,
    ) -> list[SearchHit]:
        """Call the MCP search tool and convert results to SearchHits."""
        try:
            raw = await self._call(self._server, self._tool_name, {"query": query})
        except Exception as exc:
            _LOG.warning(
                "MCP search %r failed: %s", self.name, exc
            )
            return []

        # Try to extract results from the MCP response.
        # The MCP tool returns {"content": [{"text": ...}, ...]} — we parse
        # the text as JSON and look for a "results" key.
        content_text = _extract_text(raw)
        if not content_text:
            return []

        try:
            parsed = json.loads(content_text)
        except (json.JSONDecodeError, TypeError):
            return []

        if isinstance(parsed, dict) and "results" in parsed:
            items = parsed["results"]
        elif isinstance(parsed, list):
            items = parsed
        else:
            return []

        hits: list[SearchHit] = []
        for item in items[:limit]:
            if isinstance(item, dict):
                hits.append(
                    SearchHit(
                        url=str(item.get("url", "")),
                        title=str(item.get("title", "")),
                        snippet=str(item.get("snippet", item.get("description", ""))),
                        source_engine=self.name,
                        rank=0,
                    )
                )
        return hits


class _MCPRetrievalExtractionProvider:
    """Adapts a shape-compatible MCP tool into an ExtractionProvider.

    The MCP tool must accept `{"id": str}` or `{"url": str}` and return
    document content.
    """
    name: str

    def __init__(
        self, server: str, tool_name: str, call_fn: Any, description: str = ""
    ) -> None:
        self.name = f"mcp__{server}__{tool_name}"
        self._server = server
        self._tool_name = tool_name
        self._call = call_fn
        self._description = description

    async def extract(self, url: str) -> ExtractedDoc:
        """Call the MCP extraction tool and return an ExtractedDoc."""
        try:
            raw = await self._call(self._server, self._tool_name, {"id": url})
        except Exception as exc:
            _LOG.warning(
                "MCP extract %r failed: %s", self.name, exc
            )
            return ExtractedDoc(
                url=url,
                title="",
                content=f"Extraction failed: {exc}",
            )

        content_text = _extract_text(raw)
        return ExtractedDoc(
            url=url,
            title="",
            content=content_text or "",
        )

    async def extract_many(self, urls: list[str]) -> list[ExtractedDoc]:
        """Call extract for each URL (no batching)."""
        return [await self.extract(u) for u in urls]


def _extract_text(raw: dict) -> str:
    """Extract text content from an MCP tool result dict."""
    parts = []
    for item in raw.get("content", []):
        if hasattr(item, "text"):
            parts.append(item.text)
        elif isinstance(item, dict) and "text" in item:
            parts.append(item["text"])
    return "\n".join(parts)


def _tool_matches_search_shape(tool_def: Any) -> bool:
    """Check if a tool's name and schema match the search shape.

    Name must be in _SEARCH_TOOL_NAMES, and the inputSchema must have a
    "query" property.
    """
    # Check by name if we have the raw MCP tool
    name = getattr(tool_def, "name", "") or ""
    if name in _SEARCH_TOOL_NAMES:
        return True
    # Also check if name starts with common prefixes
    if any(name.startswith(p) for p in ("search", "web_search", "brave_search")):
        return True
    return False


def _tool_matches_fetch_shape(tool_def: Any) -> bool:
    """Check if a tool's name and schema match the fetch shape.

    Name must be in _FETCH_TOOL_NAMES, and the inputSchema must have an
    "id" or "url" property.
    """
    name = getattr(tool_def, "name", "") or ""
    if name in _FETCH_TOOL_NAMES:
        return True
    if any(name.startswith(p) for p in ("fetch", "web_fetch", "fetch_url", "extract")):
        return True
    return False


def build_retrieval_providers(
    mcp_tools: list[dict[str, Any]],
    *,
    call_fn: Any,  # McpPool.call_tool or similar
) -> tuple[list, list]:
    """Scan the MCP tool list for search/fetch-shaped tools and return
    (search_providers, extraction_providers) lists.

    Each provider implements the @runtime_checkable Protocol from
    retrieval/providers.py. The caller registers them via _build_broker
    into the existing GroundingPipeline.

    Args:
        mcp_tools: List of dicts with keys "server", "tool_name", "tool" (the raw
                   MCP tool object with .name and .inputSchema).
        call_fn: Async callable (server, tool_name, arguments) -> result dict.

    Returns:
        (list of SearchProvider, list of ExtractionProvider)
    """
    search_providers = []
    extraction_providers = []

    for entry in mcp_tools:
        server = entry["server"]
        tool_name = entry["tool_name"]
        tool_obj = entry["tool"]

        if _tool_matches_search_shape(tool_obj):
            provider = _MCPRetrievalSearchProvider(
                server, tool_name, call_fn,
                description=getattr(tool_obj, "description", "") or "",
            )
            search_providers.append(provider)
            _LOG.debug(
                "MCP retrieval: registered search provider %r", provider.name
            )

        if _tool_matches_fetch_shape(tool_obj):
            provider = _MCPRetrievalExtractionProvider(
                server, tool_name, call_fn,
                description=getattr(tool_obj, "description", "") or "",
            )
            extraction_providers.append(provider)
            _LOG.debug(
                "MCP retrieval: registered extraction provider %r", provider.name
            )

    return search_providers, extraction_providers


class CompositeSearchProvider:
    """Fan-out SearchProvider: queries the primary (bundled) provider AND every
    MCP retrieval provider, merging their hits (deduped by URL, primary first).

    This is the join that makes MCP-discovered URLs flow through the SAME
    GroundingPipeline as bundled hits (workorder §3) — the composite is what the
    runtime hands to `stream_research_answer` / `retrieval_capability_handlers`,
    so MCP hits get reranked, extracted, NLI-verified and cited identically.
    """

    name = "composite"

    def __init__(self, primary: Any, extras: list) -> None:
        # primary first so bundled results rank ahead on ties; extras are the MCP
        # providers. A composite with no extras behaves exactly like `primary`.
        self._providers = [primary, *extras]

    async def search(
        self,
        query: str,
        *,
        limit: int = 10,
        domains_allow: frozenset[str] | None = None,
        domains_deny: frozenset[str] | None = None,
    ) -> list[SearchHit]:
        merged: list[SearchHit] = []
        seen: set[str] = set()
        for provider in self._providers:
            try:
                hits = await provider.search(
                    query,
                    limit=limit,
                    domains_allow=domains_allow,
                    domains_deny=domains_deny,
                )
            except TypeError:
                # An MCP provider with a narrower signature — call positionally.
                hits = await provider.search(query, limit=limit)
            except Exception as exc:  # noqa: BLE001 — one bad provider must not sink discovery
                _LOG.warning(
                    "Composite search: provider %r failed: %s",
                    getattr(provider, "name", "?"), exc,
                )
                continue
            for hit in hits:
                if hit.url and hit.url in seen:
                    continue
                if hit.url:
                    seen.add(hit.url)
                merged.append(hit)
        return merged


class CompositeExtractionProvider:
    """Fallback-chain ExtractionProvider: tries the primary (bundled) extractor
    first; if it yields no usable content, falls through to the MCP extraction
    providers. extract_many maps extract over the URLs."""

    name = "composite"

    def __init__(self, primary: Any, extras: list) -> None:
        self._providers = [primary, *extras]

    async def extract(self, url: str) -> ExtractedDoc:
        last: ExtractedDoc | None = None
        for provider in self._providers:
            try:
                doc = await provider.extract(url)
            except Exception as exc:  # noqa: BLE001
                _LOG.warning(
                    "Composite extract: provider %r failed: %s",
                    getattr(provider, "name", "?"), exc,
                )
                continue
            last = doc
            if getattr(doc, "fetched_ok", False) or (doc.content or "").strip():
                return doc
        # Every provider failed / returned empty — surface the last attempt (or a
        # miss) so the caller still gets a well-formed ExtractedDoc.
        return last or ExtractedDoc(url=url, title="", content="")

    async def extract_many(self, urls: list[str]) -> list[ExtractedDoc]:
        return [await self.extract(u) for u in urls]


def compose_with_mcp(
    primary_search: Any,
    primary_extraction: Any,
    *,
    mcp_searches: list,
    mcp_extractions: list,
) -> tuple[Any, Any]:
    """Wrap the bundled search/extraction providers with their MCP counterparts.

    When there are no MCP providers, returns the primaries UNCHANGED (no wrapper
    overhead, identical behavior) — so the composition is inert until an MCP
    server with search/fetch-shaped tools is actually configured.
    """
    search = (
        CompositeSearchProvider(primary_search, mcp_searches)
        if mcp_searches
        else primary_search
    )
    extraction = (
        CompositeExtractionProvider(primary_extraction, mcp_extractions)
        if mcp_extractions
        else primary_extraction
    )
    return search, extraction
