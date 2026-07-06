"""Live HTTP retrieval providers — hermetic tests (httpx.MockTransport).

No network: mock transports return canned backend responses so we test the
mapping to the existing interfaces (SearchHit / ExtractedDoc / Passage / verdicts)
and the graceful-degradation paths. (The live smoke against the real LAN endpoints
is run separately.)
"""

from __future__ import annotations

import httpx
from disco.retrieval.live import (
    Crawl4aiExtractionProvider,
    OpenAIEmbedder,
    SearxngSearchProvider,
    SidecarNLIVerifier,
    TeiReranker,
    build_multi_search,
)
from disco.retrieval.models import Passage
from disco.retrieval.source_adapters import MultiSearchProvider


def _async(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


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


async def test_searxng_failure_degrades_to_no_hits():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    assert await SearxngSearchProvider("http://x", transport=_async(handler)).search("q") == []


def test_build_multi_search_maps_ids_and_falls_back_to_ddgs():
    provider = build_multi_search(
        ["arxiv", "news", "ddgs", "semantic_scholar", "searxng", "tavily", "unknown"],
        searxng_url="http://searx",
        tavily_key="tv",
        ss_key="ss",
    )

    assert isinstance(provider, MultiSearchProvider)
    assert [type(p).__name__ for p in provider._providers] == [
        "ArxivSearchProvider",
        "NewsSearchProvider",
        "DdgsSearchProvider",
        "SemanticScholarSearchProvider",
        "SearxngSearchProvider",
        "TavilySearchProvider",
    ]

    fallback = build_multi_search([])
    assert type(fallback).__name__ == "DdgsSearchProvider"


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
        await TeiReranker(
            "http://dead:8091", transport=httpx.MockTransport(handler)
        ).probe()
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
        await OpenAIEmbedder(
            "http://dead:8090/v1", transport=httpx.MockTransport(handler)
        ).probe()
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
        await SidecarNLIVerifier(
            "http://dead:8092", transport=httpx.MockTransport(dead)
        ).probe()
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
