"""Live HTTP retrieval providers — hermetic tests (httpx.MockTransport).

No network: mock transports return canned backend responses so we test the
mapping to the existing interfaces (SearchHit / ExtractedDoc / Passage / verdicts)
and the graceful-degradation paths. (The live smoke against the real LAN endpoints
is run separately.)
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest
from disco.core.host_egress import GuardedResponse
from disco.retrieval import _extraction_fallbacks, bundled_providers
from disco.retrieval._retrieval_cache import RetrievalCache
from disco.retrieval.live import (
    Crawl4aiExtractionProvider,
    OpenAIEmbedder,
    ProviderConfigError,
    SearxngSearchProvider,
    SidecarNLIVerifier,
    TeiReranker,
    _resolve_extraction_wiring,
    _resolve_search_wiring,
    build_multi_search,
)
from disco.retrieval.models import Passage
from disco.retrieval.source_adapters import MultiSearchProvider


def _async(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


@pytest.fixture(autouse=True)
def no_real_extraction_pacing(monkeypatch: pytest.MonkeyPatch):
    """Record the arXiv/Wayback pacing waits instead of sleeping them, and give
    every test empty gates — the gates are PROCESS state and must not leak the
    reservation one test made into the wall-clock of the next."""
    waits: list[float] = []

    async def fake_sleep(delay: float) -> None:
        waits.append(delay)

    monkeypatch.setattr(_extraction_fallbacks, "_pace_sleep", fake_sleep)
    _extraction_fallbacks.reset_extraction_pacing()
    yield waits
    _extraction_fallbacks.reset_extraction_pacing()


@pytest.fixture(autouse=True)
def no_real_pdf_probe(monkeypatch: pytest.MonkeyPatch):
    """The mocked crawler's failed HTML pages have no PDF body to recover."""
    from disco.retrieval import _pdf_recovery

    async def original(url, **kwargs):
        return GuardedResponse(url, 200, {"content-type": "text/html"}, b"<p>HTML fixture</p>")

    monkeypatch.setattr(_pdf_recovery, "guarded_get", original)


# ---- SearXNG ----------------------------------------------------------------


async def test_searxng_maps_results_and_filters_domains():
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.params["format"] == "json"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": "https://good.com/a",
                        "title": "A",
                        "content": "snip",
                        "engine": "google",
                    },
                    {"url": "https://bad.com/b", "title": "B", "content": "x", "engine": "brave"},
                ]
            },
        )

    p = SearxngSearchProvider("http://x", transport=_async(handler))
    hits = await p.search("q", domains_deny=frozenset({"bad.com"}))
    assert [h.url for h in hits] == ["https://good.com/a"]  # bad.com filtered
    assert hits[0].snippet == "snip" and hits[0].source_engine == "google" and hits[0].rank == 0


async def test_searxng_dedupes_tracking_variants_and_keeps_engine_provenance():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": "https://www.toyota.com/all-vehicles/",
                        "title": "Toyota vehicles",
                        "content": "clean",
                        "engine": "yahoo",
                    },
                    {
                        "url": (
                            "https://www.toyota.com/all-vehicles/"
                            "?msockid=051546ee9cc06539396c512e9dea6439"
                        ),
                        "title": "Toyota vehicles",
                        "content": "tracked",
                        "engine": "bing",
                    },
                    {
                        "url": "https://example.com/merged",
                        "title": "Merged upstream",
                        "engines": ["bing", "yahoo"],
                    },
                ]
            },
        )

    provider = SearxngSearchProvider("http://x", transport=_async(handler))
    hits, diagnostic = await provider.search_detailed("q", limit=10)

    assert diagnostic["result_count"] == 3  # raw provider count remains observable
    assert [hit.url for hit in hits] == [
        "https://www.toyota.com/all-vehicles/",
        "https://example.com/merged",
    ]
    assert [hit.source_engine for hit in hits] == ["yahoo+bing", "bing+yahoo"]


async def test_searxng_exposes_unresponsive_engines_as_diagnostic():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"results": [], "unresponsive_engines": ["slow-engine", "broken-engine"]},
        )

    provider = SearxngSearchProvider("http://x", transport=_async(handler))
    assert await provider.search("q") == []
    hits, diagnostic = await provider.search_detailed("q")
    assert hits == []
    assert diagnostic["status_code"] == 200
    assert diagnostic["result_count"] == 0
    assert isinstance(diagnostic["latency_ms"], int)
    assert diagnostic["unresponsive_engines"] == ["slow-engine", "broken-engine"]


