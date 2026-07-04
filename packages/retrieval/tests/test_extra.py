"""Coverage for the engine's corpus path, the router-backed rewriter, and a
couple of NLI edges — shipped paths not hit by the scenario tests."""

from __future__ import annotations

from disco.retrieval import (
    CrossEncoderNLIVerifier,
    DefaultCorpusService,
    DefaultRetrievalEngine,
    ExtractedDoc,
    HashingEmbedder,
    InMemoryVectorStore,
    LexicalReranker,
    Passage,
    RetrievalRequest,
    RouterQueryRewriter,
)
from research_fakes import FakeExtractionProvider, FakeRouter, FakeSearchProvider


async def test_engine_retrieves_from_space_corpus():
    """corpus_ids scopes retrieval into a Space corpus via the vector store (§4)."""
    store = InMemoryVectorStore()
    embedder = HashingEmbedder()
    corpora = DefaultCorpusService(store, embedder)
    await corpora.ingest(
        "space_X",
        owner_id="local",
        docs=[
            ExtractedDoc(
                url="u",
                title="t",
                content="quantum entanglement notes",
                passages=[
                    Passage(
                        id="u_p0",
                        source_url="u",
                        source_title="t",
                        text="quantum entanglement notes",
                    )
                ],
            )
        ],
    )
    engine = DefaultRetrievalEngine(
        FakeSearchProvider([]),
        FakeExtractionProvider({}),
        LexicalReranker(),
        embedder=embedder,
        vector_store=store,
    )
    res = await engine.retrieve(
        RetrievalRequest(query="quantum", use_web=False, corpus_ids=frozenset({"space_X"}))
    )
    assert res.passages and res.passages[0].corpus_id == "space_X"
    assert res.all_hits == []  # no web discovery when use_web is False


async def test_router_query_rewriter_parses_lines():
    router = FakeRouter(rewriter_text="first paraphrase\nsecond paraphrase\nthird paraphrase")
    rw = RouterQueryRewriter(router)
    many = await rw.rewrite("q", n=4)
    assert many == ["first paraphrase", "second paraphrase", "third paraphrase"]
    one = await rw.rewrite("q", n=1)
    assert one == ["first paraphrase"]


async def test_router_query_rewriter_strips_leaked_think():
    """A leaked <think> preamble must never become the engine query (live-caught:
    its first line went to the search engine verbatim and poisoned the corpus)."""
    router = FakeRouter(
        rewriter_text="<think>Okay, the user wants me to rewrite\na title following a spec"
        "</think>\ntransatlantic cable 1866 finance"
    )
    rw = RouterQueryRewriter(router)
    assert await rw.rewrite("q", n=1) == ["transatlantic cable 1866 finance"]
    # unclosed trailing think (budget ran out) → nothing usable → original query
    router = FakeRouter(rewriter_text="<think>hmm, let me consider what")
    rw = RouterQueryRewriter(router)
    assert await rw.rewrite("q", n=2) == ["q"]


def test_nli_empty_hypothesis_scores_zero():
    assert CrossEncoderNLIVerifier().score("anything", "") == 0.0
