"""MCP retrieval-tier tests — RP-05 rung B.

Anti-gaming bar (from master brief):
- isinstance(wrapped, SearchProvider/ExtractionProvider) against the REAL
  runtime-checkable Protocols
- citation through the MCP tier flows the SAME GroundingPipeline as a bundled
  provider (drive the real broker, not a mock)
"""

from __future__ import annotations

import json

import pytest
from disco.retrieval.models import SearchHit
from disco.retrieval.providers import ExtractionProvider, SearchProvider
from disco.tools.mcp.retrieval_tier import (
    _MCPRetrievalExtractionProvider,
    _MCPRetrievalSearchProvider,
    _tool_matches_fetch_shape,
    _tool_matches_search_shape,
    build_retrieval_providers,
)

# ---------------------------------------------------------------------------
# Fake MCP call function (simulates pool.call_tool)
# ---------------------------------------------------------------------------


class _FakeMCPCall:
    """Simulates an MCP pool's call_tool method."""

    def __init__(self, results: dict | None = None):
        self._results = dict(results or {})
        self.calls: list[tuple] = []

    async def call(self, server: str, tool: str, arguments: dict) -> dict:
        self.calls.append((server, tool, arguments))
        return self._results.get(
            f"{server}/{tool}",
            {"content": [], "isError": False},
        )


class _FakeMCPTool:
    """A minimal MCP tool object with name and description."""

    def __init__(self, name: str, description: str = "", input_schema: dict | None = None):
        self.name = name
        self.description = description
        if input_schema is None:
            if name in {"search", "web_search", "brave_search"}:
                input_schema = {"properties": {"query": {"type": "string"}}}
            elif name in {"fetch", "web_fetch", "fetch_url", "extract"}:
                input_schema = {"properties": {"id": {"type": "string"}}}
            else:
                input_schema = {"properties": {}}
        self.inputSchema = input_schema


# ---------------------------------------------------------------------------
# Shape matching tests
# ---------------------------------------------------------------------------


def test_tool_matches_search_shape():
    """Search-shaped tools are correctly identified."""
    assert _tool_matches_search_shape(_FakeMCPTool("search"))
    assert _tool_matches_search_shape(_FakeMCPTool("web_search"))
    assert _tool_matches_search_shape(_FakeMCPTool("brave_search"))
    assert not _tool_matches_search_shape(_FakeMCPTool("echo"))
    assert not _tool_matches_search_shape(_FakeMCPTool("fetch"))
    assert not _tool_matches_search_shape(
        _FakeMCPTool("search", input_schema={"properties": {"term": {"type": "string"}}})
    )


def test_tool_matches_fetch_shape():
    """Fetch-shaped tools are correctly identified."""
    assert _tool_matches_fetch_shape(_FakeMCPTool("fetch"))
    assert _tool_matches_fetch_shape(_FakeMCPTool("web_fetch"))
    assert _tool_matches_fetch_shape(_FakeMCPTool("fetch_url"))
    assert _tool_matches_fetch_shape(_FakeMCPTool("extract"))
    assert not _tool_matches_fetch_shape(_FakeMCPTool("echo"))
    assert not _tool_matches_fetch_shape(_FakeMCPTool("search"))
    assert not _tool_matches_fetch_shape(
        _FakeMCPTool("fetch", input_schema={"properties": {"path": {"type": "string"}}})
    )


# ---------------------------------------------------------------------------
# Protocol compliance tests (ANTI-GAMING: isinstance against REAL Protocols)
# ---------------------------------------------------------------------------


def test_search_provider_isinstance_check():
    """ANTI-GAMING: isinstance(wrapped, SearchProvider) against the REAL
    @runtime_checkable Protocol."""
    fake_call = _FakeMCPCall().call
    provider = _MCPRetrievalSearchProvider("srv", "search", fake_call)

    assert isinstance(provider, SearchProvider)
    assert provider.name == "mcp__srv__search"


def test_extraction_provider_isinstance_check():
    """ANTI-GAMING: isinstance(wrapped, ExtractionProvider) against the REAL
    @runtime_checkable Protocol."""
    fake_call = _FakeMCPCall().call
    provider = _MCPRetrievalExtractionProvider("srv", "fetch", fake_call)

    assert isinstance(provider, ExtractionProvider)
    assert provider.name == "mcp__srv__fetch"


