"""Provider-neutral transport headers: explicit RequestPolicy, never name-selected."""

from __future__ import annotations

import json

import httpx
import pytest
from disco.core import LLMMessage
from disco.core.llm import CapabilityProfile, CompletionRequest, ModelEntry, ModelRole
from disco.core.llm.errors import LLMError
from disco.core.llm.openai_provider import OpenAIProvider
from disco.core.llm.request_policy import RequestPolicy
from pydantic import ValidationError


def request(**kwargs):
    base = {
        "profile": CapabilityProfile(role=ModelRole.RAG_ANSWERER),
        "messages": [LLMMessage(role="user", content="Summarize this.")],
    }
    base.update(kwargs)
    return CompletionRequest(**base)


def wire_capture():
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        )

    return seen, handler


def test_defaults_leave_wire_headers_unchanged():
    req = request(metadata={"conversation_id": "conv-1"}, request_id="req-1")
    before = OpenAIProvider("https://neutral.example/v1")._headers(req)
    assert RequestPolicy().headers == {}
    assert RequestPolicy().session_header is None
    assert "X-Custom-Session" not in before
    assert "User-Agent" not in before
    assert before["X-Disco-Session"] == "conv-1"
    assert before["X-Disco-Conversation"] == "conv-1"
    assert before["X-Disco-Request"] == "req-1"


@pytest.mark.parametrize(
    "url",
    [
        "https://api.example-one.com/v1",
        "https://gateway.example-two.net/v1",
        "http://localhost:8080/v1",
        "https://unknown.example/v1",
    ],
)
@pytest.mark.parametrize("model", ["alpha-1", "beta-2", "custom-model-x"])
def test_arbitrary_endpoints_and_models_share_one_transport_path(url, model):
    policy = RequestPolicy(
        headers={"User-Agent": "disco-test/1.0"},
        session_header="X-Custom-Session",
    )
    req = request(metadata={"conversation_id": "conv-9"}, request_id="req-9")
    actual = OpenAIProvider(url, name=model, request_policy=policy)._headers(req, model=model)
    expected = OpenAIProvider("https://neutral.example/v1", request_policy=policy)._headers(
        req, model="other-model"
    )
    assert actual["User-Agent"] == "disco-test/1.0"
    assert actual["X-Custom-Session"] == "conv-9"
    assert actual == expected


@pytest.mark.parametrize(
    "model",
    ["glm-5.3-flash", "deepseek-v4.1-flash", "qwen3.8-flash", "future-model"],
)
def test_familiar_names_gain_no_implicit_transport_headers(model):
    req = request(metadata={"conversation_id": "conv-1"}, request_id="req-1")
    actual = OpenAIProvider("https://unknown.example/v1", name=model)._headers(req, model=model)
    expected = OpenAIProvider("https://neutral.example/v1")._headers(req, model="plain")
    assert actual == expected
    assert "User-Agent" not in actual


def test_models_sharing_one_provider_keep_separate_transport_policies():
    provider = OpenAIProvider(
        "https://neutral.example/v1",
        model_policies={
            "model-a": RequestPolicy(
                headers={"User-Agent": "agent-a/1.0"},
                session_header="X-Session-A",
            ),
            "model-b": RequestPolicy(
                headers={"User-Agent": "agent-b/1.0"},
                session_header="X-Session-B",
            ),
        },
    )
    req = request(metadata={"conversation_id": "conv-1"}, request_id="req-1")
    ha = provider._headers(req, model="model-a")
    hb = provider._headers(req, model="model-b")
    hc = provider._headers(req, model="model-c")
    assert ha["User-Agent"] == "agent-a/1.0"
    assert ha["X-Session-A"] == "conv-1"
    assert "X-Session-B" not in ha
    assert hb["User-Agent"] == "agent-b/1.0"
    assert hb["X-Session-B"] == "conv-1"
    assert "User-Agent" not in hc
    assert "X-Session-A" not in hc
    assert hc["X-Disco-Session"] == "conv-1"


