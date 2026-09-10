"""Extraction must preserve qualifications outside the ranking excerpt budget."""

import httpx
from disco.retrieval._crawl4ai import Crawl4aiExtractionProvider
from disco.retrieval._retrieval_cache import RetrievalCache
from disco.retrieval.deep_research._agent_state import _retrieved_source_passages
from disco.retrieval.deep_research._source_inspection import Inspection, source_inspection_rows
from disco.retrieval.models import ExtractedDoc, RetrievalResult


async def test_full_crawler_body_survives_old_cache_ranking_and_inspection(tmp_path):
    url = "https://example.com/specification"
    qualification = "Critical qualification: a committed value is visible before checkpointing."
    body = ("Background explanation with enough content for ranking. " * 20 + "\n\n") * 30
    body += qualification
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": url,
                        "success": True,
                        "status_code": 200,
                        "markdown": {"fit_markdown": body},
                        "metadata": {"title": "Specification"},
                    }
                ]
            },
        )

    cache = RetrievalCache(tmp_path / "cache.sqlite")
    old = ExtractedDoc(url=url, title="Specification", content=body[:16_000])
    cache.put_page(url, old.model_dump(mode="json"))
    transport = httpx.MockTransport(handler)
    # A deliberately capped caller must not poison the full-retention cache either.
    await Crawl4aiExtractionProvider(
        "http://crawler", transport=transport, cache=cache, max_chars=16_000
    ).extract(url)
    provider = Crawl4aiExtractionProvider("http://crawler", transport=transport, cache=cache)
    doc = await provider.extract(url)
    assert doc.content == body, doc.error
    assert len(doc.passages) <= 12
    assert qualification not in "".join(p.text for p in doc.passages)
    assert await provider.extract(url) == doc
    assert len(calls) == 2
    result = RetrievalResult(
        passages=doc.passages[:1], extracted=[doc], all_hits=[], issued_queries=["specification"]
    )
    admitted = _retrieved_source_passages(result)[0]
    row = source_inspection_rows(
        {admitted.id: admitted}, (Inspection(admitted.id, focus="committed value checkpointing"),)
    )[0]
    assert row["ok"] and qualification in row["text"]
    assert row["start"] > 16_000
    assert row["text"] == body[row["start"] : row["end"]]
    assert len(row["text"]) <= 2200