def test_build_retrieval_providers_returns_protocol_instances():
    """build_retrieval_providers returns lists where EVERY element is an
    instance of the correct Protocol."""
    search_results = {
        "srv/search": {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "results": [
                                {"id": "1", "title": "Test Result", "url": "https://example.com"},
                            ]
                        }
                    ),
                }
            ],
            "isError": False,
        },
    }
    fake_call = _FakeMCPCall(search_results).call

    entries = [
        {
            "server": "srv",
            "tool_name": "search",
            # Production registration stores the qualified name in ToolDef;
            # the separate tool_name entry carries the raw MCP name.
            "tool": _FakeMCPTool(
                "mcp__srv__search",
                "Search the web",
                {"properties": {"query": {"type": "string"}}},
            ),
        },
        {
            "server": "srv",
            "tool_name": "fetch",
            "tool": _FakeMCPTool(
                "mcp__srv__fetch",
                "Fetch a URL",
                {"properties": {"url": {"type": "string"}}},
            ),
        },
        {
            "server": "srv",
            "tool_name": "echo",
            "tool": _FakeMCPTool("echo", "Echo a message"),
        },
    ]

    searchers, extractors = build_retrieval_providers(entries, call_fn=fake_call)

    # search tool → SearchProvider
    assert len(searchers) == 1
    assert isinstance(searchers[0], SearchProvider)

    # fetch tool → ExtractionProvider
    assert len(extractors) == 1
    assert isinstance(extractors[0], ExtractionProvider)

    # echo tool → neither (not matched)
    non_matched = {
        "server": "srv",
        "tool_name": "echo",
        "tool": _FakeMCPTool("echo"),
    }
    assert not _tool_matches_search_shape(non_matched["tool"])
    assert not _tool_matches_fetch_shape(non_matched["tool"])


# ---------------------------------------------------------------------------
# Functional tests — drive the REAL broker pattern
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_provider_calls_mcp_tool():
    """The wrapped SearchProvider calls the real MCP call function with
    the correct arguments."""
    search_results = {
        "srv/search": {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "results": [
                                {"id": "1", "title": "Result One", "url": "https://example.com/1"},
                                {"id": "2", "title": "Result Two", "url": "https://example.com/2"},
                            ],
                        }
                    ),
                }
            ],
            "isError": False,
        },
    }
    fake_call = _FakeMCPCall(search_results)
    provider = _MCPRetrievalSearchProvider("srv", "search", fake_call.call)

    hits = await provider.search("test query", limit=5)

    # The MCP tool was called with the right arguments
    assert len(fake_call.calls) == 1
    server, tool, args = fake_call.calls[0]
    assert server == "srv"
    assert tool == "search"
    assert args["query"] == "test query"

    # Results are converted to SearchHit objects
    assert len(hits) == 2
    assert hits[0].url == "https://example.com/1"
    assert hits[0].title == "Result One"
    assert hits[0].source_engine == "mcp__srv__search"


@pytest.mark.asyncio
async def test_extraction_provider_calls_mcp_tool():
    """The wrapped ExtractionProvider calls the real MCP call function."""
    extract_results = {
        "srv/fetch": {
            "content": [
                {
                    "type": "text",
                    "text": "Full page content here with enough substantive source text to cite.",
                },
            ],
            "isError": False,
        },
    }
    fake_call = _FakeMCPCall(extract_results)
    provider = _MCPRetrievalExtractionProvider("srv", "fetch", fake_call.call)

    doc = await provider.extract("https://example.com/page")

    # The MCP tool was called
    assert len(fake_call.calls) == 1
    server, tool, args = fake_call.calls[0]
    assert server == "srv"
    assert tool == "fetch"
    assert args["id"] == "https://example.com/page"

    # Result is an ExtractedDoc
    assert doc.url == "https://example.com/page"
    assert "Full page content" in doc.content
    assert doc.status == "ok"
    assert doc.fetched_ok is True
    assert len(doc.passages) == 1
    assert doc.passages[0].source_url == "https://example.com/page"


@pytest.mark.asyncio
async def test_extraction_provider_extract_many():
    """extract_many calls extract for each URL."""
    extract_results = {
        "srv/fetch": {
            "content": [{"type": "text", "text": "Content"}],
            "isError": False,
        },
    }
    fake_call = _FakeMCPCall(extract_results)
    provider = _MCPRetrievalExtractionProvider("srv", "fetch", fake_call.call)

    docs = await provider.extract_many(["https://a.com", "https://b.com"])

    assert len(docs) == 2
    assert len(fake_call.calls) == 2
    assert docs[0].url == "https://a.com"
    assert docs[1].url == "https://b.com"


@pytest.mark.asyncio
async def test_search_provider_handles_error_gracefully():
    """When the MCP call fails, the provider returns an empty list (no crash)."""

    async def failing_call(server, tool, args):
        raise RuntimeError("MCP call failed")

    provider = _MCPRetrievalSearchProvider("srv", "search", failing_call)
    hits = await provider.search("query")
    assert hits == []


