"""Regressions for stream terminal framing (hermetic, no network)."""

from __future__ import annotations

import json

import httpx
import pytest
from disco.core.events import LLMMessage
from disco.core.llm.errors import LLMTransientError
from disco.core.llm.openai_provider import OpenAIProvider
from disco.core.llm.types import CapabilityProfile, CompletionRequest, ModelRole


def _request() -> CompletionRequest:
    return CompletionRequest(
        profile=CapabilityProfile(role=ModelRole.RAG_ANSWERER),
        messages=[LLMMessage(role="user", content="terminal probe")],
        max_tokens=50,
        temperature=0.0,
    )


def _sse(*chunks: dict, done: bool = False) -> bytes:
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
    if done:
        body += "data: [DONE]\n\n"
    return body.encode()


def _chunk(content: str | None = None, finish: str | None = None) -> dict:
    delta = {}
    if content is not None:
        delta["content"] = content
    return {"model": "probe", "choices": [{"delta": delta, "finish_reason": finish}]}


def _usage() -> dict:
    return {"model": "probe", "choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2}}


def _provider(response_factory) -> OpenAIProvider:
    def handler(_request: httpx.Request) -> httpx.Response:
        return response_factory()

    return OpenAIProvider("http://fake/v1", name="fake", transport=httpx.MockTransport(handler))


class _ContentThenReadError(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield _sse(_chunk("partial"))
        raise httpx.ReadError("simulated read failure")


@pytest.mark.asyncio
async def test_content_then_clean_eof_is_typed_failure_and_stream_has_no_final_success():
    provider = _provider(
        lambda: httpx.Response(
            200, content=_sse(_chunk("partial")), headers={"content-type": "text/event-stream"}
        )
    )
    with pytest.raises(LLMTransientError):
        await provider.complete(_request(), model="probe")

    chunks = []
    with pytest.raises(LLMTransientError):
        async for item in provider.stream_complete(_request(), model="probe"):
            chunks.append(item)
    assert any(item.delta_text == "partial" for item in chunks)
    assert not any(item.done and item.final is not None for item in chunks)


@pytest.mark.parametrize("body", [b"", b"data: {not-json}\n\n"])
@pytest.mark.asyncio
async def test_empty_or_invalid_sse_then_eof_is_typed_failure(body: bytes):
    provider = _provider(
        lambda: httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})
    )
    with pytest.raises(LLMTransientError):
        await provider.complete(_request(), model="probe")


@pytest.mark.asyncio
async def test_explicit_stop_then_separate_usage_only_chunk_is_preserved():
    body = _sse(_chunk("complete"), _chunk(finish="stop"), _usage())
    result = await _provider(
        lambda: httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})
    ).complete(_request(), model="probe")
    assert result.text == "complete"
    assert result.finish_reason == "stop"
    assert result.usage.output_tokens == 2


@pytest.mark.parametrize("done", [False, True])
@pytest.mark.asyncio
async def test_explicit_length_then_separate_usage_only_chunk_is_preserved(done: bool):
    body = _sse(_chunk("truncated"), _chunk(finish="length"), _usage(), done=done)
    result = await _provider(
        lambda: httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})
    ).complete(_request(), model="probe")
    assert result.text == "truncated"
    assert result.finish_reason == "length"
    assert result.usage.output_tokens == 2


@pytest.mark.asyncio
async def test_content_done_without_finish_is_a_normal_stop():
    body = _sse(_chunk("complete"), done=True)
    result = await _provider(
        lambda: httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})
    ).complete(_request(), model="probe")
    assert result.text == "complete"
    assert result.finish_reason == "stop"


@pytest.mark.parametrize("finish", [None, "stop"])
@pytest.mark.asyncio
async def test_buffered_json_response_remains_supported_with_or_without_finish(finish: str | None):
    choice = {"message": {"content": "buffered"}}
    if finish is not None:
        choice["finish_reason"] = finish
    body = {
        "model": "probe",
        "choices": [choice],
        "usage": {"prompt_tokens": 4, "completion_tokens": 3},
    }
    result = await _provider(
        lambda: httpx.Response(200, json=body, headers={"content-type": "application/json"})
    ).complete(_request(), model="probe")
    assert result.text == "buffered"
    assert result.finish_reason == "stop"
    assert result.usage.output_tokens == 3


@pytest.mark.asyncio
async def test_content_then_transport_read_error_is_typed_failure():
    provider = _provider(
        lambda: httpx.Response(
            200, stream=_ContentThenReadError(), headers={"content-type": "text/event-stream"}
        )
    )
    with pytest.raises(LLMTransientError):
        await provider.complete(_request(), model="probe")
