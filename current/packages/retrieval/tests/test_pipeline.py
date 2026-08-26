"""Providers/two-slots (§8.1) + quality pipeline (§8.2)."""

from __future__ import annotations

import asyncio
import datetime

import pytest
from disco.retrieval import (
    DefaultRetrievalEngine,
    LexicalReranker,
    RetrievalRequest,
    reciprocal_rank_fusion,
)
from disco.retrieval.engine import ProviderOperationError
from disco.retrieval.models import ExtractedDoc, Passage
from disco.retrieval.source_adapters import MultiSearchProvider
from research_fakes import FakeExtractionProvider, FakeRewriter, FakeSearchProvider, hit


def _engine(search, *, docs, failing=None, rewriter=None):
    return DefaultRetrievalEngine(
        search,
        FakeExtractionProvider(docs, failing=failing),
        LexicalReranker(),
        rewriter=rewriter,
    )


# ---- §8.1 providers & the two slots -----------------------------------------


async def test_citations_come_from_extracted_passages_not_snippets():
    """The pipeline never treats a SearchHit.snippet as cited content (§1.4)."""
    search = FakeSearchProvider([hit("http://x/doc", snippet="SNIPPET-NOT-CONTENT")])
    eng = _engine(search, docs={"http://x/doc": "real extracted body about cats"})
    res = await eng.retrieve(RetrievalRequest(query="cats", depth="shallow"))
    assert res.passages, "expected extracted passages"
    assert all("SNIPPET-NOT-CONTENT" not in p.text for p in res.passages)
    assert res.passages[0].text == "real extracted body about cats"


async def test_provider_swap_is_transparent():
    """The engine runs identically against two differently-named providers."""
    docs = {"http://x/a": "alpha content"}
    r1 = await _engine(FakeSearchProvider([hit("http://x/a")], name="searxng"), docs=docs).retrieve(
        RetrievalRequest(query="alpha", depth="shallow")
    )
    r2 = await _engine(FakeSearchProvider([hit("http://x/a")], name="serper"), docs=docs).retrieve(
        RetrievalRequest(query="alpha", depth="shallow")
    )
    assert [p.text for p in r1.passages] == [p.text for p in r2.passages]


async def test_explicit_failure_is_first_class():
    """A blocked/paywalled URL appears in all_hits with the right status, never
    silently dropped (§2.2)."""
    search = FakeSearchProvider([hit("http://x/ok"), hit("http://x/paywall")])
    eng = _engine(
        search,
        docs={"http://x/ok": "open content"},
        failing={"http://x/paywall": "paywalled"},
    )
    res = await eng.retrieve(RetrievalRequest(query="content", depth="shallow"))
    paywall = next(d for d in res.extracted if d.url == "http://x/paywall")
    assert paywall.fetched_ok is False and paywall.status == "paywalled"
    paywall_hit = next(h for h in res.all_hits if h.url == "http://x/paywall")
    assert paywall_hit.status == "paywalled"  # still discovered, honestly unread


async def test_request_bounds_discovery_extraction_and_marks_unattempted_hits():
    hits = [hit(f"http://x/{index}") for index in range(5)]
    docs = {f"http://x/{index}": f"body {index}" for index in range(5)}
    search = FakeSearchProvider(hits)
    result = await _engine(search, docs=docs).retrieve(
        RetrievalRequest(
            query="body",
            depth="shallow",
            top_k=4,
            discover_limit=3,
            extract_cap=1,
        )
    )

    assert len(result.all_hits) == 3
    assert len(result.extracted) == 1
    assert [item.status for item in result.all_hits] == ["ok", None, None]