async def test_searxng_failure_degrades_to_no_hits():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    provider = SearxngSearchProvider("http://x", transport=_async(handler))
    assert await provider.search("q") == []
    hits, diagnostic = await provider.search_detailed("q")
    assert hits == []
    assert diagnostic["provider_error"] == "HTTPStatusError"
    assert diagnostic["status_code"] == 502
    assert diagnostic["result_count"] == 0
    assert isinstance(diagnostic["latency_ms"], int)


# ---- SearXNG: the low-trust wall --------------------------------------------


def _searxng_returning(*results: dict) -> SearxngSearchProvider:
    """A provider whose SearXNG instance answers with exactly these rows."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": list(results)})

    return SearxngSearchProvider("http://x", transport=_async(handler))


async def test_searxng_drops_a_low_trust_host_and_counts_it_in_the_diagnostic():
    provider = _searxng_returning(
        {"url": "https://reddit.com/r/ml/comments/1", "title": "Thread", "engine": "google"},
        {"url": "https://arxiv.org/abs/2401.00001", "title": "Paper", "engine": "google"},
    )

    hits, diagnostic = await provider.search_detailed("q")

    assert [hit.url for hit in hits] == ["https://arxiv.org/abs/2401.00001"]
    assert diagnostic["low_trust_dropped"] == 1


async def test_a_caller_allow_list_overrides_the_low_trust_wall_for_that_host():
    provider = _searxng_returning(
        {"url": "https://reddit.com/r/ml/comments/1", "title": "Thread", "engine": "google"},
        {"url": "https://arxiv.org/abs/2401.00001", "title": "Paper", "engine": "google"},
    )

    hits, diagnostic = await provider.search_detailed(
        "what do redditors say about x", domains_allow=frozenset({"reddit.com"})
    )

    assert [hit.url for hit in hits] == ["https://reddit.com/r/ml/comments/1"]
    assert "low_trust_dropped" not in diagnostic


async def test_low_trust_subdomains_are_dropped_with_their_registrable_domain():
    provider = _searxng_returning(
        {"url": "https://old.reddit.com/r/ml/comments/1", "title": "Thread", "engine": "google"},
        {"url": "https://someone.substack.com/p/post", "title": "Post", "engine": "google"},
        {"url": "https://www.linkedin.com/pulse/take", "title": "Take", "engine": "google"},
        {"url": "https://arxiv.org/abs/2401.00001", "title": "Paper", "engine": "google"},
    )

    hits, diagnostic = await provider.search_detailed("q")

    assert [hit.url for hit in hits] == ["https://arxiv.org/abs/2401.00001"]
    assert diagnostic["low_trust_dropped"] == 3


async def test_a_host_outside_the_low_trust_list_is_never_dropped():
    # The near-misses are the point: the wall is a host policy, not a substring
    # search, so a host that merely CONTAINS a listed domain must survive it.
    provider = _searxng_returning(
        {"url": "https://notreddit.com/a", "title": "A", "engine": "google"},
        {"url": "https://reddit.com.example.org/b", "title": "B", "engine": "google"},
        {"url": "https://www.nature.com/articles/c", "title": "C", "engine": "google"},
    )

    hits, _diagnostic = await provider.search_detailed("q")

    assert [hit.url for hit in hits] == [
        "https://notreddit.com/a",
        "https://reddit.com.example.org/b",
        "https://www.nature.com/articles/c",
    ]


async def test_the_low_trust_count_is_absent_when_the_wall_dropped_nothing():
    provider = _searxng_returning(
        {"url": "https://arxiv.org/abs/2401.00001", "title": "Paper", "engine": "google"},
    )

    _hits, diagnostic = await provider.search_detailed("q")

    assert "low_trust_dropped" not in diagnostic
    assert diagnostic["result_count"] == 1  # the key is absent from a real diagnostic


def test_build_multi_search_maps_ids_and_falls_back_to_the_keyless_composite():
    provider = build_multi_search(
        ["arxiv", "news", "wikipedia", "semantic_scholar", "searxng", "tavily", "unknown"],
        searxng_url="http://searx",
        tavily_key="tv",
        ss_key="ss",
    )

    assert isinstance(provider, MultiSearchProvider)
    assert [type(p).__name__ for p in provider._providers] == [
        "ArxivSearchProvider",
        "NewsSearchProvider",
        "WikipediaSearchProvider",
        "SemanticScholarSearchProvider",
        "SearxngSearchProvider",
        "TavilySearchProvider",
    ]

    fallback = build_multi_search([])
    assert [p.name for p in fallback._providers] == [
        "parallel",
        "exa",
        "wikipedia",
        "arxiv",
        "semantic_scholar",
    ]


def test_build_multi_search_migrates_the_retired_ddgs_source_id():
    """A conversation pinned to `ddgs` before its removal still resolves — to
    the two keyless web legs that took over its job, never to a crash."""
    provider = build_multi_search(["ddgs"])
    assert isinstance(provider, MultiSearchProvider)
    assert [p.name for p in provider._providers] == ["parallel", "exa"]


def test_build_multi_search_raises_when_requested_sources_are_unconstructible():
    """Explicitly requested sources are never silently swapped for another tier."""
    with pytest.raises(ProviderConfigError, match="could be constructed"):
        build_multi_search(["searxng"])  # no searxng_url → unconstructible


def test_unapproved_search_origin_raises_instead_of_a_silent_substitution():
    with pytest.raises(ProviderConfigError, match="approved trust origin"):
        _resolve_search_wiring("tavily", "", "key", "", {}, lambda *_a: False)


def test_unapproved_extraction_origin_raises_instead_of_silent_local():
    with pytest.raises(ProviderConfigError, match="approved trust origin"):
        _resolve_extraction_wiring("firecrawl", "", "key", "", {}, lambda *_a: False)


# ---- Crawl4AI ---------------------------------------------------------------


def _crawl(results: list[dict]):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "results": results})

    return handler


async def test_crawl4ai_extracts_content_and_chunks_passages():
    body = "Para one is long enough to be a passage here.\n\n" * 4
    handler = _crawl(
        [
            {
                "url": "https://en.wikipedia.org/wiki/X",
                "success": True,
                "status_code": 200,
                "markdown": {"fit_markdown": body, "raw_markdown": body},
                "metadata": {"title": "X — Wikipedia"},
            }
        ]
    )
    doc = await Crawl4aiExtractionProvider("http://x", transport=_async(handler)).extract(
        "https://en.wikipedia.org/wiki/X"
    )
    assert doc.status == "ok" and doc.fetched_ok
    assert doc.title == "X — Wikipedia"
    assert len(doc.passages) >= 1
    assert all(p.source_url == doc.url for p in doc.passages)


async def test_crawl4ai_falls_back_to_raw_markdown_when_fit_is_empty():
    handler = _crawl(
        [
            {
                "url": "https://example.com/post",
                "success": True,
                "status_code": 200,
                "markdown": {
                    "fit_markdown": "",
                    "raw_markdown": "Real content here, long enough to keep.",
                },
                "metadata": {"title": "Post"},
            }
        ]
    )
    doc = await Crawl4aiExtractionProvider("http://x", transport=_async(handler)).extract(
        "https://example.com/post"
    )
    assert doc.fetched_ok and "Real content here" in doc.content


async def test_crawl4ai_maps_failure_statuses_for_honest_rendering():
    handler = _crawl(
        [
            {
                "url": "https://example.com/blocked",
                "success": False,
                "status_code": 403,
                "markdown": {},
            }
        ]
    )
    doc = await Crawl4aiExtractionProvider("http://x", transport=_async(handler)).extract(
        "https://example.com/blocked"
    )
    assert doc.status == "blocked" and not doc.fetched_ok  # shown, not silently dropped


def _ok_result(url: str) -> dict:
    return {
        "url": url,
        "success": True,
        "status_code": 200,
        "markdown": {"fit_markdown": "Body text long enough to become a citable passage."},
        "metadata": {"title": "Title"},
    }


async def test_crawl4ai_batch_failure_is_isolated_per_url(
    no_real_backoff: list[float],
) -> None:
    """One request-level 500 used to mark the whole batch failed, which is how
    publisher PDFs and journals vanished while easy blogs survived."""
    requested: list[list[str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        urls = json.loads(req.content)["urls"]
        requested.append(urls)
        if len(urls) > 1:
            return httpx.Response(500, text="crawler exploded on the batch")
        url = urls[0]
        if url.endswith("/upstream"):
            return httpx.Response(500, text="upstream boom")
        if url.endswith("/blocked"):
            return httpx.Response(
                200,
                json={
                    "results": [{"url": url, "success": False, "status_code": 403, "markdown": {}}]
                },
            )
        return httpx.Response(200, json={"results": [_ok_result(url)]})

    urls = [
        "https://example.com/first",
        "https://example.org/upstream",
        "https://example.net/blocked",
        "https://example.com/last",
    ]
    docs = await Crawl4aiExtractionProvider("http://x", transport=_async(handler)).extract_many(
        urls
    )

    assert [doc.url for doc in docs] == urls  # order preserved
    assert [doc.fetched_ok for doc in docs] == [True, False, False, True]
    assert docs[2].status == "blocked"  # honest class kept, not flattened to "error"
    assert requested[0] == urls  # the batch was tried first
    # Transient (upstream 5xx) is retried exactly once; anti-bot never is.
    assert requested.count(["https://example.org/upstream"]) == 2
    assert requested.count(["https://example.net/blocked"]) == 1
    assert no_real_backoff == [0.75]


async def test_crawl4ai_follows_redirects_instead_of_discarding_the_source() -> None:
    """A 307 from the crawler used to be rejected; authoritative hosts answer 3xx."""
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.path)
        if req.url.path == "/crawl":
            return httpx.Response(307, headers={"location": "http://x/crawl/v2"})
        return httpx.Response(200, json={"results": [_ok_result("https://example.com/journal")]})

    doc = await Crawl4aiExtractionProvider("http://x", transport=_async(handler)).extract(
        "https://example.com/journal"
    )
    assert seen == ["/crawl", "/crawl/v2"]
    assert doc.fetched_ok and doc.status == "ok"


async def test_crawl4ai_egress_denial_fails_only_the_denied_url() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        urls = json.loads(req.content)["urls"]
        return httpx.Response(200, json={"results": [_ok_result(url) for url in urls]})

    docs = await Crawl4aiExtractionProvider("http://x", transport=_async(handler)).extract_many(
        ["http://127.0.0.1/private", "https://example.com/public"]
    )
    assert not docs[0].fetched_ok and docs[0].status == "error"
    assert docs[1].fetched_ok  # the sibling still reaches the evidence pool


# ---- arXiv abstracts through the export API ---------------------------------

_ARXIV_ID = "1706.03762"
_ARXIV_ABS_URL = f"https://arxiv.org/abs/{_ARXIV_ID}"
_ARXIV_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Attention Is
      All You Need</title>
    <summary>  The dominant sequence transduction models are based on complex
    recurrent or convolutional neural networks that include an encoder and a
    decoder, and the very best of them connect the two through attention.  </summary>
  </entry>
</feed>
"""


