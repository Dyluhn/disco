"""Universal data providers (§B1/B2): the three tiers for search + extraction, and
the bundled (keyless) defaults. The bundled local extractor is tested offline; the
provider SELECTION is tested by type."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from disco.retrieval import DefaultRetrievalEngine, LexicalReranker, RetrievalRequest
from disco.retrieval._mcp_search_providers import (
    ExaMcpSearchProvider,
    ParallelMcpSearchProvider,
)
from disco.retrieval._transport_retry import (
    OUTCOME_AUTH_REJECTED,
    OUTCOME_KEY,
    SEARCH_ATTEMPTS,
    _extraction_error_class,
    classify_search_response,
    engines_cooling,
    is_transient_extraction_failure,
    search_degradation_from_notes,
    search_with_degradation_retry,
)
from disco.retrieval._wikipedia_search import WikipediaSearchProvider
from disco.retrieval.bundled_providers import (
    _FIRECRAWL_CONCURRENCY,
    BraveSearchProvider,
    FirecrawlExtractionProvider,
    LocalExtractionProvider,
    TavilySearchProvider,
    _MarkdownExtractor,
)
from disco.retrieval.live import _make_extraction, _make_search
from disco.retrieval.source_adapters import (
    ArxivSearchProvider,
    MultiSearchProvider,
    NewsSearchProvider,
    SemanticScholarSearchProvider,
    SiteScopedSearchProvider,
)
from disco.retrieval.url_policy import url_allowed
from research_fakes import FakeExtractionProvider

# ---- provider selection (config → instance) ---------------------------------


def test_search_defaults_to_bundled_keyless_composite():
    """The first-run default is the composite — and the RETIRED `ddgs` id still
    resolves to a working provider instead of crashing an old config."""
    for requested in ("bundled", "", "ddgs"):
        provider = _make_search(requested, "", "")
        assert isinstance(provider, MultiSearchProvider), requested
    legs = [p.name for p in _make_search("bundled", "", "")._providers]
    assert legs == ["parallel", "exa", "wikipedia", "arxiv", "semantic_scholar"]


def test_search_tiers():
    assert type(_make_search("searxng", "http://h:8888", "")).__name__ == "SearxngSearchProvider"
    assert isinstance(_make_search("tavily", "", "key"), TavilySearchProvider)
    assert isinstance(_make_search("exa", "", ""), ExaMcpSearchProvider)
    assert isinstance(_make_search("parallel", "", ""), ParallelMcpSearchProvider)
    assert isinstance(_make_search("wikipedia", "", ""), WikipediaSearchProvider)
    assert isinstance(_make_search("arxiv", "", ""), ArxivSearchProvider)
    assert isinstance(_make_search("news", "", ""), NewsSearchProvider)
    assert isinstance(_make_search("semantic_scholar", "", "key"), SemanticScholarSearchProvider)
    assert isinstance(_make_search("site_scoped", "example.com", ""), SiteScopedSearchProvider)


def test_searxng_requests_general_and_science_categories():
    """Research queries reach the academic engines, not only the general web."""
    provider = _make_search("searxng", "http://h:8888", "")
    assert provider._build_params("q", None)["categories"] == "general,science"
    custom = _make_search("searxng", "http://h:8888", "", categories="general,it")
    assert custom._build_params("q", None)["categories"] == "general,it"


def test_extraction_defaults_to_bundled_local():
    assert isinstance(_make_extraction("local", "", ""), LocalExtractionProvider)
    assert isinstance(_make_extraction("", "", ""), LocalExtractionProvider)


def test_extraction_tiers():
    crawl = _make_extraction("crawl4ai", "http://h", "")
    assert type(crawl).__name__ == "Crawl4aiExtractionProvider"
    assert isinstance(_make_extraction("firecrawl", "", "key"), FirecrawlExtractionProvider)


# ---- bundled local extractor (stdlib HTML→markdown, offline) -----------------


def test_local_extractor_emits_markdown_and_drops_noise():
    html = (
        "<html><head><title>My Page</title><style>a{}</style></head>"
        "<body><nav>menu</nav><h1>Title</h1>"
        '<p>A <a href="https://x.test">link</a>.</p>'
        "<ul><li>one</li><li>two</li></ul>"
        "<script>evil()</script></body></html>"
    )
    p = _MarkdownExtractor()
    p.feed(html)
    md = p.markdown()
    assert p.title.strip() == "My Page"
    assert "# Title" in md
    assert "[link](https://x.test)" in md
    assert "- one" in md and "- two" in md
    assert "evil()" not in md and "a{}" not in md  # script + style dropped


def test_domain_policy_matches_only_exact_hosts_and_subdomains():
    assert url_allowed("https://docs.example.com/a", frozenset({"example.com"}), None)
    assert not url_allowed("https://notexample.com/a", frozenset({"example.com"}), None)
    assert not url_allowed("https://example.com.evil/a", frozenset({"example.com"}), None)


async def test_paid_search_providers_thread_recency_and_domain_controls():
    def tavily_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["time_range"] == "week"
        assert payload["include_domains"] == ["example.com"]
        assert payload["exclude_domains"] == ["blocked.example"]
        return httpx.Response(200, json={"results": []})

    tavily = TavilySearchProvider("key", transport=httpx.MockTransport(tavily_handler))
    await tavily.search(
        "q",
        time_filter="week",
        domains_allow=frozenset({"example.com"}),
        domains_deny=frozenset({"blocked.example"}),
    )

    def brave_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["freshness"] == "pm"
        return httpx.Response(200, json={"web": {"results": []}})

    brave = BraveSearchProvider("key", transport=httpx.MockTransport(brave_handler))
    await brave.search("q", time_filter="month")


def test_paid_providers_no_key_is_safe():
    # no key → empty/failed, never a crash or a leaked request
    assert asyncio.run(TavilySearchProvider("").search("x")) == []
    doc = asyncio.run(FirecrawlExtractionProvider("").extract("https://x.test"))
    assert not doc.fetched_ok and doc.status == "error"


# ---- paid providers: the request each vendor actually accepts ---------------
#
# Both of these were sending an obsolete request. Tavily put the key in the JSON
# body (the current contract is `Authorization: Bearer` with NO body key), and
# Firecrawl scraped the deprecated V1 endpoint. On a metered account the first
# credited call would have failed — and, because every failure collapsed into an
# empty list, failed as "no results", which the research model answers by
# rewriting a perfectly good query.
#
# These assert the request SHAPE (that the header exists, that the body carries
# no credential field) and never the credential value.

#: Enough prose for `chunk_passages` to produce a citable passage — an extractor
#: that returns content but no passages is a failed extraction here.
_MARKDOWN_PAGE = "\n\n".join(
    f"Paragraph {i} with plenty of words in it so that it counts as real content."
    for i in range(6)
)


async def test_tavily_authenticates_with_bearer_header_and_no_body_key():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = request.headers
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"results": []})

    provider = TavilySearchProvider("placeholder", transport=httpx.MockTransport(handler))
    await provider.search("q", limit=3)

    assert captured["url"] == "https://api.tavily.com/search"
    assert "authorization" in captured["headers"]
    assert captured["headers"]["authorization"].startswith("Bearer ")
    assert "api_key" not in captured["body"], "the current contract has no body credential"
    assert captured["body"]["query"] == "q" and captured["body"]["max_results"] == 3


async def test_tavily_maps_results_and_reports_a_clean_zero_as_empty():
    """A REAL empty result set is a research finding and must stay one: no
    provider_error marker, so the degradation classifier leaves it alone."""

    def hits_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": "https://good.test/a",
                        "title": "A",
                        "content": "snip",
                        "published_date": "2026-01-02",
                    },
                    {"url": "https://blocked.test/b", "title": "B", "content": "x"},
                ]
            },
        )

    provider = TavilySearchProvider("placeholder", transport=httpx.MockTransport(hits_handler))
    hits, diagnostic = await provider.search_detailed(
        "q", domains_deny=frozenset({"blocked.test"})
    )
    assert [hit.url for hit in hits] == ["https://good.test/a"]
    assert hits[0].source_engine == "tavily" and hits[0].snippet == "snip"
    assert diagnostic["result_count"] == 2  # raw provider count stays observable
    assert isinstance(diagnostic["latency_ms"], int)

    def empty_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    empty = TavilySearchProvider("placeholder", transport=httpx.MockTransport(empty_handler))
    hits, diagnostic = await empty.search_detailed("q")
    assert hits == []
    assert "provider_error" not in diagnostic
    assert classify_search_response(0, diagnostic) is None  # a clean zero, not an outage


@pytest.mark.parametrize("status_code", [401, 403])
async def test_tavily_rejected_key_is_an_auth_outage_never_no_hits(status_code: int):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"detail": "unauthorized"})

    provider = TavilySearchProvider("placeholder", transport=httpx.MockTransport(handler))
    hits, diagnostic = await provider.search_detailed("q")

    assert hits == []
    assert diagnostic["status_code"] == status_code
    assert "auth rejected" in str(diagnostic["provider_error"])
    degradation = classify_search_response(0, diagnostic)
    assert degradation is not None, "an auth failure is an outage, never a clean zero"
    assert degradation.auth_rejected is True and degradation.rate_limited is False


@pytest.mark.parametrize(
    ("status_code", "marker"), [(402, "quota"), (429, "rate limit")]
)
async def test_tavily_quota_and_rate_limit_are_the_rate_class(status_code: int, marker: str):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"detail": "no"})

    provider = TavilySearchProvider("placeholder", transport=httpx.MockTransport(handler))
    _hits, diagnostic = await provider.search_detailed("q")

    assert marker in str(diagnostic["provider_error"])
    degradation = classify_search_response(0, diagnostic)
    assert degradation is not None
    assert degradation.rate_limited is True and degradation.auth_rejected is False


async def test_tavily_missing_key_is_an_auth_fault_not_an_empty_world():
    hits, diagnostic = await TavilySearchProvider("").search_detailed("q")
    assert hits == []
    degradation = classify_search_response(0, diagnostic)
    assert degradation is not None and degradation.auth_rejected is True


async def test_a_rejected_tavily_key_is_never_retried():
    """Money and turns: an auth failure that reaches the retry driver must end
    after ONE attempt and carry its own outcome, not `degraded` and not `no_hits`."""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(401, json={"detail": "unauthorized"})

    provider = TavilySearchProvider("placeholder", transport=httpx.MockTransport(handler))
    _hits, diagnostic = await search_with_degradation_retry(
        lambda: provider.search_detailed("q")
    )

    assert attempts == 1
    assert diagnostic[OUTCOME_KEY] == OUTCOME_AUTH_REJECTED
    assert engines_cooling() == (), "a refused key says nothing about an engine"


async def test_a_tavily_upstream_error_still_gets_its_retries():
    """The unretryable classes must not swallow the retryable one: a 502 is a
    transient outage and keeps the full below-the-model retry budget."""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(502, text="bad gateway")

    provider = TavilySearchProvider("placeholder", transport=httpx.MockTransport(handler))
    _hits, diagnostic = await search_with_degradation_retry(
        lambda: provider.search_detailed("q")
    )
    assert attempts == SEARCH_ATTEMPTS
    assert diagnostic[OUTCOME_KEY] == "degraded"


async def test_a_rejected_tavily_key_never_reaches_the_model_as_no_hits():
    """The whole point, end to end. A run whose paid search key is refused must
    read back as an untested outage — the classification the research agent turns
    into PROVIDER_DEGRADED — and never as the clean zero that makes a model spend
    turns (and credits) rewriting a query that was never actually asked."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "unauthorized"})

    provider = TavilySearchProvider("placeholder", transport=httpx.MockTransport(handler))
    result = await DefaultRetrievalEngine(
        provider, FakeExtractionProvider({}), LexicalReranker()
    ).retrieve(RetrievalRequest(query="anything at all", depth="shallow"))

    assert result.all_hits == []
    diagnostic = result.notes["retrieval_trace"]["provider_diagnostics"][0]
    assert diagnostic[OUTCOME_KEY] == OUTCOME_AUTH_REJECTED
    degradation = search_degradation_from_notes(result.notes)
    assert degradation is not None, "a refused key is an outage, not a research finding"
    assert degradation.auth_rejected is True and degradation.attempts == 1


