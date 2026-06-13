"""F1 wiring — recover_tool_calls must be invoked at the provider boundary
when the structured tool_calls channel is empty AND req.assist is True.

Two call sites in OpenAIProvider:
  * `_to_response`  (non-streaming .complete;  ~line 316)
  * `stream_complete` (the LIVE path Disco streams; ~line 519)

Behavior contract (T3):
  - structured empty + Hermes text + req.assist=True  -> recovered ProposedToolCall, finish_reason="tool_calls"
  - req.assist=False (capable-model default)           -> byte-identical to today (no recovery)
  - structured tool_calls present                       -> recover_tool_calls NOT called (structured wins)
"""

from __future__ import annotations

import json

import httpx
import pytest

from disco.core.events import LLMMessage
from disco.core.llm.openai_provider import OpenAIProvider
from disco.core.llm.types import (
    CapabilityProfile,
    CompletionRequest,
    ModelRole,
)


HERMES_CONTENT = '<tool_call>{"name": "foo", "arguments": {"x": 1}}</tool_call>'


def _req(text: str = "hi", *, assist: bool = False) -> CompletionRequest:
    return CompletionRequest(
        profile=CapabilityProfile(role=ModelRole.RAG_ANSWERER),
        messages=[LLMMessage(role="user", content=text)],
        max_tokens=50,
        temperature=0.0,
        assist=assist,
    )


def _provider(handler) -> OpenAIProvider:
    return OpenAIProvider("http://fake/v1", name="fake", transport=httpx.MockTransport(handler))


def _sse(*chunks: dict) -> bytes:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return body.encode()


# ---------------------------------------------------------------------------
# (a) empty-structured + Hermes text + assist ON  ->  recovered
# ---------------------------------------------------------------------------

async def test_non_streaming_recovers_from_hermes_text_when_assist_on():
    """_to_response path: structured is empty, model emitted a Hermes call in content,
    req.assist=True -> recover and surface a ProposedToolCall, finish_reason=tool_calls."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "m1",
                "choices": [
                    {
                        "message": {
                            "content": HERMES_CONTENT,
                            "reasoning_content": None,
                            "tool_calls": None,
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 5},
            },
        )

    r = await _provider(handler).complete(_req(assist=True), model="m1")

    assert len(r.tool_calls) == 1, f"expected 1 recovered call, got {r.tool_calls!r}"
    assert r.tool_calls[0].tool_name == "foo"
    assert r.tool_calls[0].arguments == {"x": 1}
    assert r.finish_reason == "tool_calls"


async def test_non_streaming_recovers_from_reasoning_content_when_assist_on():
    """_to_response path: the Hermes call is in reasoning_content (a reasoning model
    occasionally dumps the tool call there). Must be recovered with the same finish_reason."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "m1",
                "choices": [
                    {
                        "message": {
                            "content": "I'm going to call foo now.",
                            "reasoning_content": HERMES_CONTENT,
                            "tool_calls": None,
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 5},
            },
        )

    r = await _provider(handler).complete(_req(assist=True), model="m1")

    assert len(r.tool_calls) == 1, f"expected 1 recovered call, got {r.tool_calls!r}"
    assert r.tool_calls[0].tool_name == "foo"
    assert r.tool_calls[0].arguments == {"x": 1}
    assert r.finish_reason == "tool_calls"


