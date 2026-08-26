"""MCP retrieval-tier registry — RP-05 rung B.

Wraps shape-matching MCP tools as SearchProvider / ExtractionProvider satisfying
the @runtime_checkable Protocols in retrieval/providers.py. Registers them with
the build broker at conversation start, so citations through the MCP tier flow
the SAME GroundingPipeline as bundled providers.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast

from disco.retrieval.bundled_providers import chunk_passages
from disco.retrieval.models import ExtractedDoc, SearchHit
from disco.retrieval.source_adapters import _search_diagnostic
from disco.retrieval.url_policy import source_url_key, url_allowed

from ._retrieval_tier_parts import extract_text as _extract_text
from ._retrieval_tier_parts import tool_input_fields as _tool_input_fields

_LOG = logging.getLogger(__name__)

# Tool name patterns that match the search / fetch shape.
# The brief says "tools whose names match the OpenAI search(query)→..."
# We look for tools named exactly "search" or "fetch" (or prefixed variants),
# with input schemas containing "query" / "id" respectively.
_SEARCH_TOOL_NAMES: frozenset[str] = frozenset({"search", "web_search", "brave_search"})
_FETCH_TOOL_NAMES: frozenset[str] = frozenset({"fetch", "web_fetch", "fetch_url", "extract"})


def _first_advertised(input_fields: frozenset[str], candidates: tuple[str, ...]) -> str | None:
    return next((field for field in candidates if field in input_fields), None)


def _add_domain_argument(
    arguments: dict[str, object],
    input_fields: frozenset[str],
    values: frozenset[str] | None,
    candidates: tuple[str, str],
) -> None:
    field = _first_advertised(input_fields, candidates)
    if values and field:
        arguments[field] = sorted(values)


def _mcp_search_arguments(
    input_fields: frozenset[str],
    query: str,
    limit: int,
    domains_allow: frozenset[str] | None,
    domains_deny: frozenset[str] | None,
    time_filter: str | None,
) -> dict[str, object] | None:
    arguments: dict[str, object] = {"query": query}
    limit_field = _first_advertised(input_fields, ("limit", "count", "max_results"))
    if limit_field:
        arguments[limit_field] = limit
    recency_field = _first_advertised(
        input_fields, ("time_filter", "time_range", "recency", "freshness")
    )
    if time_filter and not recency_field:
        return None
    if time_filter and recency_field:
        arguments[recency_field] = (
            {"week": "pw", "month": "pm"}.get(time_filter, time_filter)
            if recency_field == "freshness"
            else time_filter
        )
    _add_domain_argument(
        arguments, input_fields, domains_allow, ("domains_allow", "include_domains")
    )
    _add_domain_argument(arguments, input_fields, domains_deny, ("domains_deny", "exclude_domains"))
    return arguments


def _mcp_result_items(raw: dict, provider_name: str) -> list[dict[str, Any]]:
    """Return valid MCP search rows for the legacy plain-search adapter.

    Detailed callers use ``_mcp_result_items_detailed`` so malformed responses
    remain distinguishable from a valid empty result. Keep this small wrapper
    for the extraction-neutral legacy call sites and tests.
    """
    items, outcome = _mcp_result_items_detailed(raw, provider_name)
    if outcome != "ok" and outcome != "empty":
        return []
    return items


def _mcp_result_items_detailed(raw: object, provider_name: str) -> tuple[list[dict[str, Any]], str]:
    if not isinstance(raw, Mapping):
        return [], "invalid_response"
    if raw.get("isError") or raw.get("is_error"):
        _LOG.warning("MCP search %r returned an error result", provider_name)
        return [], "upstream"
    content_text = _extract_text(dict(raw))
    if not content_text:
        return [], "invalid_response"
    try:
        parsed = json.loads(content_text)
    except (json.JSONDecodeError, TypeError):
        return [], "invalid_response"
    items = parsed.get("results") if isinstance(parsed, dict) else parsed
    if not isinstance(items, list):
        return [], "invalid_response"
    if any(not isinstance(item, dict) or not isinstance(item.get("url"), str) for item in items):
        return [], "invalid_response"
    return [item for item in items if isinstance(item, dict)], "ok" if items else "empty"


def _mcp_search_hits(
    items: list[dict[str, Any]],
    *,
    provider_name: str,
    limit: int,
    domains_allow: frozenset[str] | None,
    domains_deny: frozenset[str] | None,
) -> list[SearchHit]:
    hits: list[SearchHit] = []
    for item in items:
        url = str(item.get("url", ""))
        if not url or not url_allowed(url, domains_allow, domains_deny):
            continue
        hits.append(
            SearchHit(
                url=url,
                title=str(item.get("title", "")),
                snippet=str(item.get("snippet", item.get("description", ""))),
                source_engine=provider_name,
                rank=len(hits),
            )
        )
        if len(hits) >= limit:
            break
    return hits


_DIAGNOSTIC_FIELDS = frozenset(
    {
        "provider",
        "outcome",
        "status_code",
        "attempts",
        "latency_ms",
        "retry_wait_ms",
        "result_count",
        "provider_error",
        "max_concurrency",
    }
)


def _bounded_diagnostic(
    provider_name: str,
    raw: Mapping[str, object],
    *,
    started: float,
    result_count: int,
) -> dict[str, object]:
    """Keep provider diagnostics finite and free of response bodies/secrets."""
    raw_outcome = raw.get("outcome")
    allowed_outcomes = {
        "ok",
        "empty",
        "rate_limited",
        "auth",
        "quota",
        "timeout",
        "upstream",
        "invalid_response",
        "partial_outage",
    }
    outcome = str(raw_outcome) if raw_outcome in allowed_outcomes else None
    if outcome is None:
        outcome = "ok" if result_count else "empty"
    status_code = raw.get("status_code")
    diagnostic = _search_diagnostic(
        provider_name,
        outcome,
        started=started,
        status_code=status_code if isinstance(status_code, int) else None,
        result_count=result_count,
    )
    for key in _DIAGNOSTIC_FIELDS:
        value = raw.get(key)
        if key in diagnostic or value is None:
            continue
        if key == "provider_error" and isinstance(value, str):
            diagnostic[key] = value[:48]
        elif key in {"provider", "outcome"}:
            continue
        elif isinstance(value, int):
            diagnostic[key] = max(0, value)
    return diagnostic


async def _call_search_provider(
    provider: Any,
    detailed: Any,
    query: str,
    limit: int,
    domains_allow: frozenset[str] | None,
    domains_deny: frozenset[str] | None,
    time_filter: str | None,
) -> Any:
    if callable(detailed):
        detailed_fn = cast(Callable[..., Awaitable[Any]], detailed)
        return await detailed_fn(
            query,
            limit=limit,
            domains_allow=domains_allow,
            domains_deny=domains_deny,
            time_filter=time_filter,
        )
    search_fn = cast(Callable[..., Awaitable[Any]], provider.search)
    return await search_fn(
        query,
        limit=limit,
        domains_allow=domains_allow,
        domains_deny=domains_deny,
        time_filter=time_filter,
    )


def _search_result_diagnostic(
    result: Any, provider_name: str, started: float, detailed: Any
) -> tuple[list[SearchHit], dict[str, object]]:
    if detailed is not None:
        if not isinstance(result, tuple) or len(result) != 2:
            raise ValueError("search_detailed returned an invalid result")
        raw_hits, raw_diagnostic = result
        if not isinstance(raw_hits, list) or not isinstance(raw_diagnostic, Mapping):
            raise ValueError("search_detailed returned an invalid result")
        hits = [hit for hit in raw_hits if isinstance(hit, SearchHit)]
        return hits, _bounded_diagnostic(
            provider_name, raw_diagnostic, started=started, result_count=len(hits)
        )
    if not isinstance(result, list):
        raise ValueError("search returned an invalid result")
    hits = [hit for hit in result if isinstance(hit, SearchHit)]
    return hits, _search_diagnostic(
        provider_name, "ok" if hits else "empty", started=started, result_count=len(hits)
    )


def _provider_failure(
    provider_name: str, started: float, exc: Exception
) -> tuple[list[SearchHit], dict[str, object]]:
    _LOG.warning("Composite search: provider %r failed (%s)", provider_name, type(exc).__name__)
    outcome = "invalid_response" if isinstance(exc, ValueError) else "upstream"
    return [], _search_diagnostic(provider_name, outcome, started=started, error=exc)


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
        input_fields: frozenset[str] = frozenset(),
    ) -> None:
        self.name = f"mcp__{server}__{tool_name}"
        self._server = server
        self._tool_name = tool_name
        self._call = call_fn
        self._description = description
        self._input_fields = input_fields

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
        hits, _diagnostic = await self.search_detailed(
            query,
            limit=limit,
            domains_allow=domains_allow,
            domains_deny=domains_deny,
            time_filter=time_filter,
        )
        return hits

    async def search_detailed(
        self,
        query: str,
        *,
        limit: int = 10,
        domains_allow: frozenset[str] | None = None,
        domains_deny: frozenset[str] | None = None,
        time_filter: str | None = None,
    ) -> tuple[list[SearchHit], dict[str, object]]:
        """Call one MCP search tool and preserve empty/failure semantics."""
        started = asyncio.get_running_loop().time()
        arguments = _mcp_search_arguments(
            self._input_fields,
            query,
            limit,
            domains_allow,
            domains_deny,
            time_filter,
        )
        if arguments is None:
            return [], _search_diagnostic(self.name, "empty", started=started, result_count=0)
        try:
            raw = await self._call(self._server, self._tool_name, arguments)
        except Exception as exc:
            _LOG.warning("MCP search %r failed (%s)", self.name, type(exc).__name__)
            return [], _search_diagnostic(self.name, "upstream", started=started, error=exc)
        items, outcome = _mcp_result_items_detailed(raw, self.name)
        if outcome not in {"ok", "empty"}:
            return [], _search_diagnostic(self.name, outcome, started=started)
        hits = _mcp_search_hits(
            items,
            provider_name=self.name,
            limit=limit,
            domains_allow=domains_allow,
            domains_deny=domains_deny,
        )
        return hits, _search_diagnostic(
            self.name,
            "ok" if hits else "empty",
            started=started,
            result_count=len(hits),
        )


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
                content="",
                passages=[],
                fetched_ok=False,
                error=f"Extraction failed: {exc}",
                status="error",
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
                input_fields=_tool_input_fields(tool_obj),
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
        hits, _diagnostic = await self.search_detailed(
            query,
            limit=limit,
            domains_allow=domains_allow,
            domains_deny=domains_deny,
            time_filter=time_filter,
        )
        return hits

    async def search_detailed(
        self,
        query: str,
        *,
        limit: int = 10,
        domains_allow: frozenset[str] | None = None,
        domains_deny: frozenset[str] | None = None,
        time_filter: str | None = None,
    ) -> tuple[list[SearchHit], dict[str, object]]:
        batches, diagnostic = await self._gather_batches_detailed(
            query,
            limit=limit,
            domains_allow=domains_allow,
            domains_deny=domains_deny,
            time_filter=time_filter,
        )
        merged: list[SearchHit] = []
        seen: dict[str, int] = {}
        max_batch = max((len(hits) for _provider, hits in batches), default=0)
        for rank in range(max_batch):
            self._merge_rank(rank, batches, merged, seen)
            # Finish the whole rank bucket before truncating so a duplicate from
            # a later provider can still attach provenance to an included hit.
            if len(merged) >= limit:
                return merged[:limit], diagnostic
        return merged[:limit], diagnostic

    async def _gather_batches(
        self,
        query: str,
        *,
        limit: int,
        domains_allow: frozenset[str] | None,
        domains_deny: frozenset[str] | None,
        time_filter: str | None,
    ) -> list[tuple[Any, list[SearchHit]]]:
        batches, _diagnostic = await self._gather_batches_detailed(
            query,
            limit=limit,
            domains_allow=domains_allow,
            domains_deny=domains_deny,
            time_filter=time_filter,
        )
        return batches

    async def _gather_batches_detailed(
        self,
        query: str,
        *,
        limit: int,
        domains_allow: frozenset[str] | None,
        domains_deny: frozenset[str] | None,
        time_filter: str | None,
    ) -> tuple[list[tuple[Any, list[SearchHit]]], dict[str, object]]:
        providers = list(self._providers)
        results = await asyncio.gather(
            *(
                self._search_one_detailed(
                    provider, query, limit, domains_allow, domains_deny, time_filter
                )
                for provider in providers
            )
        )
        batches: list[tuple[Any, list[SearchHit]]] = []
        provider_diagnostics: dict[str, object] = {}
        failed = 0
        for provider, (hits, diagnostic) in zip(providers, results, strict=True):
            name = str(getattr(provider, "name", type(provider).__name__))[:80]
            provider_diagnostics[name] = diagnostic
            outcome = str(diagnostic.get("outcome", "empty"))
            if outcome not in {"ok", "empty"}:
                failed += 1
            if hits:
                batches.append((provider, hits))
        if not providers:
            aggregate = "empty"
        elif failed == len(providers):
            aggregate = "all_failed"
        elif failed:
            aggregate = "partial_outage"
        elif batches:
            aggregate = "success"
        else:
            aggregate = "empty"
        return batches, {
            "provider_aggregate": aggregate,
            "providers": provider_diagnostics,
        }

    @staticmethod
    async def _search_one(
        provider: Any,
        query: str,
        limit: int,
        domains_allow: frozenset[str] | None,
        domains_deny: frozenset[str] | None,
        time_filter: str | None,
    ) -> list[SearchHit] | None:
        hits, _diagnostic = await CompositeSearchProvider._search_one_detailed(
            provider, query, limit, domains_allow, domains_deny, time_filter
        )
        return hits

    @staticmethod
    async def _search_one_detailed(
        provider: Any,
        query: str,
        limit: int,
        domains_allow: frozenset[str] | None,
        domains_deny: frozenset[str] | None,
        time_filter: str | None,
    ) -> tuple[list[SearchHit], dict[str, object]]:
        started = asyncio.get_running_loop().time()
        provider_name = str(getattr(provider, "name", type(provider).__name__))[:80]
        detailed = getattr(provider, "search_detailed", None)
        try:
            result = await _call_search_provider(
                provider, detailed, query, limit, domains_allow, domains_deny, time_filter
            )
            return _search_result_diagnostic(result, provider_name, started, detailed)
        except TypeError as exc:
            if callable(detailed):
                return _provider_failure(provider_name, started, exc)
            try:
                result = await provider.search(query, limit=limit)
                return _search_result_diagnostic(result, provider_name, started, None)
            except Exception as narrow_exc:  # noqa: BLE001
                return _provider_failure(provider_name, started, narrow_exc)
        except Exception as exc:  # noqa: BLE001
            return _provider_failure(provider_name, started, exc)

    @staticmethod
    def _merge_rank(
        rank: int,
        batches: list[tuple[Any, list[SearchHit]]],
        merged: list[SearchHit],
        seen: dict[str, int],
    ) -> None:
        for provider, hits in batches:
            if rank >= len(hits):
                continue
            CompositeSearchProvider._record_hit(provider, hits[rank], merged, seen)

    @staticmethod
    def _record_hit(
        provider: Any, hit: SearchHit, merged: list[SearchHit], seen: dict[str, int]
    ) -> None:
        key = source_url_key(hit.url)
        if hit.url and key in seen:
            # Dedup the candidate URL, but not its provenance. Provider
            # affinity is required downstream: an MCP fetch tool may be
            # the only extractor able to read a URL that bundled search
            # also happened to discover.
            index = seen[key]
            existing = merged[index]
            source = hit.source_engine or getattr(provider, "name", "")
            engines = [part for part in existing.source_engine.split("|") if part]
            if source and source not in engines:
                merged[index] = existing.model_copy(
                    update={"source_engine": "|".join([*engines, source])}
                )
            return
        if hit.url:
            seen[key] = len(merged)
        merged.append(hit)


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