def test_session_alias_stable_within_one_conversation_distinct_across():
    provider = OpenAIProvider(
        "https://neutral.example/v1",
        request_policy=RequestPolicy(session_header="X-Custom-Session"),
    )
    first = provider._headers(
        request(metadata={"conversation_id": "conv-same"}, request_id="req-1"),
        model="m",
    )
    second = provider._headers(
        request(metadata={"conversation_id": "conv-same"}, request_id="req-2"),
        model="m",
    )
    other = provider._headers(
        request(metadata={"conversation_id": "conv-other"}, request_id="req-1"),
        model="m",
    )
    assert first["X-Custom-Session"] == "conv-same"
    assert second["X-Custom-Session"] == "conv-same"
    assert other["X-Custom-Session"] == "conv-other"
    assert first["X-Custom-Session"] == first["X-Disco-Session"]


def test_session_alias_falls_back_without_conversation_metadata():
    provider = OpenAIProvider(
        "https://neutral.example/v1",
        request_policy=RequestPolicy(session_header="X-Custom-Session"),
    )
    by_request = provider._headers(request(request_id="req-fallback"), model="m")
    assert by_request["X-Custom-Session"] == "req-fallback"
    assert by_request["X-Custom-Session"] == by_request["X-Disco-Session"]
    unscoped = provider._headers(request(), model="m")
    assert unscoped["X-Custom-Session"] == provider._session_id
    assert unscoped["X-Custom-Session"] == unscoped["X-Disco-Session"]
    # Stable for unscoped retries on the same provider instance.
    again = provider._headers(request(), model="m")
    assert again["X-Custom-Session"] == unscoped["X-Custom-Session"]


@pytest.mark.parametrize("mode", ["sse", "json-on-stream", "responses", "stream-rejection"])
@pytest.mark.parametrize("delivery", ["complete", "stream"])
async def test_public_transports_carry_the_same_explicit_headers(mode, delivery):
    policy = RequestPolicy(
        headers={"User-Agent": "disco-wire/2.0"},
        session_header="X-Custom-Session",
    )
    seen = []

    def handler(req):
        body = json.loads(req.content)
        seen.append((req, body))
        if mode == "stream-rejection" and body.get("stream"):
            return httpx.Response(400, json={"error": {"message": "stream unsupported"}})
        if mode == "responses":
            assert req.url.path == "/v1/responses"
            assert "input" in body and "messages" not in body
            return httpx.Response(200, json={
                "status": "completed",
                "output": [{"type": "message", "content": [
                    {"type": "output_text", "text": "ok"}
                ]}],
            })
        assert req.url.path == "/v1/chat/completions"
        if mode == "sse":
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                content=b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":null}]}\n\n'
                b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
                b'data: [DONE]\n\n')
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]
        })

    provider = OpenAIProvider(
        "https://unknown.example/v1" + ("/responses" if mode == "responses" else ""),
        api_key="test-credential",
        request_policy=RequestPolicy(headers={"X-Default": "unused"}),
        model_policies={"arbitrary-model": policy},
        transport=httpx.MockTransport(handler),
    )
    req = request(metadata={"conversation_id": "conv-wire"}, request_id="req-wire")
    if delivery == "stream" and mode == "stream-rejection":
        # The public streaming API preserves its existing no-buffered-fallback contract.
        with pytest.raises(LLMError):
            _ = [c async for c in provider.stream_complete(req, model="arbitrary-model")]
    elif delivery == "stream":
        chunks = [c async for c in provider.stream_complete(req, model="arbitrary-model")]
        assert chunks[-1].final.text == "ok"
    else:
        assert (await provider.complete(req, model="arbitrary-model")).text == "ok"
    buffered_fallback = mode == "stream-rejection" and delivery == "complete"
    assert len(seen) == (2 if buffered_fallback else 1)
    if buffered_fallback:
        assert [body["stream"] for _, body in seen] == [True, False]
    for captured, body in seen:
        assert captured.headers["User-Agent"] == "disco-wire/2.0"
        assert captured.headers["X-Custom-Session"] == "conv-wire"
        assert captured.headers["X-Disco-Session"] == "conv-wire"
        assert captured.headers["X-Disco-Conversation"] == "conv-wire"
        assert captured.headers["Authorization"] == "Bearer test-credential"
        assert "x-default" not in captured.headers
        assert "headers" not in body and "session_header" not in body


