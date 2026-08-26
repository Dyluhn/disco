"""Deterministic contract tests for the paid-provider HTTP boundary."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from disco.retrieval.bundled_providers import (
    BraveSearchProvider,
    FirecrawlExtractionProvider,
    TavilySearchProvider,
)
from disco.retrieval.provider_http import (
    BoundedHttpExecutor,
    HttpAttemptPolicy,
    HttpResult,
)


def _run(coro):
    return asyncio.run(coro)


def test_retry_after_is_bounded_and_recorded():
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "999"})
        return httpx.Response(200, json={"ok": True})

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    result = _run(
        BoundedHttpExecutor(
            "test",
            transport=httpx.MockTransport(handler),
            policy=HttpAttemptPolicy(
                deadline_s=5.0, max_attempts=3, max_retry_after_s=1.25, backoff_s=0.1
            ),
            sleep=sleep,
            jitter=lambda value: value,
        ).request("GET", "https://provider.test")
    )
    assert isinstance(result, HttpResult)
    assert result.response is not None and result.response.status_code == 200
    assert result.diagnostic["outcome"] == "ok"
    assert result.diagnostic["attempts"] == 2
    assert sleeps == [1.25]
    assert result.diagnostic["retry_wait_ms"] == 1250


def test_connection_transport_error_retries_once():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("connection unavailable", request=request)
        return httpx.Response(200, json={"ok": True})

    result = _run(
        BoundedHttpExecutor(
            "test",
            transport=httpx.MockTransport(handler),
            policy=HttpAttemptPolicy(deadline_s=5, backoff_s=0),
        ).request("GET", "https://provider.test")
    )
    assert calls == 2
    assert result.diagnostic["outcome"] == "ok"
    assert result.diagnostic["attempts"] == 2


def test_503_retries_then_succeeds():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503 if calls == 1 else 200, json={"ok": True})

    result = _run(
        BoundedHttpExecutor(
            "test",
            transport=httpx.MockTransport(handler),
            policy=HttpAttemptPolicy(deadline_s=5, backoff_s=0),
        ).request("GET", "https://provider.test")
    )
    assert calls == 2
    assert result.diagnostic["attempts"] == 2
    assert result.diagnostic["status_code"] == 200
    assert result.diagnostic["outcome"] == "ok"


def test_redirect_is_not_a_successful_provider_operation():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"Location": "https://provider.test/next"},
            json={"ok": True},
        )

    result = _run(
        BoundedHttpExecutor(
            "test", transport=httpx.MockTransport(handler), policy=HttpAttemptPolicy(deadline_s=5)
        ).request("GET", "https://provider.test")
    )
    assert result.diagnostic["status_code"] == 302
    assert result.diagnostic["attempts"] == 1
    assert result.diagnostic["outcome"] == "upstream"


def test_timeout_retries_then_succeeds():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("read timed out", request=request)
        return httpx.Response(200, json={"ok": True})

    result = _run(
        BoundedHttpExecutor(
            "test",
            transport=httpx.MockTransport(handler),
            policy=HttpAttemptPolicy(deadline_s=5, backoff_s=0),
        ).request("GET", "https://provider.test")
    )
    assert calls == 2
    assert result.diagnostic["attempts"] == 2
    assert result.diagnostic["outcome"] == "ok"


def test_exhausted_retry_is_upstream_failure():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    result = _run(
        BoundedHttpExecutor(
            "test",
            transport=httpx.MockTransport(handler),
            policy=HttpAttemptPolicy(deadline_s=5, backoff_s=0),
        ).request("GET", "https://provider.test")
    )
    assert calls == 3
    assert result.diagnostic["attempts"] == 3
    assert result.diagnostic["status_code"] == 503
    assert result.diagnostic["outcome"] == "upstream"


@pytest.mark.parametrize("status", [401, 402, 403, 422, 432, 433])
def test_non_retryable_provider_status_is_a_failure(status: int):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status)

    result = _run(
        BoundedHttpExecutor(
            "test", transport=httpx.MockTransport(handler), policy=HttpAttemptPolicy(deadline_s=5)
        ).request("GET", "https://provider.test")
    )
    assert result.response is not None and result.response.status_code == status
    assert calls == 1
    assert result.diagnostic["attempts"] == 1
    expected = "auth" if status in {401, 403} else "quota"
    assert result.diagnostic["outcome"] == expected


async def test_semaphore_wait_consumes_operation_deadline():
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        started.set()
        await release.wait()
        return httpx.Response(200, json={"ok": True})

    executor = BoundedHttpExecutor(
        "test",
        transport=httpx.MockTransport(handler),
        policy=HttpAttemptPolicy(deadline_s=0.03, backoff_s=0),
        concurrency=1,
    )
    first = asyncio.create_task(executor.request("GET", "https://provider.test/first"))
    await started.wait()
    second = await executor.request("GET", "https://provider.test/second")
    release.set()
    await first
    assert second.diagnostic["outcome"] == "timeout"
    assert second.diagnostic["attempts"] == 0


def test_cancellation_during_backoff_is_not_retried_or_swallowed():
    started = asyncio.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async def sleep(delay: float) -> None:
        started.set()
        await asyncio.Event().wait()

    async def run() -> None:
        task = asyncio.create_task(
            BoundedHttpExecutor(
                "test",
                transport=httpx.MockTransport(handler),
                policy=HttpAttemptPolicy(deadline_s=5, backoff_s=0.01),
                sleep=sleep,
            ).request("GET", "https://provider.test")
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    _run(run())


async def test_tavily_brave_and_firecrawl_current_wire_contracts():
    seen: list[tuple[str, str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content or b"{}")
        seen.append((request.method, str(request.url), payload))
        if "tavily" in str(request.url):
            assert request.headers["authorization"] == "Bearer tavily-secret"
            assert "api_key" not in payload
            return httpx.Response(200, json={"results": [{"url": "https://a.test", "title": "A"}]})
        if "brave" in str(request.url):
            assert request.headers["x-subscription-token"] == "brave-secret"
            assert request.url.params["freshness"] == "pw"
            return httpx.Response(
                200,
                json={"web": {"results": [{"url": "https://b.test", "title": "B"}]}},
            )
        assert request.url.path == "/v2/scrape"
        assert request.headers["authorization"] == "Bearer fire-secret"
        assert payload == {"url": "https://example.com/c", "formats": ["markdown"]}
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "markdown": "A readable passage that is long enough to cite." * 2,
                    "metadata": {"title": "C"},
                },
            },
        )

    transport = httpx.MockTransport(handler)
    tavily = TavilySearchProvider("tavily-secret", transport=transport)
    brave = BraveSearchProvider("brave-secret", transport=transport)
    firecrawl = FirecrawlExtractionProvider("fire-secret", transport=transport)
    assert len(await tavily.search("q", time_filter="week")) == 1
    assert len(await brave.search("q", time_filter="week")) == 1
    doc = await firecrawl.extract("https://example.com/c")
    assert doc.fetched_ok and doc.title == "C" and doc.passages
    assert len(seen) == 3


async def test_valid_empty_is_distinct_from_provider_failure():
    def empty(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    def failed(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    empty_provider = TavilySearchProvider("key", transport=httpx.MockTransport(empty))
    failed_provider = TavilySearchProvider("key", transport=httpx.MockTransport(failed))
    empty_hits, empty_diag = await empty_provider.search_detailed("q")
    failed_hits, failed_diag = await failed_provider.search_detailed("q")
    assert empty_hits == [] and empty_diag["outcome"] == "empty"
    assert failed_hits == [] and failed_diag["outcome"] == "upstream"

    def brave_empty(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"web": {"results": []}})

    brave_hits, brave_diag = await BraveSearchProvider(
        "key", transport=httpx.MockTransport(brave_empty)
    ).search_detailed("q")
    assert brave_hits == [] and brave_diag["outcome"] == "empty"


async def test_adapter_malformed_json_and_schema_are_invalid_response():
    def malformed(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json")

    def wrong_shape(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"web": {"results": [{"url": 4}]}})

    tavily_hits, tavily_diag = await TavilySearchProvider(
        "key", transport=httpx.MockTransport(malformed)
    ).search_detailed("q")
    brave_hits, brave_diag = await BraveSearchProvider(
        "key", transport=httpx.MockTransport(wrong_shape)
    ).search_detailed("q")
    assert tavily_hits == [] and tavily_diag["outcome"] == "invalid_response"
    assert brave_hits == [] and brave_diag["outcome"] == "invalid_response"

    def firecrawl_wrong_shape(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": {"markdown": 4}})

    firecrawl_doc, firecrawl_diag = await FirecrawlExtractionProvider(
        "key", transport=httpx.MockTransport(firecrawl_wrong_shape)
    ).extract_detailed("https://example.com/bad-shape")
    assert not firecrawl_doc.fetched_ok
    assert firecrawl_diag["outcome"] == "invalid_response"


async def test_top_level_json_list_and_null_are_invalid_response():
    def tavily_list(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    def brave_null(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=None)

    def firecrawl_list(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    tavily_hits, tavily_diag = await TavilySearchProvider(
        "key", transport=httpx.MockTransport(tavily_list)
    ).search_detailed("q")
    brave_hits, brave_diag = await BraveSearchProvider(
        "key", transport=httpx.MockTransport(brave_null)
    ).search_detailed("q")
    firecrawl_doc, firecrawl_diag = await FirecrawlExtractionProvider(
        "key", transport=httpx.MockTransport(firecrawl_list)
    ).extract_detailed("https://example.com/list")

    assert tavily_hits == [] and tavily_diag["outcome"] == "invalid_response"
    assert brave_hits == [] and brave_diag["outcome"] == "invalid_response"
    assert not firecrawl_doc.fetched_ok
    assert firecrawl_diag["outcome"] == "invalid_response"


async def test_tavily_rejects_non_official_base_url():
    with pytest.raises(ValueError, match="official API origin"):
        TavilySearchProvider("key", base_url="https://proxy.example.test")


async def test_firecrawl_valid_empty_is_distinct_from_exhausted_failure():
    def empty(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": {"markdown": ""}})

    def failed(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    empty_doc, empty_diag = await FirecrawlExtractionProvider(
        "key", transport=httpx.MockTransport(empty)
    ).extract_detailed("https://example.com/empty")
    failed_doc, failed_diag = await FirecrawlExtractionProvider(
        "key", transport=httpx.MockTransport(failed)
    ).extract_detailed("https://example.com/fail")
    assert not empty_doc.fetched_ok and empty_diag["outcome"] == "empty"
    assert not failed_doc.fetched_ok and failed_diag["outcome"] == "upstream"


async def test_firecrawl_empty_then_rate_limited_batch_is_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        url = json.loads(request.content)["url"]
        if url.endswith("/empty"):
            return httpx.Response(200, json={"success": True, "data": {"markdown": ""}})
        return httpx.Response(429, headers={"Retry-After": "0"})

    provider = FirecrawlExtractionProvider(
        "key", transport=httpx.MockTransport(handler), base_url="https://api.firecrawl.order"
    )
    docs, diagnostic = await provider.extract_many_detailed(
        ["https://example.com/empty", "https://example.com/rate"]
    )
    assert not any(doc.fetched_ok for doc in docs)
    assert diagnostic["outcome"] == "rate_limited"
    assert diagnostic["status_code"] == 429


async def test_firecrawl_shared_ceiling_is_two_and_mixed_batch_isolated():
    active = 0
    maximum = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.001)
        active -= 1
        url = json.loads(request.content)["url"]
        if url.endswith("/bad"):
            return httpx.Response(200, json={"success": True, "data": {"markdown": 3}})
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "markdown": "A readable passage with enough words for citation. " * 3,
                    "metadata": {"title": url.rsplit("/", 1)[-1]},
                },
            },
        )

    provider = FirecrawlExtractionProvider(
        "key", transport=httpx.MockTransport(handler), base_url="https://api.firecrawl.test"
    )
    urls = [f"https://example.com/{i}" for i in range(8)] + ["https://example.com/bad"]
    docs, diagnostic = await provider.extract_many_detailed(urls)
    assert maximum <= 2
    assert diagnostic["max_concurrency"] <= 2
    assert sum(doc.fetched_ok for doc in docs) == 8
    assert not docs[-1].fetched_ok
    assert docs[-1].status == "error"
    assert diagnostic["outcome"] == "partial_outage"


async def test_firecrawl_ceiling_is_shared_across_instances():
    active = 0
    maximum = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.001)
        active -= 1
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "markdown": "A readable passage with enough words for citation. " * 3,
                    "metadata": {"title": "shared"},
                },
            },
        )

    first_transport = httpx.MockTransport(handler)
    second_transport = httpx.MockTransport(handler)
    first = FirecrawlExtractionProvider(
        "key", transport=first_transport, base_url="https://api.firecrawl.shared"
    )
    second = FirecrawlExtractionProvider(
        "key", transport=second_transport, base_url="https://api.firecrawl.shared"
    )
    await asyncio.gather(
        first.extract_many([f"https://example.com/a{i}" for i in range(4)]),
        second.extract_many([f"https://example.com/b{i}" for i in range(4)]),
    )
    assert maximum <= 2
