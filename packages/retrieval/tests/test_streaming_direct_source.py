"""The public Search stream must read a complete URL without replacing its source."""

import pytest
from disco.retrieval import LexicalReranker
from disco.retrieval.bundled_providers import LocalExtractionProvider
from disco.retrieval.streaming import stream_research_answer
from research_fakes import FakeExtractionProvider, FakeNLI, FakeSearchProvider, hit
from test_streaming_keep_searching import AnswerSequenceRouter, TrackingRewriter

PRIMARY = "https://original.example/report"
MIRROR = "https://mirror.example/copy"
BODY = "The measured output is twelve units."


class RecordingExtraction(FakeExtractionProvider):
    def __init__(self, *, failing=None):
        super().__init__({PRIMARY: BODY, MIRROR: "Unrelated mirror."}, failing=failing)
        self.urls = []

    async def extract_many(self, urls):
        self.urls.extend(urls)
        return await super().extract_many(urls)


async def collect(query=PRIMARY, *, extraction=None, denied=frozenset(), supported=True):
    search = FakeSearchProvider([hit(MIRROR)])
    extraction = extraction or RecordingExtraction()
    rewriter = TrackingRewriter("a broader query")
    router = AnswerSequenceRouter(["The measured output is twelve units. [[report_p0]]"])
    frames = [frame async for frame in stream_research_answer(
        query,
        search=search,
        extraction=extraction,
        reranker=LexicalReranker(),
        router=router,
        nli=FakeNLI({
            "The measured output is twelve units": "entail" if supported else "contradict",
        }),
        rewriter=rewriter,
        max_research_rounds=3,
        domains_deny=denied,
    )]
    return frames, search, extraction, rewriter, router


async def test_full_stream_reads_original_and_preserves_cited_evidence_without_search():
    frames, search, extraction, rewriter, router = await collect()
    assert search.queries == [] and rewriter.calls == []
    assert extraction.urls == [PRIMARY]
    assert not [frame for frame in frames if frame["type"] == "error"]
    final = next(frame["answer"] for frame in frames if frame["type"] == "final")
    assert final["query"] == PRIMARY
    assert [(p["source_url"], p["text"]) for p in final["passages"]] == [(PRIMARY, BODY)]
    assert [(h["url"], h["source_engine"]) for h in final["all_hits"]] == [(PRIMARY, "direct_url")]
    assert BODY in router.stream_requests[0].messages[1].content
    assert MIRROR not in str(final)
    assert frames[-1] == {"type": "state", "status": "finished"}


async def test_unreadable_original_is_not_replaced_by_search_mirror():
    frames, search, extraction, rewriter, router = await collect(
        extraction=RecordingExtraction(failing={PRIMARY: "paywalled"})
    )
    assert search.queries == [] and rewriter.calls == []
    assert extraction.urls == [PRIMARY] and router.stream_calls == []
    assert frames[-1]["type"] == "error" and "couldn't read" in frames[-1]["message"]
    assert not [frame for frame in frames if frame["type"] == "final"]


@pytest.mark.parametrize("denied", [frozenset({"original.example"}), frozenset({"example"})])
async def test_direct_url_obeys_denied_host_without_extracting_or_searching(denied):
    frames, search, extraction, rewriter, router = await collect(denied=denied)
    assert search.queries == [] and rewriter.calls == []
    assert extraction.urls == [] and router.stream_calls == []
    assert frames[-1]["type"] == "error"


async def test_unsupported_direct_answer_never_rewrites_into_open_search():
    frames, search, extraction, rewriter, router = await collect(supported=False)
    assert search.queries == [] and rewriter.calls == []
    assert extraction.urls == [PRIMARY] and len(router.stream_calls) == 1
    assert frames[-1]["type"] == "error" and "did not support" in frames[-1]["message"]
    assert not [frame for frame in frames if frame["type"] in {"token", "final", "phase"}]


async def test_url_embedded_in_prose_remains_an_ordinary_search():
    query = "independent replication of " + PRIMARY
    _, search, extraction, _, _ = await collect(query)
    assert search.queries == [query]
    assert extraction.urls == [MIRROR]


async def test_direct_stream_still_uses_real_egress_guard():
    frames, search, _, _, router = await collect(
        "http://127.0.0.1/private", extraction=LocalExtractionProvider()
    )
    assert search.queries == [] and router.stream_calls == []
    assert frames[-1]["type"] == "error" and "couldn't read" in frames[-1]["message"]
