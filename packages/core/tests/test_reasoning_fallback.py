"""Transport-boundary recovery; success SSE is captured, rejections are simulated."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from disco.core.llm.errors import (
    LLMAuthError,
    LLMContentFiltered,
    LLMContextWindowExceeded,
    LLMError,
    LLMTransientError,
)
from disco.core.llm.openai_provider import OpenAIProvider
from disco.core.llm.request_policy import RequestPolicy
from disco.core.llm.stream_progress import observe_stream_progress
from disco.core.llm.types import ToolSpec
from test_request_policy import request

CAPTURE = Path(__file__).with_name("fixtures") / "ollama-20260922/deepseek-v4.1-flash.sse"
POLICY = RequestPolicy(
    body={"routing_constraint": "private", "custom": {"retain": 7, "effort": "default"}},
    reasoning_disabled={"custom": {"effort": "minimal"}, "optional_switch": False},
)


def success(protocol="chat"):
    if protocol == "responses":
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": '{"ok":true}'},
                        ],
                    }
                ],
                "usage": {"input_tokens": 12, "output_tokens": 4},
            },
        )
    return httpx.Response(
        200, content=CAPTURE.read_bytes(), headers={"content-type": "text/event-stream"}
    )


async def run(provider, req, delivery):
    if delivery == "complete":
        return await provider.complete(req, model="arbitrary-future-model")
    chunks = [c async for c in provider.stream_complete(req, model="arbitrary-future-model")]
    assert chunks[-1].done
    return chunks[-1].final


@pytest.mark.parametrize("status", [400, 422])
@pytest.mark.parametrize("protocol", ["chat", "responses"])
@pytest.mark.parametrize("delivery", ["complete", "stream"])
async def test_rejection_recovers_once_preserving_request_and_fixed_options(
    status, protocol, delivery
):
    seen, progress, ledger = [], [], []

    def handler(req):
        seen.append(req)
        if len(seen) == 1:
            # No parameter-name or vendor matching is needed (including blank error bodies).
            return httpx.Response(status, text="")
        return success(protocol)

    provider = OpenAIProvider(
        "https://arbitrary.test/v1" + ("/responses" if protocol == "responses" else ""),
        api_key="test-secret",
        request_policy=POLICY,
        transport=httpx.MockTransport(handler),
    )
    provider._ledger_emit = lambda req, model, payload, **kw: ledger.append(payload)
    req = request(
        False,
        max_tokens=123,
        metadata={"inspect_stage": "source_reading", "source_id": "p1"},
        tools=(
            [ToolSpec(name="read", description="read", parameters_schema={"type": "object"})]
            if protocol == "chat"
            else None
        ),
    )
    original = req.model_dump()
    original_policy = POLICY.model_dump()

    async def observe(row):
        progress.append(row)

    with observe_stream_progress(observe):
        result = await run(provider, req, delivery)
    assert json.loads(result.text) == {"ok": True}
    assert result.response_metadata["reasoning_control_fallback"] is True
    assert len(seen) == len(ledger) == 2
    first, second = [json.loads(r.content) for r in seen]
    assert first.pop("optional_switch") is False
    first["custom"]["effort"] = "default"
    assert first == second
    assert second["routing_constraint"] == "private"
    assert second["custom"] == {"retain": 7, "effort": "default"}
    assert all(r.headers["authorization"] == "Bearer test-secret" for r in seen)
    headers = [{k: v for k, v in r.headers.items() if k != "content-length"} for r in seen]
    assert headers[0] == headers[1]
    assert req.model_dump() == original
    assert POLICY.model_dump() == original_policy
    assert "reasoning_control_fallback" not in progress[0].details
    assert progress[-1].details == {"source_id": "p1", "reasoning_control_fallback": True}
    assert ledger == [json.loads(r.content) for r in seen]


@pytest.mark.parametrize(
    "policy",
    [
        RequestPolicy(),
        RequestPolicy(reasoning_disabled={}),
        RequestPolicy(reasoning_enabled={"only_when_enabled": True}),
        RequestPolicy(body={"x": 3}, reasoning_disabled={"x": 3}),
    ],
)
async def test_no_wire_change_means_no_retry(policy):
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(422, text="invalid request")

    provider = OpenAIProvider(
        "https://arbitrary.test/v1", request_policy=policy, transport=httpx.MockTransport(handler)
    )
    with pytest.raises(LLMError):
        await provider.complete(request(False), model="future")
    assert len(seen) == 1


@pytest.mark.parametrize(
    "status,error_type,error_message,expected",
    [
        (400, "authentication_error", "stream access denied", LLMAuthError),
        (422, "content_filter", "stream filtered", LLMContentFiltered),
        (400, "context_length_exceeded", "maximum context length", LLMContextWindowExceeded),
        (400, "server_error", "stream upstream failure", LLMTransientError),
        (401, "", "access denied", LLMAuthError),
        (429, "", "rate limit", LLMTransientError),
        (503, "", "unavailable", LLMTransientError),
    ],
)
@pytest.mark.parametrize("delivery", ["complete", "stream"])
async def test_typed_errors_keep_their_semantics(
    status, error_type, error_message, expected, delivery
):
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(
            status,
            json={"error": {"type": error_type, "message": error_message}},
            headers={"retry-after": "3"},
        )

    provider = OpenAIProvider(
        "https://arbitrary.test/v1", request_policy=POLICY, transport=httpx.MockTransport(handler)
    )
    with pytest.raises(expected) as caught:
        await run(provider, request(False), delivery)
    assert len(seen) == 1
    if expected is LLMTransientError:
        assert caught.value.retry_after_s == 3


@pytest.mark.parametrize("delivery", ["complete", "stream"])
async def test_baseline_failure_is_bounded_and_preserves_the_final_error(delivery):
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(422 if len(seen) == 1 else 401, text="rejected")

    provider = OpenAIProvider(
        "https://arbitrary.test/v1", request_policy=POLICY, transport=httpx.MockTransport(handler)
    )
    with pytest.raises(LLMAuthError):
        await run(provider, request(False), delivery)
    assert len(seen) == 2


async def test_repeated_generic_rejection_stops_after_two_posts():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(400, text="invalid")

    provider = OpenAIProvider(
        "https://arbitrary.test/v1", request_policy=POLICY, transport=httpx.MockTransport(handler)
    )
    with pytest.raises(LLMError) as caught:
        await provider.complete(request(False), model="future")
    assert type(caught.value) is LLMError
    assert len(seen) == 2


@pytest.mark.parametrize("stream_first", [True, False])
async def test_stream_and_reasoning_fallbacks_compose_with_a_finite_budget(stream_first):
    seen = []

    def handler(req):
        body = json.loads(req.content)
        seen.append(body)
        if body["stream"] and (stream_first or "optional_switch" not in body):
            return httpx.Response(400, text="stream unsupported")
        if "optional_switch" in body:
            return httpx.Response(422, text="optional field unsupported")
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        )

    provider = OpenAIProvider(
        "https://arbitrary.test/v1", request_policy=POLICY, transport=httpx.MockTransport(handler)
    )
    result = await provider.complete(request(False), model="future")
    assert result.text == "ok"
    assert result.response_metadata["reasoning_control_fallback"] is True
    assert len(seen) == (4 if stream_first else 3)
    assert seen[-1]["stream"] is False
    assert "optional_switch" not in seen[-1]


@pytest.mark.parametrize("cancel_at", ["rejection", "fallback"])
async def test_cancellation_prevents_or_interrupts_fallback(cancel_at):
    seen = []
    reached = asyncio.Event()

    async def handler(req):
        seen.append(req)
        if len(seen) == 1:
            if cancel_at == "rejection":
                asyncio.current_task().cancel()
            return httpx.Response(422)
        reached.set()
        await asyncio.Event().wait()

    provider = OpenAIProvider(
        "https://arbitrary.test/v1", request_policy=POLICY, transport=httpx.MockTransport(handler)
    )
    task = asyncio.create_task(provider.complete(request(False), model="future"))
    if cancel_at == "fallback":
        await asyncio.wait_for(reached.wait(), 2)
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(seen) == (1 if cancel_at == "rejection" else 2)


async def test_partial_output_is_never_replayed():
    seen = []

    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"already delivered"}}]}\n\n'
            raise httpx.ReadError("disconnected")

    def handler(req):
        seen.append(req)
        return httpx.Response(
            200, stream=BrokenStream(), headers={"content-type": "text/event-stream"}
        )

    provider = OpenAIProvider(
        "https://arbitrary.test/v1", request_policy=POLICY, transport=httpx.MockTransport(handler)
    )
    chunks = []
    with pytest.raises(LLMTransientError):
        async for chunk in provider.stream_complete(request(False), model="future"):
            chunks.append(chunk)
    assert "".join(c.delta_text for c in chunks) == "already delivered"
    assert len(seen) == 1


async def test_success_does_not_claim_honored_controls_or_poison_future_calls():
    seen = []

    def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(400) if len(seen) == 1 else success()

    provider = OpenAIProvider(
        "https://arbitrary.test/v1", request_policy=POLICY, transport=httpx.MockTransport(handler)
    )
    await provider.complete(request(False), model="future")
    result = await provider.complete(request(False), model="future")
    assert len(seen) == 3
    assert "optional_switch" in seen[2]
    assert "reasoning_control_fallback" not in result.response_metadata


@pytest.mark.parametrize("enabled", [True, False, None])
@pytest.mark.parametrize("delivery", ["complete", "stream"])
async def test_both_intents_and_policy_default_recover_without_affecting_other_models(
    enabled, delivery
):
    seen = []
    policy = RequestPolicy(
        default_reasoning=True,
        reasoning_enabled={"reasoning_option": "on"},
        reasoning_disabled={"reasoning_option": "off"},
    )

    def handler(req):
        body = json.loads(req.content)
        seen.append(body)
        return httpx.Response(422) if "reasoning_option" in body else success()

    provider = OpenAIProvider(
        "https://arbitrary.test/v1",
        model_policies={"arbitrary-future-model": policy},
        transport=httpx.MockTransport(handler),
    )
    result = await run(provider, request(enabled), delivery)
    assert result.response_metadata["reasoning_control_fallback"] is True
    assert seen[0]["reasoning_option"] == ("off" if enabled is False else "on")
    assert "reasoning_option" not in seen[1]
    other = await provider.complete(request(enabled), model="other-model")
    assert "reasoning_control_fallback" not in other.response_metadata
    assert len(seen) == 3
    assert "reasoning_option" not in seen[2]
