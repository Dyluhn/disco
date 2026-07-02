from __future__ import annotations

import httpx
import pytest
from disco.core.events import LLMMessage
from disco.core.llm import (
    CallContext,
    CapabilityProfile,
    CompletionRequest,
    DefaultLLMRouter,
    ModelRole,
)
from disco.core.llm.openai_provider import OpenAIProvider
from llm_fakes import simple_config

pytestmark = pytest.mark.asyncio


def _ok_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "local-driver-q4",
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        },
    )


def _req() -> CompletionRequest:
    return CompletionRequest(
        profile=CapabilityProfile(role=ModelRole.AGENT_DRIVER),
        messages=[LLMMessage(role="user", content="build")],
    )


async def test_router_threads_conversation_id_to_provider_header() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _ok_response()

    provider = OpenAIProvider(
        "http://fake/v1", name="ollama", transport=httpx.MockTransport(handler)
    )
    router = DefaultLLMRouter(simple_config(), {"ollama": provider, "openrouter": provider})

    await router.complete(_req(), context=CallContext(conversation_id="conv_router_header"))

    assert seen
    assert seen[0].headers["x-disco-conversation"] == "conv_router_header"


async def test_router_omits_conversation_header_without_context_id() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _ok_response()

    provider = OpenAIProvider(
        "http://fake/v1", name="ollama", transport=httpx.MockTransport(handler)
    )
    router = DefaultLLMRouter(simple_config(), {"ollama": provider, "openrouter": provider})

    await router.complete(_req())

    assert seen
    assert "x-disco-conversation" not in seen[0].headers
