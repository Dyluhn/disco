"""MCP retrieval-tier registry — RP-05 rung B.

Wraps shape-matching MCP tools as SearchProvider / ExtractionProvider satisfying
the @runtime_checkable Protocols in retrieval/providers.py. Registers them with
the build broker at conversation start, so citations through the MCP tier flow
the SAME GroundingPipeline as bundled providers.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

from disco.retrieval.bundled_providers import chunk_passages
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
        self,
        server: str,
        tool_name: str,
        call_fn: Any,
        description: str = "",
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
        time_filter: str | None = None,
    ) -> list[SearchHit]:
        """Call the MCP search tool and convert results to SearchHits."""
        try:
            raw = await self._call(self._server, self._tool_name, {"query": query})
        except Exception as exc:
            _LOG.warning("MCP search %r failed: %s", self.name, exc)
            return []
        if raw.get("isError") or raw.get("is_error"):
            _LOG.warning("MCP search %r returned an error result", self.name)
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
        if not isinstance(items, list):
            return []

        hits: list[SearchHit] = []
        for item in items[:limit]:
            if isinstance(item, dict) and item.get("url"):
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
        self,
        server: str,
        tool_name: str,
        call_fn: Any,
        description: str = "",
        argument_name: str = "id",
    ) -> None:
        self.name = f"mcp__{server}__{tool_name}"
        self._server = server
        self._tool_name = tool_name
        self._call = call_fn
        self._description = description
        self._argument_name = argument_name

    async def extract(self, url: str) -> ExtractedDoc:
        """Call the MCP extraction tool and return an ExtractedDoc."""
        try:
            raw = await self._call(self._server, self._tool_name, {self._argument_name: url})
        except Exception as exc:
            _LOG.warning("MCP extract %r failed: %s", self.name, exc)
            return ExtractedDoc(
                url=url,
                title="",
                content=f"Extraction failed: {exc}",
            )

        content_text = _extract_text(raw)
        if raw.get("isError") or raw.get("is_error"):
            error = content_text.strip() or "MCP extraction tool returned an error"
            return ExtractedDoc(
                url=url,
                title="",
                content="",
                passages=[],
                fetched_ok=False,
                error=error,
                status="error",
            )
        content = content_text.strip()
        return ExtractedDoc(
            url=url,
            title=url,
            content=content,
            passages=chunk_passages(url, url, content),
            fetched_ok=bool(content),
            error=None if content else "MCP extraction tool returned no content",
            status="ok" if content else "error",
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


def _tool_input_fields(tool_def: Any) -> frozenset[str]:
    """Return the advertised argument names from either a raw MCP Tool or ToolDef.

    Runtime registration converts raw MCP tools into Disco ``ToolDef`` objects,
    so retrieval discovery must understand both shapes.  Looking only at the
    tool name can register an incompatible tool and then repeatedly call it with
    syntax it never advertised.
    """
    schema = getattr(tool_def, "inputSchema", None)
    if not isinstance(schema, Mapping):
        schema = getattr(tool_def, "input_schema", None)
    if isinstance(schema, Mapping):
        properties = schema.get("properties")
        if isinstance(properties, Mapping):
            return frozenset(str(name) for name in properties)

    args_model = getattr(tool_def, "args_model", None)
    model_fields = getattr(args_model, "model_fields", None)
    if isinstance(model_fields, Mapping):
        return frozenset(str(name) for name in model_fields)
    return frozenset()


def _tool_matches_search_shape(tool_def: Any, tool_name: str | None = None) -> bool:
    """Check if a tool's name and schema match the search shape.

    Name must be in _SEARCH_TOOL_NAMES, and the inputSchema must have a
    "query" property.
    """
    # Check by name if we have the raw MCP tool
    name = tool_name or getattr(tool_def, "name", "") or ""
    name_matches = name in _SEARCH_TOOL_NAMES
    # Also check if name starts with common prefixes
    name_matches = name_matches or any(
        name.startswith(p) for p in ("search", "web_search", "brave_search")
    )
    return name_matches and "query" in _tool_input_fields(tool_def)


def _tool_matches_fetch_shape(tool_def: Any, tool_name: str | None = None) -> bool:
    """Check if a tool's name and schema match the fetch shape.

    Name must be in _FETCH_TOOL_NAMES, and the inputSchema must have an
    "id" or "url" property.
    """
    name = tool_name or getattr(tool_def, "name", "") or ""
    name_matches = name in _FETCH_TOOL_NAMES
    name_matches = name_matches or any(
        name.startswith(p) for p in ("fetch", "web_fetch", "fetch_url", "extract")
    )
    return name_matches and bool(_tool_input_fields(tool_def) & {"id", "url"})


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

        if _tool_matches_search_shape(tool_obj, tool_name):
            provider = _MCPRetrievalSearchProvider(
                server,
                tool_name,
                call_fn,
                description=getattr(tool_obj, "description", "") or "",
            )
            search_providers.append(provider)
            _LOG.debug("MCP retrieval: registered search provider %r", provider.name)

        if _tool_matches_fetch_shape(tool_obj, tool_name):
            fields = _tool_input_fields(tool_obj)
            argument_name = "id" if "id" in fields else "url"
            provider = _MCPRetrievalExtractionProvider(
                server,
                tool_name,
                call_fn,
                description=getattr(tool_obj, "description", "") or "",
                argument_name=argument_name,
            )
            extraction_providers.append(provider)
            _LOG.debug("MCP retrieval: registered extraction provider %r", provider.name)

    return search_providers, extraction_providers


class CompositeSearchProvider:
    """Fan-out SearchProvider: queries the primary (bundled) provider AND every
    MCP retrieval provider, fairly merging their hits (deduped by URL).

    This is the join that makes MCP-discovered URLs flow through the SAME
    GroundingPipeline as bundled hits (workorder §3) — the composite is what the
    runtime hands to `stream_research_answer` / `retrieval_capability_handlers`,
    so MCP hits get reranked, extracted, NLI-verified and cited identically.
    """

    name = "composite"

    def __init__(self, primary: Any, extras: list) -> None:
        # Primary remains first within each rank bucket, while round-robin merge
        # guarantees an MCP provider cannot be permanently pushed past the
        # downstream extraction cap by a full page of bundled results.
        self._providers = [primary, *extras]

    async def search(
        self,
        query: str,
        *,
        limit: int = 10,
        domains_allow: frozenset[str] | None = None,
        domains_deny: frozenset[str] | None = None,
        time_filter: str | None = None,
    ) -> list[SearchHit]:
        batches: list[tuple[Any, list[SearchHit]]] = []
        for provider in self._providers:
            try:
                hits = await provider.search(
                    query,
                    limit=limit,
                    domains_allow=domains_allow,
                    domains_deny=domains_deny,
                    time_filter=time_filter,
                )
            except TypeError:
                # An MCP provider with a narrower signature — call positionally.
                hits = await provider.search(query, limit=limit)
            except Exception as exc:  # noqa: BLE001 — one bad provider must not sink discovery
                _LOG.warning(
                    "Composite search: provider %r failed: %s",
                    getattr(provider, "name", "?"),
                    exc,
                )
                continue
            batches.append((provider, hits))

        merged: list[SearchHit] = []
        seen: dict[str, int] = {}
        max_batch = max((len(hits) for _provider, hits in batches), default=0)
        for rank in range(max_batch):
            for provider, hits in batches:
                if rank >= len(hits):
                    continue
                hit = hits[rank]
                if hit.url and hit.url in seen:
                    # Dedup the candidate URL, but not its provenance. Provider
                    # affinity is required downstream: an MCP fetch tool may be
                    # the only extractor able to read a URL that bundled search
                    # also happened to discover.
                    index = seen[hit.url]
                    existing = merged[index]
                    source = hit.source_engine or getattr(provider, "name", "")
                    engines = [part for part in existing.source_engine.split("|") if part]
                    if source and source not in engines:
                        merged[index] = existing.model_copy(
                            update={"source_engine": "|".join([*engines, source])}
                        )
                    continue
                if hit.url:
                    seen[hit.url] = len(merged)
                merged.append(hit)
            # Finish the whole rank bucket before truncating so a duplicate from
            # a later provider can still attach provenance to an included hit.
            if len(merged) >= limit:
                return merged[:limit]
        return merged[:limit]


class CompositeExtractionProvider:
    """Fallback-chain ExtractionProvider: tries the primary (bundled) extractor
    first; if it yields no usable content, falls through to the MCP extraction
    providers. extract_many maps extract over the URLs."""

    name = "composite"

    def __init__(self, primary: Any, extras: list) -> None:
        self._providers = [primary, *extras]

    async def _extract_with(self, url: str, providers: list[Any]) -> ExtractedDoc:
        last: ExtractedDoc | None = None
        for provider in providers:
            try:
                doc = await provider.extract(url)
            except Exception as exc:  # noqa: BLE001
                _LOG.warning(
                    "Composite extract: provider %r failed: %s",
                    getattr(provider, "name", "?"),
                    exc,
                )
                continue
            last = doc
            if getattr(doc, "fetched_ok", False) or (doc.content or "").strip():
                return doc
        # Every provider failed / returned empty — surface the last attempt (or a
        # miss) so the caller still gets a well-formed ExtractedDoc.
        return last or ExtractedDoc(url=url, title="", content="")

    async def extract(self, url: str) -> ExtractedDoc:
        return await self._extract_with(url, self._providers)

    async def extract_many(self, urls: list[str]) -> list[ExtractedDoc]:
        return [await self.extract(u) for u in urls]

    async def extract_hits(self, hits: list[SearchHit]) -> list[ExtractedDoc]:
        """Extract hits while preserving discovery-provider affinity.

        A plain ``extract_many(urls)`` loses which search provider supplied each
        URL. For an MCP-discovered hit, try fetch-shaped tools from that same MCP
        server first, then retain the existing primary/other fallback chain.
        This avoids both false bundled attribution and calling every MCP fetch
        tool for unrelated bundled URLs.
        """
        docs: list[ExtractedDoc] = []
        primary, extras = self._providers[0], self._providers[1:]
        for hit in hits:
            engines = frozenset(part for part in hit.source_engine.split("|") if part)
            matching = [
                provider
                for provider in extras
                if any(
                    engine.startswith(f"mcp__{getattr(provider, '_server', '')}__")
                    for engine in engines
                )
            ]
            if matching:
                remainder = [provider for provider in extras if provider not in matching]
                order = [*matching, primary, *remainder]
            else:
                order = self._providers
            docs.append(await self._extract_with(hit.url, order))
        return docs


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
        CompositeSearchProvider(primary_search, mcp_searches) if mcp_searches else primary_search
    )
    extraction = (
        CompositeExtractionProvider(primary_extraction, mcp_extractions)
        if mcp_extractions
        else primary_extraction
    )
    return search, extraction
