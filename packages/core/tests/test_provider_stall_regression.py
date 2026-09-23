"""Regressions for the September fresh-install research failures."""

import asyncio
import datetime
import json
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest
from disco.core.llm import DefaultLLMRouter, LLMError, LLMTransientError, ModelEntry, RouterConfig
from disco.core.llm._openai_timeouts import iter_with_progress_timeout
from disco.core.llm._provider_retry import attach_retry_after, meaningful_sse_line
from disco.core.llm._router_backoff import sleep_before_retry
from disco.core.llm.openai_provider import OpenAIProvider
from disco.core.llm.stream_progress import observe_stream_progress
from test_stream_progress import _req


@pytest.mark.parametrize(
    "model,effort", [("deepseek-v4.1-flash", "none"), ("glm-5.3-flash", "low")]
)
async def test_captured_ollama_responses_through_actual_provider(model, effort):
    captured = Path(__file__).with_name("fixtures") / "ollama-20260922" / f"{model}.sse"
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200, content=captured.read_bytes(), headers={"content-type": "text/event-stream"}
        )

    provider = OpenAIProvider("https://ollama.com/v1", transport=httpx.MockTransport(handler))
    request = _req().model_copy(update={"enable_thinking": False})
    response = await provider.complete(request, model=model)
    assert json.loads(response.text) == {"ok": True}
    assert requests[0]["reasoning_effort"] == effort
    assert "thinking" not in requests[0]


@pytest.mark.parametrize(
    "line",
    [
        "",
        ": ping",
        'data: {"choices":[{"delta":{}}]}',
        'data: {"choices":[{"delta":{"role":"assistant"}}]}',
    ],
)
async def test_keepalive_traffic_cannot_extend_first_output_deadline(line):
    closed = False

    async def source():
        nonlocal closed
        try:
            while True:
                await asyncio.sleep(0.005)
                yield line
        finally:
            closed = True

    with pytest.raises(LLMTransientError, match="no first chunk"):
        async for _ in iter_with_progress_timeout(
            source(),
            provider_name="test",
            first_chunk_s=0.04,
            idle_s=0.02,
            is_progress=meaningful_sse_line,
        ):
            pass
    assert closed


async def test_reasoning_and_tool_arguments_keep_a_slow_stream_alive():
    frames = [
        'data: {"choices":[{"delta":{"reasoning_content":"x"}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"function":{"arguments":"{"}}]}}]}',
    ]

    async def source():
        for frame in frames * 8:
            await asyncio.sleep(0.005)
            yield frame

    delivered = [
        line
        async for line in iter_with_progress_timeout(
            source(),
            provider_name="test",
            first_chunk_s=0.03,
            idle_s=0.03,
            is_progress=meaningful_sse_line,
        )
    ]
    assert delivered == frames * 8


@pytest.mark.parametrize(
    "raw,expected", [("7", 7), ("99999", 300), ("-1", 0), ("NaN", None), ("garbage", None)]
)
def test_retry_after_is_finite_and_bounded(raw, expected):
    error = LLMTransientError()
    attach_retry_after(error, {"retry-after": raw})
    assert error.retry_after_s == expected


def test_retry_after_http_date():
    error = LLMTransientError()
    date = datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=30)
    attach_retry_after(error, {"retry-after": format_datetime(date, usegmt=True)})
    assert error.retry_after_s is not None and 28 <= error.retry_after_s <= 30


@pytest.mark.parametrize("stream", [False, True])
async def test_http_retry_after_reaches_typed_error(stream):
    provider = OpenAIProvider(
        "https://example.test/v1",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                429, headers={"Retry-After": "17"}, json={"error": {"type": "rate_limit_error"}}
            )
        ),
    )
    with pytest.raises(LLMTransientError) as raised:
        if stream:
            async for _ in provider.stream_complete(_req(), model="test"):
                pass
        else:
            await provider.complete(_req(), model="test")
    assert raised.value.retry_after_s == 17


@pytest.mark.parametrize("field", ["code", "type"])
async def test_structured_hard_quota_is_terminal_but_unknown_429_is_retryable(field):
    provider = OpenAIProvider(
        "https://example.test/v1",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(429, json={"error": {field: "insufficient_quota"}})
        ),
    )
    with pytest.raises(LLMError) as raised:
        await provider.complete(_req(), model="test")
    assert not isinstance(raised.value, LLMTransientError)


async def test_retry_wait_is_visible_without_inspect_and_cancellable(monkeypatch):
    monkeypatch.setenv("DISCO_INSPECT", "0")
    seen = []
    observed = asyncio.Event()

    async def observe(progress):
        seen.append(progress)
        observed.set()

    with observe_stream_progress(observe):
        task = asyncio.create_task(
            sleep_before_retry(
                2,
                0,
                error=LLMTransientError(retry_after_s=30, http_status=429),
                metadata={"inspect_stage": "source_reading"},
            )
        )
        await asyncio.wait_for(observed.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert seen[0].state == "backoff"
    assert seen[0].details == {"attempt": 2, "retry_after_s": 30, "http_status": 429}
    assert seen[0].tokens_streamed == 0


@pytest.mark.parametrize("stream", [False, True])
async def test_router_honors_header_and_emits_backoff_on_both_entry_points(monkeypatch, stream):
    from disco.core.llm import _router_backoff

    delays, progress = [], []
    attempts = 0

    async def sleep(delay):
        delays.append(delay)

    async def observe(frame):
        progress.append(frame)

    def handler(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                429, headers={"Retry-After": "17"}, json={"error": {"type": "rate_limit_error"}}
            )
        return httpx.Response(
            200,
            content=(
                'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n'
                "data: [DONE]\n\n"
            ),
            headers={"content-type": "text/event-stream"},
        )

    monkeypatch.setattr(_router_backoff.asyncio, "sleep", sleep)
    provider = OpenAIProvider(
        "https://example.test/v1", name="fake", transport=httpx.MockTransport(handler)
    )
    router = DefaultLLMRouter(
        RouterConfig(
            models={
                "test": ModelEntry(model_id="test", provider="fake", context_window=65_536),
            },
            default_model="test",
        ),
        {"fake": provider},
    )
    with observe_stream_progress(observe):
        if stream:
            chunks = [chunk async for chunk in router.stream_complete(_req())]
            assert chunks[-1].final.text == "ok"
        else:
            assert (await router.complete(_req())).text == "ok"
    assert attempts == 2 and delays == [17]
    backoff = [frame for frame in progress if frame.state == "backoff"]
    assert len(backoff) == 1 and backoff[0].details["http_status"] == 429
