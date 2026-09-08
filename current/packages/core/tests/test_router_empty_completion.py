"""An empty completion is a completed HTTP call — and never a clean success.

L25's root cause lived here: a review call whose whole output ceiling went into
the reasoning channel returned 200 OK with nothing in it, and the attempt trail
recorded ``outcome: success, error_class: null`` three times in a row. The class
was real, expensive, and uncountable.

Classification only. The router still does not retry an empty reply, and the
loop's own repair path is untouched — those walls belong to their owners.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from disco.core.events import LLMMessage
from disco.core.inspect import registry
from disco.core.llm import (
    CallContext,
    CapabilityProfile,
    CompletionRequest,
    DefaultLLMRouter,
    ModelEntry,
    ModelRole,
    RouterConfig,
)
from disco.core.llm.openai_provider import OpenAIProvider


def _sse(*chunks: dict[str, Any]) -> bytes:
    return ("".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n").encode()


def _router(content: bytes) -> tuple[DefaultLLMRouter, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=content, headers={"content-type": "text/event-stream"})

    provider = OpenAIProvider("http://fake/v1", name="fake", transport=httpx.MockTransport(handler))
    config = RouterConfig(
        models={"local": ModelEntry(model_id="m1", provider="fake", context_window=65_536)},
        default_model="local",
    )
    return DefaultLLMRouter(config, {"fake": provider}), seen


def _req() -> CompletionRequest:
    return CompletionRequest(
        profile=CapabilityProfile(role=ModelRole.RAG_ANSWERER),
        messages=[LLMMessage(role="user", content="review this")],
        max_tokens=64,
    )


def _rows(conversation_id: str) -> list[dict[str, Any]]:
    snapshot = registry().snapshot(conversation_id)
    assert snapshot is not None
    return snapshot["model_attempts"]


_CEILING_HIT = _sse(
    {"choices": [{"delta": {"reasoning_content": "all of the ceiling"}}]},
    {"choices": [{"delta": {}, "finish_reason": "length"}]},
)
_EMPTY_STOP = _sse({"choices": [{"delta": {}, "finish_reason": "stop"}]})
_NORMAL = _sse(
    {"choices": [{"delta": {"content": "an answer"}}]},
    {"choices": [{"delta": {}, "finish_reason": "stop"}]},
)


async def test_a_ceiling_hit_with_no_content_is_recorded_as_what_it_was(monkeypatch):
    monkeypatch.setenv("DISCO_INSPECT", "1")
    registry().clear()
    router, requests = _router(_CEILING_HIT)

    response = await router.complete(_req(), context=CallContext(conversation_id="ceiling"))

    assert response.text == "" and response.finish_reason == "length"
    rows = _rows("ceiling")
    assert [row["outcome"] for row in rows] == ["started", "empty"]
    assert rows[-1]["error_class"] == "ceiling_hit_empty"
    # Classification, not a new retry rung: exactly one request left the host.
    assert len(requests) == 1


async def test_a_model_that_stopped_with_nothing_keeps_its_own_class(monkeypatch):
    """The `stop` marker the loop's repair reads is unchanged, and distinct."""
    monkeypatch.setenv("DISCO_INSPECT", "1")
    registry().clear()
    router, _requests = _router(_EMPTY_STOP)

    await router.complete(_req(), context=CallContext(conversation_id="empty-stop"))

    rows = _rows("empty-stop")
    assert rows[-1]["outcome"] == "empty"
    assert rows[-1]["error_class"] == "empty_reasoning_only"


async def test_an_ordinary_completion_is_still_a_clean_success(monkeypatch):
    monkeypatch.setenv("DISCO_INSPECT", "1")
    registry().clear()
    router, _requests = _router(_NORMAL)

    await router.complete(_req(), context=CallContext(conversation_id="normal"))

    rows = _rows("normal")
    assert [row["outcome"] for row in rows] == ["started", "success"]
    assert rows[-1]["error_class"] is None


async def test_the_streamed_path_classifies_it_the_same_way(monkeypatch):
    """The class must be countable however the caller asked for the tokens."""
    monkeypatch.setenv("DISCO_INSPECT", "1")
    registry().clear()
    router, _requests = _router(_CEILING_HIT)

    async for _chunk in router.stream_complete(
        _req(), context=CallContext(conversation_id="ceiling-stream")
    ):
        pass

    rows = _rows("ceiling-stream")
    assert rows[-1]["outcome"] == "empty"
    assert rows[-1]["error_class"] == "ceiling_hit_empty"
