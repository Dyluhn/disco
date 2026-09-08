"""Discovery-only retrieval keeps leads separate from citable evidence."""

from __future__ import annotations

import datetime

from disco.retrieval import DefaultRetrievalEngine, LexicalReranker, RetrievalRequest
from disco.retrieval.models import ExtractedDoc, Passage, SearchHit


class Search:
    name = "test-search"

    def __init__(self, hits: list[SearchHit]) -> None:
        self.hits = hits
        self.queries: list[str] = []

    async def search(
        self, query, *, limit=10, domains_allow=None, domains_deny=None, time_filter=None
    ):
        del domains_allow, domains_deny, time_filter
        self.queries.append(query)
        return self.hits[:limit]


class Extraction:
    def __init__(self, docs: dict[str, ExtractedDoc] | None = None) -> None:
        self.docs = docs or {}
        self.calls: list[str] = []
        self.hit_calls: list[list[SearchHit]] = []

    async def extract_many(self, urls: list[str]) -> list[ExtractedDoc]:
        self.calls.extend(urls)
        return [self.docs[url] for url in urls]

    async def extract_hits(self, hits: list[SearchHit]) -> list[ExtractedDoc]:
        self.hit_calls.append(hits)
        return await self.extract_many([hit.url for hit in hits])


class Reranker:
    def __init__(self) -> None:
        self.calls: list[list[Passage]] = []

    async def rerank(self, query: str, passages: list[Passage], *, top_k: int) -> list[Passage]:
        del query
        self.calls.append(passages)
        return passages[:top_k]


def _passage(url: str, text: str) -> Passage:
    return Passage(
        id="corpus-1", source_url=url, source_title="Corpus", text=text, corpus_id="space"
    )


def _doc(url: str, text: str) -> ExtractedDoc:
    return ExtractedDoc(
        url=url,
        title="Fetched",
        content=text,
        passages=[Passage(id="web-1", source_url=url, source_title="Fetched", text=text)],
    )


async def test_discovery_only_keeps_hits_and_never_extracts_or_uses_snippets():
    url = "https://example.test/article"
    search = Search([SearchHit(url=url, title="Article", snippet="UNTRUSTED SNIPPET")])
    extraction = Extraction({url: _doc(url, "fetched evidence")})
    reranker = Reranker()
    engine = DefaultRetrievalEngine(search, extraction, reranker)

    result = await engine.retrieve(RetrievalRequest(query="article", discovery_only=True))

    assert result.all_hits == search.hits
    assert result.extracted == [] and result.passages == []
    assert extraction.calls == []
    assert reranker.calls == [[]]
    assert result.notes["discovery_only"] is True
    assert result.notes["retrieval_trace"]["extraction"]["attempted"] == 0


async def test_discovery_only_keeps_genuine_corpus_evidence():
    url = "https://example.test/article"
    search = Search([SearchHit(url=url, title="Article", snippet="lead")])
    extraction = Extraction({url: _doc(url, "web evidence")})
    corpus = _passage("space://space/corpus-1", "corpus evidence")

    class Embedder:
        async def embed(self, texts: list[str]) -> list[list[float]]:
            return [[1.0] for _ in texts]

    class VectorStore:
        async def query(self, namespace: str, vector: list[float], *, top_k: int) -> list[Passage]:
            assert namespace == "space" and vector == [1.0]
            return [corpus][:top_k]

    reranker = Reranker()
    result = await DefaultRetrievalEngine(
        search,
        extraction,
        reranker,
        embedder=Embedder(),
        vector_store=VectorStore(),
    ).retrieve(
        RetrievalRequest(query="article", discovery_only=True, corpus_ids=frozenset({"space"}))
    )

    assert result.passages == [corpus]
    assert result.extracted == [] and extraction.calls == []
    assert reranker.calls == [[corpus]]


async def test_ordinary_request_still_extracts_and_reranks_web_evidence():
    url = "https://example.test/article"
    hit = SearchHit(url=url, title="Article", snippet="UNTRUSTED SNIPPET")
    search = Search([hit])
    extraction = Extraction({url: _doc(url, "fetched evidence")})
    reranker = Reranker()

    result = await DefaultRetrievalEngine(search, extraction, reranker).retrieve(
        RetrievalRequest(query="article")
    )

    assert extraction.calls == [url]
    assert result.passages[0].text == "fetched evidence"
    assert "UNTRUSTED SNIPPET" not in result.passages[0].text
    assert reranker.calls == [[result.passages[0]]]
    assert "discovery_only" not in result.notes


async def test_matching_selected_direct_hit_preserves_discovery_provenance():
    requested = "https://example.test/article"
    selected = SearchHit(
        url=requested + "?utm_source=search#section",
        title="Primary MCP result",
        source_engine="mcp-primary",
        rank=7,
        published_at=datetime.date(2026, 9, 1),
    )
    search = Search([])
    extraction = Extraction({requested: _doc(requested, "primary evidence")})
    result = await DefaultRetrievalEngine(search, extraction, LexicalReranker()).retrieve(
        RetrievalRequest(query=requested, selected_hit=selected)
    )

    assert search.queries == []
    assert extraction.calls == [requested]
    assert len(extraction.hit_calls) == 1
    received = extraction.hit_calls[0][0]
    assert received.url == requested
    assert received.source_engine == "mcp-primary"
    assert received.rank == 7
    assert received.title == "Primary MCP result"
    assert received.published_at == datetime.date(2026, 9, 1)
    hit = result.all_hits[0]
    assert hit.url == requested
    assert hit.source_engine == "mcp-primary"
    assert hit.rank == 7 and hit.title == "Primary MCP result"
    assert hit.published_at == datetime.date(2026, 9, 1)


async def test_mismatched_selected_direct_hit_cannot_redirect_acquisition():
    requested = "https://example.test/article"
    selected = SearchHit(
        url="https://evil.example/redirect",
        title="Wrong result",
        source_engine="mcp-wrong",
    )
    search = Search([])
    extraction = Extraction({requested: _doc(requested, "requested evidence")})
    result = await DefaultRetrievalEngine(search, extraction, LexicalReranker()).retrieve(
        RetrievalRequest(query=requested, selected_hit=selected)
    )

    assert extraction.calls == [requested]
    assert result.all_hits[0].url == requested
    assert result.all_hits[0].source_engine == "direct_url"
    assert result.passages[0].source_url == requested


async def test_discovery_only_direct_read_still_honors_web_and_domain_policy():
    requested = "https://example.test/article"
    search = Search([])
    extraction = Extraction({requested: _doc(requested, "evidence")})
    engine = DefaultRetrievalEngine(search, extraction, LexicalReranker())

    denied = await engine.retrieve(
        RetrievalRequest(
            query=requested,
            discovery_only=True,
            domains_deny=frozenset({"example.test"}),
        )
    )
    web_off = await engine.retrieve(
        RetrievalRequest(query=requested, discovery_only=True, use_web=False)
    )

    assert denied.all_hits == [] and denied.passages == []
    assert web_off.all_hits == [] and web_off.passages == []
    assert extraction.calls == [] and search.queries == []
