"""Coverage for the engine's corpus path, the router-backed rewriter, and a
couple of NLI edges — shipped paths not hit by the scenario tests."""

from __future__ import annotations

from conftest import FakeExtractionProvider, FakeRouter, FakeSearchProvider
from perpleximanus.retrieval import (
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


def test_nli_empty_hypothesis_scores_zero():
    assert CrossEncoderNLIVerifier().score("anything", "") == 0.0
