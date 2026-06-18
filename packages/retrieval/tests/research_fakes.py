"""Fakes for the retrieval & grounding tests (retrieval-grounding-contract.md §8).

Headless: providers, reranker, embedder, rewriter, router, and NLI are all
faked/stubbed — no real network, no provider keys, no real model.
"""

from __future__ import annotations

from disco.core.llm import CompletionResponse, TokenUsage
from disco.retrieval import ExtractedDoc, Passage, SearchHit


class FakeSearchProvider:
    """Returns configured hits (optionally honoring domain allow/deny). `name`
    lets a test prove provider-swap (engine independent of which provider)."""

    def __init__(self, hits: list[SearchHit], *, name: str = "fake-searxng") -> None:
        self.name = name
        self._hits = hits
        self.queries: list[str] = []

    async def search(self, query, *, limit=10, domains_allow=None, domains_deny=None, time_filter=None):
        self.queries.append(query)
        hits = self._hits
        if domains_allow is not None:
            hits = [h for h in hits if any(d in h.url for d in domains_allow)]
        if domains_deny is not None:
            hits = [h for h in hits if not any(d in h.url for d in domains_deny)]
        return hits[:limit]


class FakeExtractionProvider:
    """Maps URL → ExtractedDoc. URLs in `failing` yield an explicit-failure doc
    (§2.2). Each ok doc gets one Passage carrying provenance."""

    def __init__(self, docs: dict[str, str], *, failing: dict[str, str] | None = None) -> None:
        self.name = "fake-firecrawl"
        self._docs = docs
        self._failing = failing or {}

    async def extract(self, url: str) -> ExtractedDoc:
        if url in self._failing:
            status = self._failing[url]
            return ExtractedDoc(
                url=url, title=url, content="", fetched_ok=False, error=status, status=status
            )
        content = self._docs.get(url, "")
        pid = url.rsplit("/", 1)[-1] or "p"
        passages = (
            [Passage(id=f"{pid}_p0", source_url=url, source_title=url, text=content)]
            if content
            else []
        )
        return ExtractedDoc(url=url, title=url, content=content, passages=passages)

    async def extract_many(self, urls):
        return [await self.extract(u) for u in urls]


class FakeRewriter:
    """Returns configured paraphrases (truncated to n)."""

    def __init__(self, paraphrases: list[str]) -> None:
        self._paraphrases = paraphrases

    async def rewrite(self, query: str, *, n: int) -> list[str]:
        return ([query] + self._paraphrases)[:n] if n > 1 else [self._paraphrases[0]]


class FakeRouter:
    """An LLMRouter double: returns scripted text and counts complete() calls
    (to prove verification is NOT the LLM path)."""

    def __init__(self, *, answerer_text: str = "", rewriter_text: str = "") -> None:
        self._answerer = answerer_text
        self._rewriter = rewriter_text
        self.complete_calls = 0

    async def complete(self, req, *, context=None):
        self.complete_calls += 1
        from disco.core.llm import ModelRole

        text = self._answerer if req.profile.role == ModelRole.RAG_ANSWERER else self._rewriter
        return CompletionResponse(
            text=text,
            usage=TokenUsage(input_tokens=1, output_tokens=1),
            finish_reason="stop",
            model_used="fake",
            routing=None,
        )

    async def stream_complete(self, req, *, context=None):  # pragma: no cover - unused
        yield  # type: ignore[misc]


class FakeNLI:
    """Verdicts keyed by claim (hypothesis) text; default 'neutral'."""

    def __init__(self, verdicts: dict[str, str]) -> None:
        self._v = verdicts
        self.calls = 0

    def entail(self, premise: str, hypothesis: str) -> str:
        self.calls += 1
        return self._v.get(hypothesis, "neutral")

    def score(self, premise: str, hypothesis: str) -> float:
        return {"entail": 1.0, "neutral": 0.5, "contradict": 0.0}[
            self._v.get(hypothesis, "neutral")
        ]


def hit(url: str, title: str = "", snippet: str = "", rank: int = 0) -> SearchHit:
    return SearchHit(url=url, title=title or url, snippet=snippet, rank=rank)
