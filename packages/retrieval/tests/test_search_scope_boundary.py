"""Explicit site operators are host constraints independent of discovery backend."""

import pytest
from disco.retrieval import DefaultRetrievalEngine, LexicalReranker, RetrievalRequest
from disco.retrieval._mcp_search_providers import ExaMcpSearchProvider, ParallelMcpSearchProvider
from disco.retrieval.streaming import stream_research_answer
from research_fakes import FakeExtractionProvider, FakeNLI, hit
from test_mcp_search_providers import _mcp_transport, _ok
from test_streaming_keep_searching import AnswerSequenceRouter, TrackingRewriter

PRIMARY = "https://docs.example.test/report"
OUTSIDE = "https://mirror.test/copy"
LOOKALIKE = "https://docs.example.test.evil.test/clone"
DENIED = "https://blocked.docs.example.test/blocked"
QUERY = "site:docs.example.test -site:blocked.docs.example.test measured output"
BODY = "The measured output is twelve units."


class IgnoringSearch:
    name = "generic"

    def __init__(self):
        self.requests = []

    async def search(self, query, **kwargs):
        self.requests.append((query, kwargs))
        return [hit(url) for url in (OUTSIDE, LOOKALIKE, DENIED, PRIMARY)]


class RecordingExtraction(FakeExtractionProvider):
    def __init__(self):
        super().__init__({url: BODY for url in (OUTSIDE, LOOKALIKE, DENIED, PRIMARY)})
        self.urls = []

    async def extract_many(self, urls):
        self.urls.extend(urls)
        return await super().extract_many(urls)


def provider(kind):
    if kind == "generic":
        return IgnoringSearch()
    urls = [OUTSIDE, LOOKALIKE, DENIED, PRIMARY]

    def response(_request, _body):
        if kind == "exa":
            text = "\n---\n".join(f"Title: Report\nURL: {url}\nHighlights:\n{BODY}" for url in urls)
            return _ok({"content": [{"type": "text", "text": text}]})
        return _ok({"structuredContent": {
            "results": [{"url": url, "title": "Report", "excerpts": [BODY]} for url in urls],
        }})

    cls = ExaMcpSearchProvider if kind == "exa" else ParallelMcpSearchProvider
    return cls(transport=_mcp_transport(on_call=response))


@pytest.mark.parametrize("kind", ["generic", "exa", "parallel"])
async def test_engine_enforces_query_host_scope_before_extraction_cap(kind):
    extraction = RecordingExtraction()
    engine = DefaultRetrievalEngine(provider(kind), extraction, LexicalReranker())
    result = await engine.retrieve(RetrievalRequest(
        query=QUERY, extract_cap=1, domains_allow=frozenset({"example.test"}),
    ))
    assert extraction.urls == [PRIMARY]
    assert [p.source_url for p in result.passages] == [PRIMARY]
    assert [h.url for h in result.all_hits] == [PRIMARY]
    assert result.issued_queries == [QUERY]


@pytest.mark.parametrize("kind", ["generic", "exa", "parallel"])
async def test_public_stream_never_extracts_or_cites_out_of_scope_sources(kind):
    extraction = RecordingExtraction()
    frames = [frame async for frame in stream_research_answer(
        QUERY, search=provider(kind), extraction=extraction, reranker=LexicalReranker(),
        router=AnswerSequenceRouter(["The measured output is twelve units. [[report_p0]]"]),
        nli=FakeNLI({"The measured output is twelve units": "entail"}), extract_cap=1,
    )]
    assert extraction.urls == [PRIMARY]
    final = next(frame["answer"] for frame in frames if frame["type"] == "final")
    assert [p["source_url"] for p in final["passages"]] == [PRIMARY]
    assert [h["url"] for h in final["all_hits"]] == [PRIMARY]


async def test_original_site_scope_survives_reformulation_without_rewriting_query_text():
    search = IgnoringSearch()
    extraction = RecordingExtraction()
    rewriter = TrackingRewriter("measured output revised query")
    frames = [frame async for frame in stream_research_answer(
        QUERY, search=search, extraction=extraction, reranker=LexicalReranker(),
        router=AnswerSequenceRouter([
            "Unsubstantiated answer. [[report_p0]]",
            "The measured output is twelve units. [[report_p0]]",
        ]),
        nli=FakeNLI({
            "Unsubstantiated answer": "contradict", "The measured output is twelve units": "entail",
        }),
        rewriter=rewriter, max_research_rounds=2,
    )]
    assert rewriter.calls == [QUERY]
    assert [q for q, _ in search.requests] == [QUERY, "measured output revised query"]
    assert extraction.urls == [PRIMARY, PRIMARY]
    final = next(frame["answer"] for frame in frames if frame["type"] == "final")
    assert [h["url"] for h in final["all_hits"]] == [PRIMARY]
    assert "Unsubstantiated" not in "".join(f.get("token", "") for f in frames)


@pytest.mark.parametrize("query,allow", [
    (QUERY, frozenset({"elsewhere.test"})),
    (QUERY, frozenset()),
    ("site:example.test measured output", frozenset({"specific.docs.example.test"})),
])
async def test_query_scope_cannot_widen_configured_domain_allowlist(query, allow):
    extraction = RecordingExtraction()
    engine = DefaultRetrievalEngine(IgnoringSearch(), extraction, LexicalReranker())
    result = await engine.retrieve(RetrievalRequest(query=query, domains_allow=allow))
    assert extraction.urls == [] and result.all_hits == [] and result.passages == []


async def test_quoted_site_operator_remains_search_text():
    search = IgnoringSearch()
    extraction = RecordingExtraction()
    engine = DefaultRetrievalEngine(search, extraction, LexicalReranker())
    query = 'Explain "site:docs.example.test" syntax'
    result = await engine.retrieve(RetrievalRequest(query=query))
    assert len(result.all_hits) == 4 and len(extraction.urls) == 4
    assert search.requests[0][0] == query


@pytest.mark.parametrize("query,allow,expected", [
    (
        "site:example.test -site:blocked.docs.example.test output",
        frozenset({"docs.example.test"}), [PRIMARY],
    ),
    (
        "(site:docs.example.test OR site:mirror.test) -site:blocked.docs.example.test output",
        None, [OUTSIDE, PRIMARY],
    ),
])
async def test_positive_scope_union_and_narrower_configured_subdomain_are_retained(
    query, allow, expected,
):
    search = IgnoringSearch()
    extraction = RecordingExtraction()
    engine = DefaultRetrievalEngine(search, extraction, LexicalReranker())
    result = await engine.retrieve(RetrievalRequest(query=query, domains_allow=allow))
    assert extraction.urls == expected
    assert [h.url for h in result.all_hits] == expected
    assert search.requests[0][0] == query