async def test_auth_and_existing_identity_survive_explicit_transport_headers():
    seen, handler = wire_capture()
    provider = OpenAIProvider(
        "https://unknown.example/v1",
        api_key="secret-key",
        request_policy=RequestPolicy(
            headers={"User-Agent": "disco-wire/2.0"},
            session_header="X-Custom-Session",
        ),
        transport=httpx.MockTransport(handler),
    )
    req = request(metadata={"conversation_id": "conv-auth"}, request_id="req-auth")
    assert (await provider.complete(req, model="arbitrary-model")).text == "ok"
    headers = seen[0].headers
    assert headers["Authorization"] == "Bearer secret-key"
    assert headers["X-Disco-Session"] == "conv-auth"
    assert headers["X-Disco-Request"] == "req-auth"
    assert headers["X-Custom-Session"] == "conv-auth"


def test_static_policy_is_not_mutated_by_header_assembly():
    policy = RequestPolicy(
        headers={"User-Agent": "disco-test/1.0"},
        session_header="X-Custom-Session",
    )
    snapshot = policy.model_dump()
    provider = OpenAIProvider("https://neutral.example/v1", request_policy=policy)
    req = request(metadata={"conversation_id": "conv-1"}, request_id="req-1")
    assembled = provider._headers(req, model="m")
    assembled["User-Agent"] = "mutated"
    provider._payload(req, "m", stream=True)
    assert policy.model_dump() == snapshot


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": "Bearer smuggled"},
        {"Content-Type": "text/plain"},
        {"Host": "evil.example"},
        {"Content-Length": "5"},
        {"Connection": "close"},
        {"Transfer-Encoding": "chunked"},
        {"Cookie": "session=hijack"},
        {"X-Disco-Session": "spoofed"},
        {"X-Disco-Request": "spoofed"},
        {"X-Disco-Conversation": "spoofed"},
        {"X-DISCO-SESSION": "spoofed"},
        {"User-Agent": "ok", "user-agent": "duplicate"},
        {"X-Bad\nName": "ok"},
        {"X-Custom": "bad\nvalue"},
        {"X-Custom": "bad\rvalue"},
        {"X-Custom": "bad\x00value"},
        {"X-Trailing\n": "ok"},
        {"X-Custom": "caf\u00e9"},
        {"X-Custom": "\U0001f642"},
    ],
)
def test_static_header_injection_duplicates_and_collisions_rejected(headers):
    with pytest.raises(ValidationError):
        RequestPolicy(headers=headers)


@pytest.mark.parametrize(
    "alias",
    [
        "Authorization",
        "Content-Type",
        "Host",
        "Content-Length",
        "Connection",
        "Transfer-Encoding",
        "Cookie",
        "X-Disco-Session",
        "X-Disco-Request",
        "X-Disco-Conversation",
        "X-DISCO-REQUEST",
        "Not A Token",
        "bad\nheader",
        "X-Trailing\n",
        "",
    ],
)
def test_session_header_alias_rejected_for_reserved_or_invalid_names(alias):
    with pytest.raises(ValidationError):
        RequestPolicy(session_header=alias)


def test_session_header_colliding_with_static_headers_rejected():
    with pytest.raises(ValidationError):
        RequestPolicy(
            headers={"X-Custom-Session": "static"},
            session_header="x-custom-session",
        )


def test_generic_header_token_accepted_without_name_based_behavior():
    policy = RequestPolicy(
        headers={"X-Arbitrary-Transport": "transport/1.0"},
        session_header="X-Arbitrary-Session",
    )
    req = request(metadata={"conversation_id": "conv-1"}, request_id="req-1")
    headers = OpenAIProvider("https://neutral.example/v1", request_policy=policy)._headers(
        req, model="m"
    )
    assert headers["X-Arbitrary-Transport"] == "transport/1.0"
    assert headers["X-Arbitrary-Session"] == "conv-1"


def test_request_policy_and_model_entry_dto_round_trip_preserves_transport_fields():
    policy = RequestPolicy(
        headers={"User-Agent": "disco-test/1.0"},
        session_header="X-Custom-Session",
    )
    assert RequestPolicy.model_validate(policy.model_dump()) == policy
    entry = ModelEntry(
        model_id="unknown-build-9000",
        provider="endpoint",
        context_window=8192,
        base_url="https://unknown.example/v1",
        requires_api_key=False,
        request_policy=policy,
    )
    assert ModelEntry.model_validate(entry.model_dump()).request_policy == policy
