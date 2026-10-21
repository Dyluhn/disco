"""Discovery transport failures remain failures all the way to the public stream."""

import asyncio

import httpx
import pytest
from disco.retrieval import LexicalReranker
from disco.retrieval.source_adapters import (
    ArxivSearchProvider,
    MultiSearchProvider,
    NewsSearchProvider,
    SemanticScholarSearchProvider,
    SiteScopedSearchProvider,
    _SearchSourceUnavailable,
)
from disco.retrieval.streaming import stream_research_answer
from research_fakes import FakeExtractionProvider, FakeNLI, FakeSearchProvider, hit
from test_streaming_keep_searching import AnswerSequenceRouter, TrackingRewriter

PROVIDERS = (ArxivSearchProvider, NewsSearchProvider, SemanticScholarSearchProvider)
SECRET = "never-echo-credential"
URL = "https://example.test/report"
BODY = "The measured output is twelve units."


async def collect(search):
    router = AnswerSequenceRouter([f"{BODY} [[report_p0]]"])
    rewrite = TrackingRewriter("broader question")
    frames = [
        frame
        async for frame in stream_research_answer(
            "What was measured?",
            search=search,
            extraction=FakeExtractionProvider({URL: BODY}),
            reranker=LexicalReranker(),
            router=router,
            nli=FakeNLI({"The measured output is twelve units": "entail"}),
            rewriter=rewrite,
            max_research_rounds=3,
        )
    ]
    return frames, router, rewrite


@pytest.mark.parametrize("factory", PROVIDERS)
@pytest.mark.parametrize("status", [401, 403, 429, 503])
async def test_http_failure_reaches_stream_without_inference_or_secret_echo(factory, status):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, text=SECRET)

    provider = factory(
        base_url=f"https://upstream.example/{SECRET}",
        transport=httpx.MockTransport(handler),
    )
    frames, router, rewrite = await collect(provider)
    errors = [frame for frame in frames if frame["type"] == "error"]
    assert len(errors) == 1 and f"HTTP {status}" in errors[0]["message"]
    assert provider.name in errors[0]["message"]
    assert "No search results" not in str(frames) and SECRET not in str(frames)
    assert not [frame for frame in frames if frame["type"] in {"token", "final"}]
    assert not router.stream_calls and not rewrite.calls and len(requests) == 1


@pytest.mark.parametrize("factory", PROVIDERS)
@pytest.mark.parametrize("failure", [httpx.ReadTimeout, httpx.ConnectError, OSError])
async def test_transport_failure_is_sanitized_and_not_empty(factory, failure):
    def handler(request):
        raise failure(SECRET)

    frames, router, rewrite = await collect(factory(transport=httpx.MockTransport(handler)))
    assert frames[-1]["type"] == "error"
    assert "could not be reached" in frames[-1]["message"]
    assert SECRET not in str(frames) and not router.stream_calls and not rewrite.calls


@pytest.mark.parametrize("factory", PROVIDERS)
async def test_transport_cancellation_is_not_wrapped_or_swallowed(factory):
    def handler(request):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await factory(transport=httpx.MockTransport(handler)).search("q")


def failing_source():
    return SemanticScholarSearchProvider(
        transport=httpx.MockTransport(lambda req: httpx.Response(429, text=SECRET)),
    )


@pytest.mark.parametrize("healthy_empty", [False, True])
async def test_composite_failure_is_not_reported_as_empty_even_with_empty_healthy_leg(
    healthy_empty,
):
    providers = (failing_source(),)
    if healthy_empty:
        providers += (FakeSearchProvider([]),)
    frames, router, rewrite = await collect(MultiSearchProvider(providers))
    assert frames[-1]["type"] == "error"
    assert "selected sources failed" in frames[-1]["message"]
    assert "No search results" not in str(frames) and SECRET not in str(frames)
    assert not router.stream_calls and not rewrite.calls


async def test_composite_keeps_grounded_result_from_healthy_leg_when_another_fails():
    frames, router, rewrite = await collect(
        MultiSearchProvider(
            (
                failing_source(),
                FakeSearchProvider([hit(URL)]),
            )
        )
    )
    assert not [frame for frame in frames if frame["type"] == "error"]
    final = next(frame["answer"] for frame in frames if frame["type"] == "final")
    assert final["passages"][0]["source_url"] == URL
    assert BODY in final["passages"][0]["text"]
    assert len(router.stream_calls) == 1 and not rewrite.calls


async def test_successful_empty_still_reports_no_results():
    provider = SemanticScholarSearchProvider(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"data": []})),
    )
    frames, router, rewrite = await collect(provider)
    assert frames[-1] == {
        "type": "error",
        "message": "No search results were found for this query.",
    }
    assert not router.stream_calls and not rewrite.calls


async def test_site_scope_does_not_hide_inner_failure():
    with pytest.raises(_SearchSourceUnavailable, match="HTTP 429"):
        await SiteScopedSearchProvider(sites="example.test", inner=failing_source()).search("q")
