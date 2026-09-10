from __future__ import annotations

import json

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


def _req(request_id: str | None = None) -> CompletionRequest:
    return CompletionRequest(
        profile=CapabilityProfile(role=ModelRole.AGENT_DRIVER),
        messages=[LLMMessage(role="user", content="build")],
        request_id=request_id,
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


async def test_opencode_headers_use_stable_conversation_identity_across_requests() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _ok_response()

    provider = OpenAIProvider(
        "https://opencode.ai/zen/go/v1",
        name="provider-alias",
        transport=httpx.MockTransport(handler),
    )
    router = DefaultLLMRouter(simple_config(), {"ollama": provider, "openrouter": provider})
    await router.complete(_req("req_first"), context=CallContext(conversation_id="conv_stable"))
    await router.complete(_req("req_second"), context=CallContext(conversation_id="conv_stable"))

    assert len(seen) == 2
    for wire, request_id in zip(seen, ["req_first", "req_second"], strict=True):
        assert wire.headers["x-opencode-session"] == "conv_stable"
        assert wire.headers["x-opencode-request"] == request_id
        assert wire.headers["x-opencode-client"] == "disco"
        assert wire.headers["x-disco-conversation"] == "conv_stable"


async def test_opencode_headers_fall_back_to_request_id_without_context() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _ok_response()

    provider = OpenAIProvider(
        "https://opencode.ai/zen/go/v1",
        api_key="secret",
        transport=httpx.MockTransport(handler),
    )
    router = DefaultLLMRouter(simple_config(), {"ollama": provider, "openrouter": provider})

    await router.complete(_req("req_standalone"))

    assert seen
    assert seen[0].headers["x-opencode-session"] == "req_standalone"
    assert seen[0].headers["x-opencode-request"] == "req_standalone"
    assert seen[0].headers["x-opencode-client"] == "disco"
    assert "x-disco-conversation" not in seen[0].headers
    assert seen[0].headers["authorization"] == "Bearer secret"


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.opencode.ai/v1",
        "https://opencode.ai.evil.test/v1",
        "https://other.test/opencode.ai/v1",
    ],
)
async def test_opencode_headers_require_exact_host_and_request_context(base_url: str) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _ok_response()

    provider = OpenAIProvider(
        base_url,
        name="opencode",
        transport=httpx.MockTransport(handler),
    )
    router = DefaultLLMRouter(simple_config(), {"ollama": provider, "openrouter": provider})

    await router.complete(_req("req_other_host"), context=CallContext(conversation_id="conv"))

    assert seen
    assert "x-opencode-session" not in seen[0].headers
    assert "x-opencode-request" not in seen[0].headers
    assert "x-opencode-client" not in seen[0].headers
    assert seen[0].headers["x-disco-conversation"] == "conv"


async def test_req_none_keeps_existing_headers_without_fabricated_session() -> None:
    provider = OpenAIProvider("https://opencode.ai/zen/go/v1", api_key="secret")

    assert provider._headers() == {
        "content-type": "application/json",
        "Authorization": "Bearer secret",
    }


@pytest.mark.parametrize(
    ("conversation_id", "request_id"),
    [(None, None), ("conv_preflight", None), ("conv_driver", "req_driver")],
)
async def test_opencode_session_survives_transient_router_retry(
    conversation_id: str | None, request_id: str | None
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(503, json={"error": {"type": "server_error"}})
        return _ok_response()

    provider = OpenAIProvider(
        "https://opencode.ai/zen/go/v1", transport=httpx.MockTransport(handler)
    )
    router = DefaultLLMRouter(simple_config(), {"ollama": provider, "openrouter": provider})
    request = _req(request_id)
    context = CallContext(conversation_id=conversation_id)
    result = await router.complete(request, context=context)

    assert result.text == "ok"
    assert len(seen) == 2
    session = seen[0].headers["x-opencode-session"]
    assert session
    assert seen[1].headers["x-opencode-session"] == session
    if conversation_id:
        assert session == conversation_id
    for wire in seen:
        assert wire.headers["x-opencode-client"] == "disco"
        assert wire.headers.get("x-opencode-request") == request_id


async def test_opencode_unscoped_session_is_stable_and_instance_local() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _ok_response()

    providers = [
        OpenAIProvider("https://opencode.ai/zen/go/v1", transport=httpx.MockTransport(handler))
        for _ in range(2)
    ]
    for provider in [providers[0], providers[0], providers[1]]:
        await provider.complete(_req(), model="m")

    sessions = [wire.headers["x-opencode-session"] for wire in seen]
    assert sessions[0] == sessions[1]
    assert sessions[0] != sessions[2]
    assert all("x-opencode-request" not in wire.headers for wire in seen)


async def test_opencode_stream_rejection_keeps_same_unscoped_session() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if json.loads(request.content)["stream"]:
            return httpx.Response(
                400,
                json={"error": {"type": "invalid_request_error", "message": "stream unsupported"}},
            )
        return _ok_response()

    provider = OpenAIProvider(
        "https://opencode.ai/zen/go/v1", transport=httpx.MockTransport(handler)
    )
    result = await provider.complete(_req(), model="m")

    assert result.text == "ok"
    assert [json.loads(wire.content)["stream"] for wire in seen] == [True, False]
    assert seen[0].headers["x-opencode-session"]
    assert seen[0].headers["x-opencode-session"] == seen[1].headers["x-opencode-session"]
    assert all("x-opencode-request" not in wire.headers for wire in seen)