async def test_streaming_recovers_from_hermes_text_when_assist_on():
    """stream_complete path (the LIVE path Disco streams): same contract."""

    content = _sse(
        {"model": "m", "choices": [{"delta": {"content": HERMES_CONTENT}}]},
        {
            "model": "m",
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 5},
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(
            200, content=content, headers={"content-type": "text/event-stream"}
        )

    final = None
    async for ch in _provider(handler).stream_complete(_req(assist=True), model="m"):
        if ch.done:
            final = ch.final
    assert final is not None
    assert len(final.tool_calls) == 1, f"expected 1 recovered call, got {final.tool_calls!r}"
    assert final.tool_calls[0].tool_name == "foo"
    assert final.tool_calls[0].arguments == {"x": 1}
    assert final.finish_reason == "tool_calls"


async def test_streaming_recovers_from_reasoning_content_when_assist_on():
    """G1 gap: stream_complete + the reasoning_buf accumulator — the Hermes call
    arrives via `reasoning_content` deltas (Qwen3 / DeepSeek-R1 thinking mode) with
    empty content. The streaming path must recover it just like the content path."""

    content = _sse(
        {"model": "m", "choices": [{"delta": {"reasoning_content": HERMES_CONTENT}}]},
        {
            "model": "m",
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 5},
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(
            200, content=content, headers={"content-type": "text/event-stream"}
        )

    final = None
    async for ch in _provider(handler).stream_complete(_req(assist=True), model="m"):
        if ch.done:
            final = ch.final
    assert final is not None
    assert len(final.tool_calls) == 1, (
        f"expected 1 recovered call from reasoning_content, got {final.tool_calls!r}"
    )
    assert final.tool_calls[0].tool_name == "foo"
    assert final.tool_calls[0].arguments == {"x": 1}
    assert final.finish_reason == "tool_calls"


# ---------------------------------------------------------------------------
# (b) req.assist=False  ->  no recovery, content passes through unchanged
# ---------------------------------------------------------------------------

async def test_non_streaming_no_recovery_when_assist_off():
    """Capable-model path must be byte-identical to today: text preserved, no calls,
    finish_reason from the wire."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "m1",
                "choices": [
                    {
                        "message": {
                            "content": HERMES_CONTENT,
                            "reasoning_content": None,
                            "tool_calls": None,
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 5},
            },
        )

    r = await _provider(handler).complete(_req(assist=False), model="m1")

    assert r.tool_calls == []
    assert r.text == HERMES_CONTENT
    assert r.finish_reason == "stop"


async def test_streaming_no_recovery_when_assist_off():
    """Streaming capable-model path must also be byte-identical to today."""

    content = _sse(
        {"model": "m", "choices": [{"delta": {"content": HERMES_CONTENT}}]},
        {
            "model": "m",
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 5},
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=content, headers={"content-type": "text/event-stream"}
        )

    final = None
    async for ch in _provider(handler).stream_complete(_req(assist=False), model="m"):
        if ch.done:
            final = ch.final
    assert final is not None
    assert final.tool_calls == []
    assert final.text == HERMES_CONTENT
    assert final.finish_reason == "stop"


# ---------------------------------------------------------------------------
# (c) structured tool_calls present  ->  recover_tool_calls NOT called
# ---------------------------------------------------------------------------

async def test_non_streaming_structured_present_skips_recovery(monkeypatch):
    """When structured tool_calls is non-empty on the wire, the recovery layer
    must NOT be invoked -- even though req.assist=True and content also happens
    to contain a Hermes marker. Structured wins."""

    sentinel_calls: list[tuple] = []

    def fake_recover(*args, **kwargs):
        sentinel_calls.append((args, kwargs))
        return []

    monkeypatch.setattr(
        "disco.core.llm.openai_provider.recover_tool_calls", fake_recover
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "m1",
                "choices": [
                    {
                        "message": {
                            "content": HERMES_CONTENT,  # would recover if we tried
                            "reasoning_content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "real_tool",
                                        "arguments": '{"y": 2}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 5},
            },
        )

    r = await _provider(handler).complete(_req(assist=True), model="m1")

    # Structured tool_calls surfaced unchanged
    assert len(r.tool_calls) == 1
    assert r.tool_calls[0].tool_name == "real_tool"
    assert r.tool_calls[0].arguments == {"y": 2}
    assert r.finish_reason == "tool_calls"
    # The recovery function was never consulted
    assert sentinel_calls == [], (
        f"recover_tool_calls must NOT fire when structured tool_calls is present; "
        f"saw {sentinel_calls!r}"
    )


async def test_streaming_structured_present_skips_recovery(monkeypatch):
    """Streaming path: structured tool_calls deltas -> recovery not invoked."""

    sentinel_calls: list[tuple] = []

    def fake_recover(*args, **kwargs):
        sentinel_calls.append((args, kwargs))
        return []

    monkeypatch.setattr(
        "disco.core.llm.openai_provider.recover_tool_calls", fake_recover
    )

    content = _sse(
        {
            "model": "m",
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "function": {
                                    "name": "real_tool",
                                    "arguments": '{"y": 2}',
                                },
                            }
                        ]
                    }
                }
            ],
        },
        {
            "model": "m",
            "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 5},
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=content, headers={"content-type": "text/event-stream"}
        )

    final = None
    async for ch in _provider(handler).stream_complete(_req(assist=True), model="m"):
        if ch.done:
            final = ch.final
    assert final is not None
    assert len(final.tool_calls) == 1
    assert final.tool_calls[0].tool_name == "real_tool"
    assert final.tool_calls[0].arguments == {"y": 2}
    assert final.finish_reason == "tool_calls"
    assert sentinel_calls == [], (
        f"recover_tool_calls must NOT fire on stream path when structured tool_calls is present; "
        f"saw {sentinel_calls!r}"
    )