@pytest.mark.asyncio
async def test_mcp_search_enforces_filters_and_threads_supported_recency():
    captured: dict = {}

    async def call(server, tool, arguments):
        captured.update(arguments)
        return {
            "content": [
                {
                    "text": json.dumps(
                        {
                            "results": [
                                {"url": "https://example.com/good", "title": "Good"},
                                {"url": "https://notexample.com/bad", "title": "Bad"},
                            ]
                        }
                    )
                }
            ]
        }

    provider = _MCPRetrievalSearchProvider(
        "srv",
        "search",
        call,
        input_fields=frozenset({"query", "limit", "time_range", "include_domains"}),
    )
    hits = await provider.search(
        "new release",
        limit=4,
        time_filter="week",
        domains_allow=frozenset({"example.com"}),
    )

    assert captured == {
        "query": "new release",
        "limit": 4,
        "time_range": "week",
        "include_domains": ["example.com"],
    }
    assert [hit.url for hit in hits] == ["https://example.com/good"]


@pytest.mark.asyncio
async def test_mcp_without_recency_capability_is_excluded_from_recency_run():
    called = False

    async def call(server, tool, arguments):
        nonlocal called
        called = True
        return {"content": []}

    provider = _MCPRetrievalSearchProvider("srv", "search", call)
    assert await provider.search("new release", time_filter="week") == []
    assert called is False


@pytest.mark.asyncio
async def test_narrow_mcp_failure_does_not_sink_healthy_search_provider():
    from disco.tools.mcp.retrieval_tier import CompositeSearchProvider

    class _Healthy:
        name = "healthy"

        async def search(self, query, **kwargs):
            return [SearchHit(url="https://example.com/good", title="Good")]

    class _NarrowFailing:
        name = "narrow-failing"

        async def search(self, query, *, limit):
            raise RuntimeError("provider unavailable")

    composite = CompositeSearchProvider(_Healthy(), [_NarrowFailing()])
    hits = await composite.search("new release", limit=4)

    assert [hit.url for hit in hits] == ["https://example.com/good"]


@pytest.mark.asyncio
async def test_composite_detailed_search_distinguishes_empty_and_partial_failure():
    from disco.tools.mcp.retrieval_tier import CompositeSearchProvider

    class _Empty:
        name = "empty"

        async def search_detailed(self, query, **kwargs):
            return [], {"provider": self.name, "outcome": "empty", "result_count": 0}

    class _Healthy:
        name = "healthy"

        async def search_detailed(self, query, **kwargs):
            return [SearchHit(url="https://example.com/good", title="Good")], {
                "provider": self.name,
                "outcome": "ok",
                "result_count": 1,
            }

    class _Broken:
        name = "broken"

        async def search_detailed(self, query, **kwargs):
            raise RuntimeError("secret response body")

    composite = CompositeSearchProvider(_Empty(), [_Healthy(), _Broken()])
    hits, diagnostic = await composite.search_detailed("q", limit=4)

    assert [hit.url for hit in hits] == ["https://example.com/good"]
    assert diagnostic["provider_aggregate"] == "partial_outage"
    assert diagnostic["providers"]["empty"]["outcome"] == "empty"
    assert diagnostic["providers"]["broken"]["outcome"] == "upstream"
    assert diagnostic["providers"]["broken"]["provider_error"] == "RuntimeError"
    assert "secret response body" not in str(diagnostic)


@pytest.mark.asyncio
async def test_composite_detailed_search_reports_all_failed_and_invalid_response():
    from disco.tools.mcp.retrieval_tier import CompositeSearchProvider

    class _Invalid:
        name = "invalid"

        async def search_detailed(self, query, **kwargs):
            return [], {
                "provider": self.name,
                "outcome": "invalid_response",
                "result_count": 0,
                "body": "must not be copied",
            }

    class _Broken:
        name = "broken"

        async def search(self, query, **kwargs):
            raise RuntimeError("unavailable")

    composite = CompositeSearchProvider(_Invalid(), [_Broken()])
    hits, diagnostic = await composite.search_detailed("q", limit=4)

    assert hits == []
    assert diagnostic["provider_aggregate"] == "all_failed"
    assert diagnostic["providers"]["invalid"]["outcome"] == "invalid_response"
    assert "body" not in str(diagnostic)
    assert "must not be copied" not in str(diagnostic)