async def test_firecrawl_scrapes_the_v2_endpoint_with_a_bearer_header():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = request.headers
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "markdown": _MARKDOWN_PAGE,
                    "metadata": {"title": "Real Title", "statusCode": 200},
                },
            },
        )

    provider = FirecrawlExtractionProvider(
        "placeholder", transport=httpx.MockTransport(handler)
    )
    doc = await provider.extract("https://example.com/page")

    assert captured["url"] == "https://api.firecrawl.dev/v2/scrape"
    assert "authorization" in captured["headers"]
    assert captured["headers"]["authorization"].startswith("Bearer ")
    assert captured["body"] == {"url": "https://example.com/page", "formats": ["markdown"]}
    assert doc.fetched_ok and doc.title == "Real Title" and doc.passages


def test_firecrawl_base_url_never_doubles_its_version_segment():
    for base in ("https://api.firecrawl.dev", "https://api.firecrawl.dev/", "https://fc.test/v1"):
        provider = FirecrawlExtractionProvider("placeholder", base)
        assert not provider._base.endswith(("/v1", "/v2", "/"))


@pytest.mark.parametrize(
    ("status_code", "error_class"),
    [(401, "auth_rejected"), (403, "auth_rejected"), (402, "rate_limited"), (429, "rate_limited")],
)
async def test_firecrawl_account_failures_are_classified_and_never_retried(
    status_code: int, error_class: str
):
    """A rejected key or an exhausted plan is a fact about the ACCOUNT. It must
    not read as an unreadable page, and it must not be retried — retrying a 402
    across a turn's URLs is how a credit balance vanishes into an outage."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": "no"})

    provider = FirecrawlExtractionProvider(
        "placeholder", transport=httpx.MockTransport(handler)
    )
    doc = await provider.extract("https://example.com/page")

    assert not doc.fetched_ok
    assert _extraction_error_class(doc) == error_class
    assert is_transient_extraction_failure(doc) is False


async def test_firecrawl_isolates_one_bad_url_and_bounds_its_concurrency():
    """Per-URL isolation (the crawl4ai rule, one layer over): a failing URL costs
    only itself. And a turn's batch never opens more scrapes at once than the
    configured ceiling."""
    in_flight = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0)
        in_flight -= 1
        # The scraped url travels in the BODY — one request per url is the whole
        # point of the contract.
        if json.loads(request.content)["url"].endswith("/bad"):
            return httpx.Response(500, text="upstream exploded")
        return httpx.Response(
            200, json={"data": {"markdown": _MARKDOWN_PAGE, "metadata": {"title": "T"}}}
        )

    urls = [f"https://example.com/{name}" for name in ("a", "bad", "b", "c", "d")]
    provider = FirecrawlExtractionProvider(
        "placeholder", transport=httpx.MockTransport(handler)
    )
    docs = await provider.extract_many(urls)

    assert [doc.url for doc in docs] == urls  # order preserved, nothing dropped
    assert [doc.fetched_ok for doc in docs] == [True, False, True, True, True]
    assert peak <= _FIRECRAWL_CONCURRENCY


def test_local_extractor_populates_citable_passages():
    """The research pipeline cites PASSAGES, not raw content — an extractor that
    returns content but no passages makes a run report 'couldn't read any'. So the
    local extractor must chunk (regression for the in-app extraction failure)."""
    from disco.retrieval.bundled_providers import chunk_passages

    para = "Paragraph with plenty of words so it counts as a real passage of content here."
    body = "\n\n".join(f"{i}. {para}" for i in range(60))  # ~5k chars → several passages
    ps = chunk_passages("https://x.test", "Title", body)
    assert len(ps) >= 3
    assert all(p.source_url == "https://x.test" and p.text for p in ps)
    assert len({p.id for p in ps}) == len(ps)  # stable, unique ids


def test_local_extractor_drops_empty_anchor_nav():
    """Empty-anchor links (`[](url)` nav menus) are dropped — passages get real
    content, not menu noise."""
    html = (
        '<html><body><ul><li><a href="/a"></a></li>'
        '<li><a href="/b">Real text</a></li></ul></body></html>'
    )
    p = _MarkdownExtractor()
    p.feed(html)
    md = p.markdown()
    assert "[](/a)" not in md  # empty anchor gone
    assert "[Real text](/b)" in md  # anchor with text kept


def test_a_searxng_provider_built_with_a_data_dir_carries_the_result_cache(tmp_path) -> None:
    """The self-hosted tier's 24-hour result cache lives in the data dir; with
    no data dir the provider runs uncached rather than inventing a path."""
    cached = _make_search("searxng", "http://h:8888", "", data_dir=str(tmp_path))
    uncached = _make_search("searxng", "http://h:8888", "")

    assert cached._cache is not None
    assert (tmp_path / "retrieval-cache.sqlite").exists()
    assert uncached._cache is None
