"""OpenAI-compatible ModelProvider adapter — hermetic tests (httpx.MockTransport).

No network: a mock transport returns canned OpenAI responses so we test the
adapter's parsing, error classification, reasoning-model handling, and SSE
reassembly. (The live smoke against the real Qwen endpoint is run separately.)
"""

from __future__ import annotations

import json

import httpx
import pytest
from perpleximanus.core.events import LLMMessage
from perpleximanus.core.llm.errors import (
    LLMAuthError,
    LLMContextWindowExceeded,
    LLMError,
    is_context_window_exceeded,
)
from perpleximanus.core.llm.openai_provider import OpenAIProvider
from perpleximanus.core.llm.types import CapabilityProfile, CompletionRequest, ModelRole


def _req(text: str = "hi") -> CompletionRequest:
    return CompletionRequest(
        profile=CapabilityProfile(role=ModelRole.RAG_ANSWERER),
        messages=[LLMMessage(role="user", content=text)],
        max_tokens=50,
        temperature=0.0,
    )


def _provider(handler) -> OpenAIProvider:
    return OpenAIProvider("http://fake/v1", name="fake", transport=httpx.MockTransport(handler))


def _sse(*chunks: dict) -> bytes:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return body.encode()


async def test_non_streaming_parses_content_and_usage():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "m1" and body["stream"] is False
        return httpx.Response(
            200,
            json={
                "model": "m1",
                "choices": [
                    {
                        # reasoning model: content is the ANSWER, reasoning_content the thinking
                        "message": {"content": "Paris", "reasoning_content": "let me think..."},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1},
            },
        )

    r = await _provider(handler).complete(_req(), model="m1")
    assert r.text == "Paris"  # reasoning_content is NOT treated as the answer
    assert r.finish_reason == "stop"
    assert r.usage.input_tokens == 5 and r.usage.output_tokens == 1


async def test_reasoning_only_truncation_yields_empty_content():
    # A reasoning model truncated mid-thought (small max_tokens) → empty content.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": None, "reasoning_content": "still..."},
                        "finish_reason": "length",
                    }
                ]
            },
        )

    r = await _provider(handler).complete(_req(), model="m")
    assert r.text == "" and r.finish_reason == "length"


async def test_context_window_error_is_classified_and_preserves_message():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "code": 400,
                    "message": "request exceeds the available context size (131072 tokens)",
                    "type": "exceed_context_size_error",
                }
            },
        )

    with pytest.raises(LLMContextWindowExceeded) as exc:
        await _provider(handler).complete(_req(), model="m")
    assert is_context_window_exceeded(exc.value)  # the condenser's dependency
    assert "exceeds the available context size" in str(exc.value)  # real message preserved


async def test_auth_error_preserves_real_message():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, json={"error": {"message": "Invalid API Key", "type": "authentication_error"}}
        )

    with pytest.raises(LLMAuthError) as exc:
        await _provider(handler).complete(_req(), model="m")
    assert str(exc.value) == "Invalid API Key"


async def test_generic_provider_error_is_not_flattened():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"message": "something specific broke", "type": "invalid_request"}},
        )

    with pytest.raises(LLMError) as exc:
        await _provider(handler).complete(_req(), model="m")
    assert "something specific broke" in str(exc.value)  # not a generic failure


async def test_streaming_reassembles_and_final_matches():
    content = _sse(
        {"model": "m", "choices": [{"delta": {"content": "Hello"}}]},
        {"model": "m", "choices": [{"delta": {"content": ", world"}}]},
        {
            "model": "m",
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(200, content=content, headers={"content-type": "text/event-stream"})

    deltas: list[str] = []
    final = None
    async for ch in _provider(handler).stream_complete(_req(), model="m"):
        if ch.done:
            final = ch.final
        elif ch.delta_text:
            deltas.append(ch.delta_text)
    assert "".join(deltas) == "Hello, world"
    assert final is not None
    assert final.text == "Hello, world"
    assert final.finish_reason == "stop"
    assert final.usage.output_tokens == 2


async def test_streaming_error_status_raises_typed_before_any_token():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, json={"error": {"message": "Invalid API Key", "type": "authentication_error"}}
        )

    with pytest.raises(LLMAuthError):
        async for _chunk in _provider(handler).stream_complete(_req(), model="m"):
            pass
