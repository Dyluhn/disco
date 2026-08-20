"""Deterministic search-query compression (``_query_compression``).

The planner emits sub-questions as full interrogative sentences; search
engines — especially the keyless ddgs/Brave-scrape fallbacks — rank those
terribly.  The compressor must turn them into keyword queries a search box
would love, without an LLM call, and without ever touching what synthesis
and judging see (that wiring is asserted in ``test_pipeline.py``).
"""

from __future__ import annotations

import pytest
from disco.retrieval import DefaultRetrievalEngine, LexicalReranker, RetrievalRequest
from disco.retrieval._query_compression import compress_search_query
from disco.retrieval.models import Passage
from research_fakes import FakeExtractionProvider, FakeSearchProvider, hit

# The real production query that returned zero on-topic sources when sent
# verbatim to ddgs / the Brave HTML scrape (2026-08-19 run logs).
_REAL_SUBQUESTION = (
    "Which open-source LLM models (including base, instruct, and notable "
    "fine-tunes/merges) were released or significantly updated between "
    "2026-08-12 and 2026-08-19, and what are their exact model names, "
    "parameter sizes, and release dates?"
)
_REAL_COMPRESSED = (
    "open-source LLM models released updated 2026-08-12..2026-08-19 "
    "model names parameter sizes release dates"
)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        # The real production failure case: 40-word interrogative → 12 terms,
        # interrogative scaffolding + parenthetical gone, date range collapsed.
        (_REAL_SUBQUESTION, _REAL_COMPRESSED),
        # Interrogative scaffolding and trailing question clause stripped.
        (
            "What are the documented side effects of semaglutide, and how common are they?",
            "documented side effects semaglutide common",
        ),
        # Quoted phrases survive verbatim as single terms.
        (
            'Who coined the phrase "attention is all you need" and in what paper?',
            'coined phrase "attention is all you need" paper',
        ),
        # "from X to Y" date ranges collapse like "between X and Y".
        (
            "How did US inflation change from 2021 to 2023 according to CPI data?",
            "US inflation change 2021..2023 according CPI data",
        ),
        # Already-terse keyword queries pass through unchanged.
        ("rust borrow checker lifetimes", "rust borrow checker lifetimes"),
        # Too short to compress safely → original returned (fallback).
        ("q", "q"),
        ("what is love", "what is love"),
        # Whitespace is normalized even on the fallback path.
        ("  what   is\tlove ", "what is love"),
        # Term cap: at most 12 terms survive, in original order.
        (
            "alpha bravo charlie delta echo foxtrot golf hotel india juliett "
            "kilo lima mike november oscar",
            "alpha bravo charlie delta echo foxtrot golf hotel india juliett kilo lima",
        ),
        # Case-insensitive dedupe keeps the first occurrence.
        (
            "Python packaging tools and python packaging standards",
            "Python packaging tools standards",
        ),
        ("", ""),
    ],
)
def test_compression_table(query: str, expected: str) -> None:
    assert compress_search_query(query) == expected


def test_compression_is_deterministic() -> None:
    assert compress_search_query(_REAL_SUBQUESTION) == compress_search_query(_REAL_SUBQUESTION)


async def test_search_provider_sees_compressed_query_reranker_sees_original() -> None:
    """The seam: `_transform` compresses what discovery sends to EVERY
    SearchProvider, while `req.query` (what reranking and everything
    downstream consumes) stays the full sub-question."""

    class _RecordingReranker(LexicalReranker):
        def __init__(self) -> None:
            self.queries: list[str] = []

        async def rerank(self, query: str, passages: list[Passage], *, top_k: int) -> list[Passage]:
            self.queries.append(query)
            return await super().rerank(query, passages, top_k=top_k)

    search = FakeSearchProvider([hit("http://x/a")])
    reranker = _RecordingReranker()
    engine = DefaultRetrievalEngine(
        search, FakeExtractionProvider({"http://x/a": "model release notes"}), reranker
    )

    result = await engine.retrieve(RetrievalRequest(query=_REAL_SUBQUESTION, depth="shallow"))

    assert search.queries == [_REAL_COMPRESSED]
    assert reranker.queries == [_REAL_SUBQUESTION]
    assert result.issued_queries == [_REAL_COMPRESSED]  # audit shows what was searched