async def test_extraction_status_uses_canonical_source_identity():
    class _CanonicalizingExtraction:
        async def extract_many(self, urls: list[str]) -> list[ExtractedDoc]:
            return [
                ExtractedDoc(
                    url="https://example.com/release",
                    title="Release",
                    content="released",
                    passages=[
                        Passage(
                            id="release",
                            source_url="https://example.com/release",
                            source_title="Release",
                            text="released",
                        )
                    ],
                    status="ok",
                )
            ]

    search = FakeSearchProvider(
        [hit("https://example.com/release/?utm_source=newsletter#details")]
    )
    engine = DefaultRetrievalEngine(
        search,
        _CanonicalizingExtraction(),  # type: ignore[arg-type]
        LexicalReranker(),
    )

    result = await engine.retrieve(RetrievalRequest(query="release", depth="shallow"))

    assert result.all_hits[0].status == "ok"


async def test_discovery_date_reaches_citable_passage() -> None:
    dated = datetime.date(2026, 8, 11)
    search = FakeSearchProvider(
        [hit("https://example.com/release").model_copy(update={"published_at": dated})]
    )

    result = await _engine(
        search,
        docs={"https://example.com/release": "A model was released."},
    ).retrieve(RetrievalRequest(query="release", depth="shallow"))

    assert result.passages[0].published_at == dated


# ---- §8.2 the quality pipeline ----------------------------------------------


def test_rrf_rewards_cross_query_agreement():
    """A URL ranked across multiple queries outranks one high in only one."""
    list_a = [hit("http://both"), hit("http://only_a")]
    list_b = [hit("http://only_b"), hit("http://both")]
    fused = reciprocal_rank_fusion([list_a, list_b])
    assert fused[0].url == "http://both"


def test_rrf_dedupes_tracking_variants_of_the_same_source():
    fused = reciprocal_rank_fusion(
        [
            [hit("https://example.com/story?utm_source=a")],
            [hit("http://example.com/story#section")],
        ]
    )
    assert len(fused) == 1


def test_rrf_counts_a_source_once_per_input_list():
    duplicate = hit("https://example.com/a?msockid=abc").model_copy(
        update={"source_engine": "bing"}
    )
    clean = hit("https://example.com/a").model_copy(update={"source_engine": "yahoo"})
    corroborated = hit("https://example.com/b")

    fused = reciprocal_rank_fusion(
        [[clean, duplicate, corroborated], [corroborated]]
    )

    assert fused[0].url == corroborated.url
    source_a = next(item for item in fused if item.url == clean.url)
    assert source_a.source_engine == "yahoo+bing"


async def test_single_query_dedupes_before_extraction():
    clean = "https://www.toyota.com/all-vehicles/"
    tracked = f"{clean}?msockid=051546ee9cc06539396c512e9dea6439"
    result = await _engine(
        FakeSearchProvider([hit(clean), hit(tracked)]),
        docs={clean: "one extracted page"},
    ).retrieve(RetrievalRequest(query="Toyota vehicles", depth="shallow"))

    assert [item.url for item in result.all_hits] == [clean]
    assert [item.url for item in result.extracted] == [clean]


async def test_depth_controls_issued_queries():
    search = FakeSearchProvider([hit("http://x/a")])
    rewriter = FakeRewriter(["q paraphrase 1", "q paraphrase 2", "q paraphrase 3"])
    eng = _engine(search, docs={"http://x/a": "body"}, rewriter=rewriter)
    shallow = await eng.retrieve(RetrievalRequest(query="q", depth="shallow"))
    deep = await eng.retrieve(RetrievalRequest(query="q", depth="deep"))
    assert shallow.issued_queries == ["q"]  # raw query, no rewrite
    assert len(deep.issued_queries) >= 3  # multi-query


async def test_rerank_reduces_to_top_k_and_all_hits_distinct():
    hits = [hit(f"http://x/{i}") for i in range(5)]
    docs = {f"http://x/{i}": f"document number {i} about pipelines" for i in range(5)}
    eng = _engine(FakeSearchProvider(hits), docs=docs)
    res = await eng.retrieve(RetrievalRequest(query="pipelines document", top_k=3, depth="shallow"))
    assert len(res.passages) == 3  # reduced to top_k
    assert len(res.all_hits) == 5  # full discovery set, distinct from reranked passages