def _arxiv_api(status: int = 200, body: str = _ARXIV_FEED):
    """A transport that answers the export API and FAILS any /crawl attempt."""

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path != "/crawl", "an arXiv /abs page must never reach the crawler"
        assert req.url.host == "export.arxiv.org"
        return httpx.Response(status, text=body)

    return handler


async def test_arxiv_abstract_comes_from_the_export_api_and_never_from_the_crawler():
    """1,425 arXiv /abs pages went to the crawler across the 09-03 batches and 0
    came back ok; the API answers the same abstract with no JS challenge."""
    seen: list[str] = []
    api = _arxiv_api()

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        return api(req)

    doc = await Crawl4aiExtractionProvider("http://x", transport=_async(handler)).extract(
        _ARXIV_ABS_URL
    )
    assert seen == [f"https://export.arxiv.org/api/query?id_list={_ARXIV_ID}"]
    assert doc.status == "ok" and doc.fetched_ok
    assert doc.url == _ARXIV_ABS_URL  # the citation points at the abstract page
    assert doc.title == "Attention Is All You Need"
    assert doc.content.startswith("The dominant sequence transduction models are based")
    assert "\n" not in doc.content  # whitespace-collapsed
    assert doc.passages and all(p.source_url == _ARXIV_ABS_URL for p in doc.passages)


