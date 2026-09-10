"""Source-oriented research must not lose a document to duplicate ranked chunks."""

import pytest
from disco.retrieval.engine import DefaultRetrievalEngine
from disco.retrieval.models import ExtractedDoc, Passage, RetrievalRequest, SearchHit
from disco.retrieval.ranking import distinct_source_passages


class Search:
    async def search(self, query, **kwargs):
        return [SearchHit(url=f"https://example.test/{name}", title=name) for name in ("a", "b")]


class Extraction:
    async def extract_many(self, urls):
        return [
            ExtractedDoc(
                url=url,
                title=url,
                content="Original supporting evidence with conditions and qualifications.",
                passages=[
                    Passage(
                        id=f"{name}{index}",
                        source_url=url,
                        source_title=name,
                        text=f"Evidence {name} {index}",
                    )
                    for index in range(3 if name == "a" else 1)
                ],
                fetched_ok=True,
                status="ok",
            )
            for url in urls
            for name in [url.rsplit("/", 1)[-1]]
        ]


class Ranked:
    async def rerank(self, query, passages, *, top_k):
        return sorted(passages, key=lambda passage: passage.id)[:top_k]


@pytest.mark.parametrize("source_oriented", [False, True])
async def test_distinct_source_request_preserves_the_other_fetched_document(source_oriented):
    engine = DefaultRetrievalEngine(Search(), Extraction(), Ranked())
    request = RetrievalRequest.model_validate(
        {"query": "original evidence", "top_k": 2, "distinct_sources": source_oriented}
    )
    result = await engine.retrieve(request)
    assert [passage.id for passage in result.passages] == (
        ["a0", "b0"] if source_oriented else ["a0", "a1"]
    )
    assert len(result.extracted) == 2


def test_distinct_web_sources_preserve_corpus_spans_and_best_web_passage():
    first = Passage(
        id="best", source_url="https://example.test/document", source_title="Document", text="Best"
    )
    other = first.model_copy(
        update={"id": "other", "source_url": "https://example.test/document#part"}
    )
    corpus = [first.model_copy(update={"id": f"span-{i}", "corpus_id": "upload"}) for i in range(2)]
    assert [p.id for p in distinct_source_passages([first, other, *corpus], top_k=3)] == [
        "best",
        "span-0",
        "span-1",
    ]