async def test_retrieval_trace_keeps_rewritten_queries_and_bounded_provider_facts():
    search_hit = hit(
        "https://example.test/story?token=secret-value&utm_source=newsletter",
        title="A " * 300,
        snippet="provider text is bounded and marked untrusted in the trace",
    ).model_copy(update={"source_engine": "search-engine"})
    search = FakeSearchProvider([search_hit], name="fake-provider")
    eng = _engine(
        search,
        docs={search_hit.url: "A substantive extracted passage about the subject."},
        rewriter=FakeRewriter(["rewritten query"]),
    )

    result = await eng.retrieve(RetrievalRequest(query="A full planned question", depth="standard"))
    trace = result.notes["retrieval_trace"]

    assert trace["planned_query"] == "A full planned question"
    assert trace["issued_queries"] == ["rewritten query"]
    assert trace["raw_discovered_hit_count"] == 1
    assert trace["reranked_passage_count"] == 1
    assert trace["extraction"] == {
        "attempted": 1,
        "success": 1,
        "failure": 0,
        "statuses": [
            {
                "url": "https://example.test/story?token=%5BREDACTED%5D&utm_source=newsletter",
                "title": search_hit.url,
                "status": "ok",
                "fetched_ok": True,
            }
        ],
    }
    query_trace = trace["queries"][0]
    assert query_trace["issued_query"] == "rewritten query"
    assert query_trace["raw_discovered_hit_count"] == 1
    assert len(query_trace["hits"]) == 1
    traced_hit = query_trace["hits"][0]
    assert traced_hit["title"].startswith("A A A") and len(traced_hit["title"]) <= 240
    assert traced_hit["url"] == (
        "https://example.test/story?token=%5BREDACTED%5D&utm_source=newsletter"
    )
    assert traced_hit["engine"] == "search-engine" and traced_hit["status"] == "ok"
    assert traced_hit["snippet"] == {
        "text": "provider text is bounded and marked untrusted in the trace",
        "untrusted": True,
    }
    assert len(traced_hit["snippet"]["text"]) <= 300


async def test_retrieval_trace_classifies_extraction_failures_without_error_text():
    class _ClassifiedExtraction:
        async def extract_many(self, urls: list[str]) -> list[ExtractedDoc]:
            errors = {
                "https://example.test/blocked": ("blocked", "captcha challenge"),
                "https://example.test/redirect": ("error", "http 307"),
                "https://example.test/upstream": ("error", "HTTP 500 Internal Server Error"),
                "https://example.test/timeout": ("error", "ReadTimeout while fetching"),
                "https://example.test/empty": ("error", "no readable content"),
                "https://example.test/other": ("error", "resolved address 127.0.0.1 is denied"),
            }
            return [
                ExtractedDoc(
                    url=url,
                    title=url,
                    content="",
                    fetched_ok=False,
                    error=errors[url][1],
                    status=errors[url][0],  # type: ignore[arg-type]
                )
                for url in urls
            ]

    urls = [
        "https://example.test/blocked",
        "https://example.test/redirect",
        "https://example.test/upstream",
        "https://example.test/timeout",
        "https://example.test/empty",
        "https://example.test/other",
    ]
    result = await DefaultRetrievalEngine(
        FakeSearchProvider([hit(url) for url in urls]),
        _ClassifiedExtraction(),  # type: ignore[arg-type]
        LexicalReranker(),
    ).retrieve(RetrievalRequest(query="failure classes", depth="shallow"))

    statuses = result.notes["retrieval_trace"]["extraction"]["statuses"]
    assert [row["error_class"] for row in statuses] == [
        "anti_bot",
        "redirect/http_307",
        "upstream_http_500",
        "timeout",
        "empty_content",
        "other",
    ]
    serialized = str(result.notes["retrieval_trace"])
    assert "captcha challenge" not in serialized
    assert "127.0.0.1" not in serialized