async def test_a_mixed_batch_splits_arxiv_from_the_crawler_and_keeps_input_order():
    crawled: list[list[str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "export.arxiv.org":
            return httpx.Response(200, text=_ARXIV_FEED)
        urls = json.loads(req.content)["urls"]
        crawled.append(urls)
        return httpx.Response(200, json={"results": [_ok_result(url) for url in urls]})

    urls = [
        "https://example.com/first",
        _ARXIV_ABS_URL,
        "https://arxiv.org/abs/astro-ph/0601001",
        "https://example.com/last",
    ]
    docs = await Crawl4aiExtractionProvider("http://x", transport=_async(handler)).extract_many(
        urls
    )
    assert [doc.url for doc in docs] == urls
    assert all(doc.fetched_ok for doc in docs)
    # Only the two non-arXiv URLs were ever posted to /crawl.
    assert crawled == [["https://example.com/first", "https://example.com/last"]]


async def test_an_arxiv_api_error_becomes_a_named_error_doc():
    doc = await Crawl4aiExtractionProvider(
        "http://x", transport=_async(_arxiv_api(500, "boom"))
    ).extract(_ARXIV_ABS_URL)
    assert not doc.fetched_ok and doc.status == "error"
    assert doc.error == "arxiv api http 500"


async def test_an_arxiv_feed_with_no_entry_is_a_miss_not_an_empty_abstract():
    empty = '<feed xmlns="http://www.w3.org/2005/Atom"></feed>'
    doc = await Crawl4aiExtractionProvider(
        "http://x", transport=_async(_arxiv_api(200, empty))
    ).extract(_ARXIV_ABS_URL)
    assert not doc.fetched_ok and doc.error == "arxiv api: no entry"


async def test_arxiv_api_calls_are_paced_one_every_three_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """arXiv's stated courtesy rate, driven by an injected clock — the gate is
    process state, so proving it with real seconds would cost the suite them."""
    clock = [1_000.0]
    waited: list[float] = []

    async def fake_sleep(delay: float) -> None:
        waited.append(delay)
        clock[0] += delay

    monkeypatch.setattr(_extraction_fallbacks, "_pace_now", lambda: clock[0])
    monkeypatch.setattr(_extraction_fallbacks, "_pace_sleep", fake_sleep)
    _extraction_fallbacks.reset_extraction_pacing()

    docs = await Crawl4aiExtractionProvider(
        "http://x", transport=_async(_arxiv_api())
    ).extract_many([f"https://arxiv.org/abs/240{i}.00001" for i in range(3)])
    assert [doc.fetched_ok for doc in docs] == [True, True, True]
    assert waited == [3.0, 3.0]  # the first goes straight out; each next waits its slot


# ---- Wayback fallback for anti-bot pages ------------------------------------

_BLOCKED_URL = "https://www.sciencedirect.com/science/article/pii/S1"
_ARCHIVED_URL = f"https://web.archive.org/web/2id_/{_BLOCKED_URL}"


# The abstract the challenge page was hiding, as the archive replays it. Two
# paragraphs, both long enough that `_chunk` keeps them, so a recovered doc has
# to have passages built from THIS html and not from the crawler's empty answer.
_ARCHIVED_HTML = (
    "<html><head><title>Cooperation in repeated games</title></head><body>"
    "<h1>Cooperation in repeated games</h1>"
    "<p>The archived copy carries the abstract the anti-bot challenge was"
    " hiding from the crawler, at enough length to become a citable passage.</p>"
    "</body></html>"
)


def _blocked_source():
    """Anti-bot on the source page. Records every batch posted to /crawl."""
    posted: list[list[str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        urls = json.loads(req.content)["urls"]
        posted.append(urls)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": url,
                        "success": False,
                        "status_code": 403,
                        "markdown": {},
                        "error_message": "Cloudflare challenge",
                    }
                    for url in urls
                ]
            },
        )

    return handler, posted