@pytest.mark.asyncio
async def test_mcp_error_results_never_become_search_hits_or_citable_content():
    result = {
        "content": [{"type": "text", "text": "unknown source"}],
        "isError": True,
    }
    fake_call = _FakeMCPCall({"srv/search": result, "srv/fetch": result})

    search = _MCPRetrievalSearchProvider("srv", "search", fake_call.call)
    extraction = _MCPRetrievalExtractionProvider("srv", "fetch", fake_call.call)

    assert await search.search("query") == []
    doc = await extraction.extract("https://example.com/missing")
    assert doc.fetched_ok is False
    assert doc.status == "error"
    assert doc.passages == []
    assert doc.content == ""
    assert doc.error == "unknown source"


@pytest.mark.asyncio
async def test_mcp_extraction_exception_is_an_explicit_failed_document():
    async def failing_call(server, tool, arguments):
        raise RuntimeError("fetch unavailable")

    provider = _MCPRetrievalExtractionProvider("srv", "fetch", failing_call)
    doc = await provider.extract("https://example.com/source")
    assert doc.fetched_ok is False
    assert doc.status == "error"
    assert doc.passages == []
    assert doc.content == ""
    assert doc.error == "Extraction failed: fetch unavailable"


@pytest.mark.asyncio
async def test_search_provider_handles_non_json_result():
    """When the MCP result is not valid JSON, the provider returns empty."""
    fake_call = _FakeMCPCall(
        {
            "srv/search": {
                "content": [{"type": "text", "text": "not valid json {{{"}],
                "isError": False,
            },
        }
    )
    provider = _MCPRetrievalSearchProvider("srv", "search", fake_call.call)
    hits = await provider.search("query")
    assert hits == []


@pytest.mark.asyncio
async def test_search_provider_detailed_result_classifies_invalid_and_empty():
    empty = _MCPRetrievalSearchProvider(
        "srv",
        "search",
        _FakeMCPCall(
            {
                "srv/search": {
                    "content": [{"type": "text", "text": json.dumps({"results": []})}],
                    "isError": False,
                }
            }
        ).call,
    )
    invalid = _MCPRetrievalSearchProvider(
        "srv",
        "search",
        _FakeMCPCall(
            {
                "srv/search": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps({"results": [{"title": "no url"}]}),
                        }
                    ],
                    "isError": False,
                }
            }
        ).call,
    )

    empty_hits, empty_diagnostic = await empty.search_detailed("q")
    invalid_hits, invalid_diagnostic = await invalid.search_detailed("q")
    assert empty_hits == []
    assert empty_diagnostic["outcome"] == "empty"
    assert invalid_hits == []
    assert invalid_diagnostic["outcome"] == "invalid_response"


@pytest.mark.asyncio
async def test_mcp_discovered_url_flows_through_real_engine_to_citation():
    """ANTI-GAMING functional half (the REAL one, not a mock broker): a URL that
    ONLY the MCP search tier discovers is extracted, reranked, and emerges as a
    citable Passage from the genuine DefaultRetrievalEngine — proving MCP discovery
    joins the SAME pipeline as bundled hits (workorder §3).

    Drives the PRODUCTION objects end to end: _MCPRetrievalSearchProvider (the real
    wrapper) → compose_with_mcp (the real composition the runtime calls) →
    DefaultRetrievalEngine + LexicalReranker (the real engine + reranker). An
    earlier version registered providers under dead `mcp_search_*` broker names —
    a path the runtime no longer uses; that was the gamed structural-only test.
    """
    from disco.retrieval.engine import DefaultRetrievalEngine
    from disco.retrieval.models import (
        ExtractedDoc,
        Passage,
        RetrievalRequest,
    )
    from disco.retrieval.ranking import LexicalReranker
    from disco.tools.mcp.retrieval_tier import compose_with_mcp

    MCP_URL = "https://mcp-only.example/doc"

    # Production MCP search provider, returning a hit ONLY the MCP tier knows.
    mcp_search_results = {
        "srv/search": {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "results": [
                                {
                                    "id": "1",
                                    "title": "MCP Discovery",
                                    "url": MCP_URL,
                                    "snippet": "discovered via MCP",
                                }
                            ],
                        }
                    ),
                }
            ],
            "isError": False,
        },
    }
    fake_call = _FakeMCPCall(mcp_search_results).call
    mcp_search = _MCPRetrievalSearchProvider("srv", "search", fake_call)

    # Bundled tier: search finds NOTHING (so the sole hit is the MCP one); the
    # shared extractor turns ANY discovered URL into a passage — the identical
    # extraction every bundled hit flows through.
    class _EmptyBundledSearch:
        name = "bundled"

        async def search(self, query, *, limit=10, domains_allow=None, domains_deny=None):
            return []

    class _SharedExtractor:
        name = "bundled"

        async def extract(self, url):
            return ExtractedDoc(
                url=url,
                title="Read",
                content="full text about the topic",
                passages=[
                    Passage(
                        id="p1",
                        source_url=url,
                        source_title="Read",
                        text="grounded passage about the topic",
                    )
                ],
                fetched_ok=True,
            )

        async def extract_many(self, urls):
            return [await self.extract(u) for u in urls]

    # Production composition: bundled primary + MCP extra (the runtime call).
    search, extraction = compose_with_mcp(
        _EmptyBundledSearch(),
        _SharedExtractor(),
        mcp_searches=[mcp_search],
        mcp_extractions=[],
    )

    engine = DefaultRetrievalEngine(search, extraction, LexicalReranker())
    result = await engine.retrieve(RetrievalRequest(query="the topic", top_k=5))

    # The MCP-discovered URL surfaced as a discovery hit...
    assert any(h.url == MCP_URL for h in result.all_hits)
    # ...and produced a CITABLE passage through the real rerank pipeline.
    assert any(p.source_url == MCP_URL for p in result.passages)
    # Sanity: it was the production MCP provider that supplied it.
    assert mcp_search.name == "mcp__srv__search"


