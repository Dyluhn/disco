"""Providers/two-slots (§8.1) + quality pipeline (§8.2)."""

from __future__ import annotations

from disco.retrieval import (
    DefaultRetrievalEngine,
    LexicalReranker,
    RetrievalRequest,
    reciprocal_rank_fusion,
)
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
    assert any(h.url == "http://x/paywall" for h in res.all_hits)  # still discovered


# ---- §8.2 the quality pipeline ----------------------------------------------


def test_rrf_rewards_cross_query_agreement():
    """A URL ranked across multiple queries outranks one high in only one."""
    list_a = [hit("http://both"), hit("http://only_a")]
    list_b = [hit("http://only_b"), hit("http://both")]
    fused = reciprocal_rank_fusion([list_a, list_b])
    assert fused[0].url == "http://both"


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