def _archive_answers(monkeypatch: pytest.MonkeyPatch, status: int, html: str = ""):
    """Answer the in-process archive fetch, and record the urls it asked for.

    The seam is the bundled extractor's own `guarded_get`, so these tests run the
    real fetch → markdown → chunk path with nothing on the wire.
    """
    asked: list[str] = []

    async def fake_get(url: str, **kwargs) -> GuardedResponse:
        asked.append(url)
        return GuardedResponse(
            url=url,
            status_code=status,
            headers={"content-type": "text/html; charset=utf-8"},
            content=html.encode(),
        )

    monkeypatch.setattr(bundled_providers, "guarded_get", fake_get)
    return asked


async def test_an_anti_bot_page_is_recovered_from_the_archive_under_its_own_url(
    monkeypatch: pytest.MonkeyPatch,
):
    """1,139 of the batch's failures were the same Cloudflare challenge; the
    archive has the page, and the citation must still name the publisher."""
    handler, _ = _blocked_source()
    asked = _archive_answers(monkeypatch, 200, _ARCHIVED_HTML)
    doc = await Crawl4aiExtractionProvider("http://x", transport=_async(handler)).extract(
        _BLOCKED_URL
    )
    assert asked == [_ARCHIVED_URL]
    assert doc.fetched_ok and doc.status == "ok"
    assert doc.url == _BLOCKED_URL  # NOT the archive
    assert "citable passage" in doc.content  # the ARCHIVED html, read here
    assert doc.passages and all(p.source_url == _BLOCKED_URL for p in doc.passages)


async def test_the_archived_copy_is_never_requested_through_the_crawler(
    monkeypatch: pytest.MonkeyPatch,
):
    """The Crawl4AI server sits behind an exit archive.org answers 429 to — 17 of
    17 attempts in one batch. Only the SOURCE may ever be posted to /crawl."""
    handler, posted = _blocked_source()
    _archive_answers(monkeypatch, 200, _ARCHIVED_HTML)
    await Crawl4aiExtractionProvider("http://x", transport=_async(handler)).extract(_BLOCKED_URL)
    assert posted == [[_BLOCKED_URL]]