async def test_exhausted_detailed_extraction_failure_is_typed():
    class _FailedDetailedExtraction:
        name = "firecrawl"

        async def extract_many(self, urls: list[str]) -> list[ExtractedDoc]:
            return [
                ExtractedDoc(
                    url=url,
                    title=url,
                    content="",
                    fetched_ok=False,
                    error="firecrawl rate_limited",
                    status="error",
                )
                for url in urls
            ]

        async def extract_many_detailed(self, urls: list[str]):
            return await self.extract_many(urls), {
                "provider": "firecrawl",
                "outcome": "rate_limited",
                "attempts": len(urls) * 3,
                "status_code": 429,
            }

    with pytest.raises(ProviderOperationError) as raised:
        await DefaultRetrievalEngine(
            FakeSearchProvider([hit("https://example.test/exhausted")]),
            _FailedDetailedExtraction(),  # type: ignore[arg-type]
            LexicalReranker(),
        ).retrieve(RetrievalRequest(query="extraction outage", depth="shallow"))
    assert raised.value.diagnostic == {
        "extraction": {
            "provider": "firecrawl",
            "outcome": "rate_limited",
            "attempts": 3,
            "status_code": 429,
        }
    }


async def test_detailed_extraction_empty_and_partial_success_remain_normal():
    class _DetailedExtraction:
        name = "firecrawl"

        def __init__(self, docs: list[ExtractedDoc], outcome: str) -> None:
            self._docs = docs
            self._outcome = outcome

        async def extract_many(self, urls: list[str]) -> list[ExtractedDoc]:
            del urls
            return self._docs

        async def extract_many_detailed(self, urls: list[str]):
            del urls
            return self._docs, {
                "provider": "firecrawl",
                "outcome": self._outcome,
                "attempts": 1,
                "status_code": 200,
            }

    empty_url = "https://example.test/empty-content"
    empty = await DefaultRetrievalEngine(
        FakeSearchProvider([hit(empty_url)]),
        _DetailedExtraction(
            [
                ExtractedDoc(
                    url=empty_url,
                    title=empty_url,
                    content="",
                    fetched_ok=False,
                    error="no readable content",
                    status="error",
                )
            ],
            "empty",
        ),
        LexicalReranker(),
    ).retrieve(RetrievalRequest(query="valid empty", depth="shallow"))
    assert not empty.passages
    assert empty.notes["retrieval_trace"]["extraction"]["provider_diagnostic"]["outcome"] == "empty"

    good_url = "https://example.test/good"
    bad_url = "https://example.test/bad"
    partial = await DefaultRetrievalEngine(
        FakeSearchProvider([hit(good_url), hit(bad_url)]),
        _DetailedExtraction(
            [
                ExtractedDoc(
                    url=good_url,
                    title="Good",
                    content="usable",
                    fetched_ok=True,
                    passages=[
                        Passage(
                            id="good-p0",
                            source_url=good_url,
                            source_title="Good",
                            text="usable evidence",
                        )
                    ],
                ),
                ExtractedDoc(
                    url=bad_url,
                    title=bad_url,
                    content="",
                    fetched_ok=False,
                    error="timeout",
                    status="error",
                ),
            ],
            "partial_outage",
        ),
        LexicalReranker(),
    ).retrieve(RetrievalRequest(query="partial extraction", depth="shallow"))
    assert partial.passages
    assert (
        partial.notes["retrieval_trace"]["extraction"]["provider_diagnostic"]["outcome"]
        == "partial_outage"
    )