@pytest.mark.asyncio
async def test_composite_search_does_not_starve_mcp_behind_primary_limit():
    """A full bundled result page must not push every MCP result beyond the
    extraction cap; otherwise MCP is called but can never affect citations."""
    from disco.retrieval.models import SearchHit
    from disco.tools.mcp.retrieval_tier import CompositeSearchProvider

    class _Provider:
        def __init__(self, name: str, count: int):
            self.name = name
            self.count = count

        async def search(self, query, **kwargs):
            return [
                SearchHit(
                    url=f"https://{self.name}.example/{index}",
                    title=f"{self.name} {index}",
                    source_engine=self.name,
                    rank=index,
                )
                for index in range(self.count)
            ]

    composite = CompositeSearchProvider(_Provider("bundled", 10), [_Provider("mcp", 1)])
    hits = await composite.search("q", limit=6)

    assert len(hits) == 6
    assert [hit.source_engine for hit in hits[:3]] == ["bundled", "mcp", "bundled"]


@pytest.mark.asyncio
async def test_duplicate_url_keeps_mcp_affinity_through_real_engine():
    """A bundled duplicate must not erase the MCP server that can read it."""
    from disco.retrieval.engine import DefaultRetrievalEngine
    from disco.retrieval.models import ExtractedDoc, Passage, RetrievalRequest, SearchHit
    from disco.retrieval.ranking import LexicalReranker
    from disco.tools.mcp.retrieval_tier import compose_with_mcp

    url = "https://example.com/shared"
    mcp_results = {
        "srv/search": {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {"results": [{"title": "Shared", "url": url, "snippet": "MCP"}]}
                    ),
                }
            ],
            "isError": False,
        },
        "srv/fetch": {
            "content": [
                {
                    "type": "text",
                    "text": (
                        "MCP_AFFINITY_PROOF safe method content is substantive enough "
                        "to become a citable passage in the production chunker."
                    ),
                }
            ],
            "isError": False,
        },
    }
    calls = _FakeMCPCall(mcp_results)
    mcp_search = _MCPRetrievalSearchProvider("srv", "search", calls.call)
    mcp_extract = _MCPRetrievalExtractionProvider("srv", "fetch", calls.call)

    class _BundledSearch:
        name = "bundled"

        async def search(self, query, **kwargs):
            return [SearchHit(url=url, title="Shared", source_engine=self.name)]

    class _BundledExtraction:
        name = "bundled"

        async def extract(self, target):
            return ExtractedDoc(
                url=target,
                title="Bundled",
                content="bundled fallback",
                passages=[
                    Passage(
                        id="bundled_p0",
                        source_url=target,
                        source_title="Bundled",
                        text="bundled fallback",
                    )
                ],
            )

        async def extract_many(self, urls):
            return [await self.extract(target) for target in urls]

    search, extraction = compose_with_mcp(
        _BundledSearch(),
        _BundledExtraction(),
        mcp_searches=[mcp_search],
        mcp_extractions=[mcp_extract],
    )
    result = await DefaultRetrievalEngine(search, extraction, LexicalReranker()).retrieve(
        RetrievalRequest(query="safe method", top_k=3)
    )

    assert result.all_hits[0].source_engine == "bundled|mcp__srv__search"
    assert any("MCP_AFFINITY_PROOF" in passage.text for passage in result.passages)
    assert ("srv", "fetch", {"id": url}) in calls.calls