async def test_a_refused_archived_copy_keeps_the_doc_and_logs_the_http_status(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """Some publishers are excluded from replay and the archive answers 403 —
    a real miss. The STATUS is what says so: the line used to print only the
    exception class, and a whole batch of 429s read as "found nothing"."""
    handler, _ = _blocked_source()
    _archive_answers(monkeypatch, 403)
    with caplog.at_level(logging.INFO, logger="disco.retrieval._extraction_fallbacks"):
        doc = await Crawl4aiExtractionProvider("http://x", transport=_async(handler)).extract(
            _BLOCKED_URL
        )
    assert not doc.fetched_ok and doc.status == "blocked"  # the honest class survives
    assert f"wayback copy of {_BLOCKED_URL}: HTTP 403" in [r.getMessage() for r in caplog.records]


async def test_no_snapshot_leaves_the_anti_bot_doc_exactly_as_it_was(
    monkeypatch: pytest.MonkeyPatch,
):
    handler, _ = _blocked_source()
    asked = _archive_answers(monkeypatch, 404)
    doc = await Crawl4aiExtractionProvider("http://x", transport=_async(handler)).extract(
        _BLOCKED_URL
    )
    assert asked == [_ARCHIVED_URL]
    assert not doc.fetched_ok and doc.status == "blocked"


async def test_a_failure_that_is_not_anti_bot_never_asks_the_archive():
    """A 404 and a timeout are facts the archive cannot change; asking it would
    spend the run's deadline on a guaranteed miss."""
    posted: list[list[str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        urls = json.loads(req.content)["urls"]
        posted.append(urls)
        return httpx.Response(
            200,
            json={
                "results": [
                    {"url": urls[0], "success": False, "status_code": 404, "markdown": {}},
                    {
                        "url": urls[1],
                        "success": False,
                        "markdown": {},
                        "error_message": "read timeout",
                    },
                ]
            },
        )

    docs = await Crawl4aiExtractionProvider("http://x", transport=_async(handler)).extract_many(
        ["https://example.com/gone", "https://example.org/slow"]
    )
    assert [doc.status for doc in docs] == ["not_found", "error"]
    assert posted == [["https://example.com/gone", "https://example.org/slow"]]


# ---- the retrieval cache, through the providers -----------------------------


def _one_result_search(calls: list[str]):
    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        return httpx.Response(
            200,
            json={
                "results": [
                    {"url": "https://a.example/x", "title": "A", "content": "s", "engine": "g"}
                ]
            },
        )

    return handler


async def test_a_repeat_search_is_served_from_the_cache_without_a_second_request(tmp_path):
    """The same eight questions are re-run every batch; only the spacing and the
    capitalisation differ, so the key normalizes both away."""
    calls: list[str] = []
    provider = SearxngSearchProvider(
        "http://x",
        transport=_async(_one_result_search(calls)),
        cache=RetrievalCache(tmp_path / "cache.sqlite"),
    )
    first = await provider.search("  Some   QUESTION ")
    hits, diagnostic = await provider.search_detailed("some question")
    assert len(calls) == 1  # the second search never went out
    assert [h.url for h in hits] == [h.url for h in first]
    assert diagnostic["cache"] == "hit"


async def test_a_degraded_search_answer_is_never_cached(tmp_path):
    """Caching an outage would freeze it: every re-run would inherit the day the
    engines were down."""
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        return httpx.Response(
            200,
            json={
                "results": [
                    {"url": "https://a.example/x", "title": "A", "content": "s", "engine": "g"}
                ],
                "unresponsive_engines": [["google", "timeout"]],
            },
        )

    provider = SearxngSearchProvider(
        "http://x", transport=_async(handler), cache=RetrievalCache(tmp_path / "cache.sqlite")
    )
    await provider.search("q")
    await provider.search("q")
    assert len(calls) == 2


async def test_an_empty_search_answer_is_never_cached(tmp_path):
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        return httpx.Response(200, json={"results": []})

    provider = SearxngSearchProvider(
        "http://x", transport=_async(handler), cache=RetrievalCache(tmp_path / "cache.sqlite")
    )
    await provider.search("q")
    await provider.search("q")
    assert len(calls) == 2


async def test_a_page_read_once_is_served_from_the_cache_on_the_next_run(tmp_path):
    posted: list[list[str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        urls = json.loads(req.content)["urls"]
        posted.append(urls)
        return httpx.Response(200, json={"results": [_ok_result(url) for url in urls]})

    cache = RetrievalCache(tmp_path / "cache.sqlite")
    url = "https://example.com/paper"
    first = await Crawl4aiExtractionProvider(
        "http://x", transport=_async(handler), cache=cache
    ).extract(url)
    second = await Crawl4aiExtractionProvider(
        "http://x", transport=_async(handler), cache=cache
    ).extract(url)
    assert posted == [[url]]  # the second run never refetched
    assert second.fetched_ok and second.url == url
    assert second.content == first.content
    assert [p.text for p in second.passages] == [p.text for p in first.passages]


async def test_a_failed_page_is_never_cached(tmp_path):
    """A cached failure deletes that source from every run for a week."""
    posted: list[list[str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        urls = json.loads(req.content)["urls"]
        posted.append(urls)
        return httpx.Response(
            200,
            json={
                "results": [{"url": urls[0], "success": False, "status_code": 404, "markdown": {}}]
            },
        )

    cache = RetrievalCache(tmp_path / "cache.sqlite")
    url = "https://example.com/gone"
    for _ in range(2):
        doc = await Crawl4aiExtractionProvider(
            "http://x", transport=_async(handler), cache=cache
        ).extract(url)
        assert not doc.fetched_ok
    assert posted == [[url], [url]]


# ---- reranker ---------------------------------------------------------------


def _passages(*texts: str) -> list[Passage]:
    return [
        Passage(id=str(i), source_url="u", source_title="t", text=t) for i, t in enumerate(texts)
    ]


async def test_reranker_orders_by_score_and_keeps_top_k():
    def handler(req: httpx.Request) -> httpx.Response:
        # relevant passage (index 1) scores highest
        return httpx.Response(200, json=[{"index": 0, "score": -5.0}, {"index": 1, "score": 8.0}])

    ranked = await TeiReranker("http://x", transport=_async(handler)).rerank(
        "q", _passages("irrelevant", "relevant"), top_k=1
    )
    assert [p.text for p in ranked] == ["relevant"]


async def test_reranker_failure_degrades_to_input_order():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    ps = _passages("a", "b", "c")
    out = await TeiReranker("http://x", transport=_async(handler)).rerank("q", ps, top_k=2)
    assert [p.text for p in out] == ["a", "b"]  # input order, not a crash


# ---- embedder ---------------------------------------------------------------


async def test_embedder_reads_openai_embeddings():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"data": [{"embedding": [0.1, 0.2]}, {"embedding": [0.3, 0.4]}]}
        )

    vecs = await OpenAIEmbedder("http://x/v1", transport=_async(handler)).embed(["a", "b"])
    assert vecs == [[0.1, 0.2], [0.3, 0.4]]


# ---- NLI sidecar ------------------------------------------------------------


async def test_nli_maps_label_to_verdict_and_caches():
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"entailment": 0.97, "label": "entailment"})

    nli = SidecarNLIVerifier("http://x", transport=httpx.MockTransport(handler))
    assert nli.entail("p", "h") == "entail"
    assert nli.score("p", "h") == 0.97
    assert calls["n"] == 1  # entail()+score() for the same pair = one HTTP call (cached)


async def test_nli_failure_degrades_to_neutral():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="down")

    nli = SidecarNLIVerifier("http://x", transport=httpx.MockTransport(handler))
    assert nli.entail("p", "h") == "neutral"  # weak, never a crash
    assert nli.score("p", "h") == 0.0


async def test_nli_failures_are_counted_so_the_degradation_is_not_silent():
    """The neutral fallback stays (grounding must never invent a contradiction),
    but the no-op is COUNTED so the report can declare partial grounding."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="down")

    nli = SidecarNLIVerifier("http://x", transport=httpx.MockTransport(handler))
    assert nli.verifier_failures == 0
    nli.entail("premise one", "claim one")
    nli.score("premise one", "claim one")  # cached — the same pair costs one call
    nli.entail("premise two", "claim two")
    assert nli.verifier_failures == 2


async def test_nli_success_records_no_failures():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"label": "entailment", "entailment": 0.9, "scores": {}})

    nli = SidecarNLIVerifier("http://x", transport=httpx.MockTransport(handler))
    assert nli.entail("p", "h") == "entail"
    assert nli.verifier_failures == 0


# ---- W-33 encoder pre-flight probes -----------------------------------------


async def test_reranker_probe_ok_when_endpoint_answers():

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="ok")

    # A 200 (or any HTTP status) means the host answered → reachable.
    await TeiReranker("http://x", transport=httpx.MockTransport(handler)).probe()


async def test_reranker_probe_ok_even_on_404():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="no route")

    # Non-2xx still proves the service is REACHABLE — pre-flight must not fail it.
    await TeiReranker("http://x", transport=httpx.MockTransport(handler)).probe()


async def test_reranker_probe_raises_named_on_connect_error():
    import pytest
    from disco.retrieval.local_encoders import EncoderUnavailable

    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(EncoderUnavailable) as ei:
        await TeiReranker("http://dead:8091", transport=httpx.MockTransport(handler)).probe()
    msg = str(ei.value)
    assert "reranker" in msg and "dead:8091" in msg  # NAMES the encoder + url


async def test_reranker_probe_raises_named_on_empty_url():
    import pytest
    from disco.retrieval.local_encoders import EncoderUnavailable

    with pytest.raises(EncoderUnavailable) as ei:
        await TeiReranker("").probe()
    assert "reranker_url is empty" in str(ei.value)


async def test_embedder_probe_raises_named_on_connect_error():
    import pytest
    from disco.retrieval.local_encoders import EncoderUnavailable

    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(EncoderUnavailable) as ei:
        await OpenAIEmbedder("http://dead:8090/v1", transport=httpx.MockTransport(handler)).probe()
    assert "embedder" in str(ei.value) and "dead:8090" in str(ei.value)


async def test_nli_probe_ok_and_named_failure():
    import pytest
    from disco.retrieval.local_encoders import EncoderUnavailable

    def ok(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    await SidecarNLIVerifier("http://x", transport=httpx.MockTransport(ok)).probe()

    def dead(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(EncoderUnavailable) as ei:
        await SidecarNLIVerifier("http://dead:8092", transport=httpx.MockTransport(dead)).probe()
    assert "NLI" in str(ei.value) and "dead:8092" in str(ei.value)


# ---- P1-4: catch ALL httpx errors (incl. InvalidURL) + deadline-bound -------


async def test_reranker_probe_converts_invalid_url_to_named_unavailable():
    """P1-4: a malformed URL (e.g. an invalid port) raises httpx.InvalidURL — NOT
    an httpx.HTTPError — when the request is built. The probe must convert it to a
    NAMED EncoderUnavailable, never let it escape uncaught."""
    import pytest
    from disco.retrieval.local_encoders import EncoderUnavailable

    # No transport: the real client raises httpx.InvalidURL parsing the bad port.
    with pytest.raises(EncoderUnavailable) as ei:
        await TeiReranker("http://host:notaport").probe()
    assert "reranker" in str(ei.value)


async def test_embedder_probe_converts_invalid_url_to_named_unavailable():
    """P1-4: same InvalidURL coverage for the embedder probe."""
    import pytest
    from disco.retrieval.local_encoders import EncoderUnavailable

    with pytest.raises(EncoderUnavailable) as ei:
        await OpenAIEmbedder("http://host:notaport").probe()
    assert "embedder" in str(ei.value)


async def test_nli_probe_converts_invalid_url_to_named_unavailable():
    """P1-4: same InvalidURL coverage for the NLI probe (the sync-transport path).
    The InvalidURL raised inside the worker thread must propagate out as a NAMED
    EncoderUnavailable, not an uncaught error."""
    import pytest
    from disco.retrieval.local_encoders import EncoderUnavailable

    with pytest.raises(EncoderUnavailable) as ei:
        await SidecarNLIVerifier("http://host:notaport").probe()
    assert "NLI" in str(ei.value)


async def test_nli_probe_is_deadline_bounded(monkeypatch):
    """P1-4: the sync NLI probe runs under an OUTER asyncio.wait_for deadline, so a
    hung sidecar (a transport that blocks past its own timeout) can NEVER stall
    Deep Research. Proven: a handler that blocks far longer than the deadline still
    returns a NAMED EncoderUnavailable within the (tiny) deadline window."""
    import time

    import disco.retrieval.live as live_mod
    import pytest
    from disco.retrieval.local_encoders import EncoderUnavailable

    monkeypatch.setattr(live_mod, "_PROBE_DEADLINE_S", 0.05)

    def slow(req: httpx.Request) -> httpx.Response:
        time.sleep(0.6)  # block the worker thread well past the deadline
        return httpx.Response(200, json={})

    nli = SidecarNLIVerifier("http://slow:8092", transport=httpx.MockTransport(slow))
    t0 = time.monotonic()
    with pytest.raises(EncoderUnavailable) as ei:
        await nli.probe()
    elapsed = time.monotonic() - t0
    assert elapsed < 0.4  # bounded by the 0.05s deadline, NOT the 0.6s block
    assert "NLI" in str(ei.value)