async def test_retrieval_trace_preserves_provider_degradation_diagnostic():
    class _DegradedSearch(FakeSearchProvider):
        async def search_detailed(
            self,
            query,
            *,
            limit=10,
            domains_allow=None,
            domains_deny=None,
            time_filter=None,
        ):
            del domains_allow, domains_deny, time_filter
            diagnostic = {
                "providers": {"searxng": {"unresponsive_engines": ["slow-engine"]}}
            }
            return await super().search(
                query,
                limit=limit,
                domains_allow=None,
                domains_deny=None,
                time_filter=None,
            ), diagnostic

    search = _DegradedSearch([])
    result = await _engine(search, docs={}).retrieve(
        RetrievalRequest(query="empty result", depth="shallow")
    )
    trace = result.notes["retrieval_trace"]
    assert trace["raw_discovered_hit_count"] == 0
    assert trace["provider_diagnostics"] == [
        {"providers": {"searxng": {"unresponsive_engines": ["slow-engine"]}}}
    ]
    assert trace["queries"][0]["provider_diagnostic"] == trace["provider_diagnostics"][0]


async def test_all_paid_provider_failure_is_typed_not_no_hits():
    class _FailedPaidSearch:
        name = "multi"

        async def search_detailed(self, query, **kwargs):
            del query, kwargs
            return [], {
                "provider_aggregate": "all_failed",
                "providers": {
                    "tavily": {
                        "provider": "tavily",
                        "outcome": "rate_limited",
                        "attempts": 3,
                    }
                }
            }

    with pytest.raises(ProviderOperationError):
        await _engine(_FailedPaidSearch(), docs={}).retrieve(
            RetrievalRequest(query="paid outage", depth="shallow")
        )


async def test_direct_paid_provider_failure_is_typed_not_no_hits():
    class _DirectFailedPaidSearch:
        name = "tavily"

        async def search_detailed(self, query, **kwargs):
            del query, kwargs
            return [], {
                "provider": "tavily",
                "outcome": "rate_limited",
                "attempts": 3,
                "status_code": 429,
            }

    with pytest.raises(ProviderOperationError):
        await _engine(_DirectFailedPaidSearch(), docs={}).retrieve(
            RetrievalRequest(query="direct paid outage", depth="shallow")
        )


async def test_valid_paid_empty_remains_normal_zero_hits():
    class _EmptyPaidSearch:
        name = "tavily"

        async def search_detailed(self, query, **kwargs):
            del query, kwargs
            return [], {
                "provider": "tavily",
                "outcome": "empty",
                "attempts": 1,
                "status_code": 200,
            }

    result = await _engine(_EmptyPaidSearch(), docs={}).retrieve(
        RetrievalRequest(query="valid empty", depth="shallow")
    )
    assert result.all_hits == []


async def test_request_local_provider_diagnostics_do_not_cross_concurrent_queries():
    class _InterleavedSearch:
        name = "interleaved"

        async def search_detailed(self, query, **kwargs):
            del kwargs
            # Force B to finish first. A shared provider snapshot would then
            # attribute B's diagnostic to A when A resumes.
            await asyncio.sleep(0.01 if query == "query A" else 0)
            return [hit(f"http://example.test/{query[-1]}")], {"query": query}

        async def search(self, query, **kwargs):
            hits, _diagnostic = await self.search_detailed(query, **kwargs)
            return hits

    search = MultiSearchProvider((_InterleavedSearch(),))
    engine = _engine(
        search,
        docs={
            "http://example.test/A": "Substantive evidence for query A.",
            "http://example.test/B": "Substantive evidence for query B.",
        },
    )
    results = await asyncio.gather(
        engine.retrieve(RetrievalRequest(query="query A", depth="shallow")),
        engine.retrieve(RetrievalRequest(query="query B", depth="shallow")),
    )
    diagnostics = [
        result.notes["retrieval_trace"]["queries"][0]["provider_diagnostic"]
        for result in results
    ]
    assert diagnostics == [
        {"providers": {"interleaved": {"query": "query A"}}},
        {"providers": {"interleaved": {"query": "query B"}}},
    ]
