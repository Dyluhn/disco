"""P1 + P2 driver-reliability tests.

P1: _payload emits `provider` block for OpenRouter, byte-identical for local.
P2: LLMProviderUnavailable classification + driver routing-retry arm.
"""

from __future__ import annotations

import httpx
import pytest
from disco.core.events import LLMMessage
from disco.core.llm.errors import LLMError, LLMProviderUnavailable, LLMTransientError
from disco.core.llm.openai_provider import OpenAIProvider
from disco.core.llm.types import CapabilityProfile, CompletionRequest, ModelRole

# ---- helpers ----------------------------------------------------------------


def _req(provider_prefs=None) -> CompletionRequest:
    return CompletionRequest(
        profile=CapabilityProfile(role=ModelRole.AGENT_DRIVER),
        messages=[LLMMessage(role="user", content="hello")],
        max_tokens=50,
        temperature=0.0,
        provider_prefs=provider_prefs,
    )


def _openrouter_provider(handler=None) -> OpenAIProvider:
    t = httpx.MockTransport(handler) if handler else None
    return OpenAIProvider(
        "https://openrouter.ai/api/v1",
        name="openrouter",
        api_key="test-key",
        transport=t,
    )


def _local_provider(handler=None) -> OpenAIProvider:
    t = httpx.MockTransport(handler) if handler else None
    return OpenAIProvider(
        "http://localhost:8080/v1",
        name="llamacpp",
        transport=t,
    )


# ---- P1: _is_openrouter gate ------------------------------------------------


def test_is_openrouter_true_for_openrouter_base_url():
    p = OpenAIProvider("https://openrouter.ai/api/v1", name="my-provider")
    assert p._is_openrouter() is True


def test_is_openrouter_true_for_openrouter_name():
    p = OpenAIProvider("https://some-other-host.com/v1", name="openrouter-custom")
    assert p._is_openrouter() is True


def test_is_openrouter_false_for_local():
    p = OpenAIProvider("http://localhost:8080/v1", name="llamacpp")
    assert p._is_openrouter() is False


def test_is_openrouter_false_for_openai_direct():
    p = OpenAIProvider("https://api.openai.com/v1", name="openai")
    assert p._is_openrouter() is False


# ---- P1: _payload provider block injection ----------------------------------


def test_payload_emits_provider_for_openrouter():
    p = _openrouter_provider()
    body = p._payload(_req(), model="gpt-4o", stream=False)
    assert "provider" in body
    assert body["provider"]["require_parameters"] is True
    assert body["provider"]["allow_fallbacks"] is True


def test_payload_provider_block_default_no_prefs():
    p = _openrouter_provider()
    body = p._payload(_req(), model="gpt-4o", stream=False)
    assert body["provider"] == {"require_parameters": True, "allow_fallbacks": True}


def test_payload_provider_prefs_merged_correctly():
    """Caller-supplied prefs extend the floor; all three keys present."""
    prefs = {"ignore": ["Chutes"]}
    p = _openrouter_provider()
    body = p._payload(_req(provider_prefs=prefs), model="gpt-4o", stream=False)
    assert body["provider"]["require_parameters"] is True
    assert body["provider"]["allow_fallbacks"] is True
    assert body["provider"]["ignore"] == ["Chutes"]


def test_payload_caller_prefs_win_on_collision():
    """Caller override of allow_fallbacks to False wins (merge order: floor then prefs)."""
    prefs = {"allow_fallbacks": False}
    p = _openrouter_provider()
    body = p._payload(_req(provider_prefs=prefs), model="gpt-4o", stream=False)
    assert body["provider"]["allow_fallbacks"] is False
    assert body["provider"]["require_parameters"] is True


def test_payload_no_provider_block_for_local():
    """Local/llamacpp payloads must be byte-identical — no provider key."""
    p = _local_provider()
    body = p._payload(_req(), model="qwen-local", stream=False)
    assert "provider" not in body


def test_payload_byte_identical_local_with_and_without_prefs():
    """Setting provider_prefs does NOT leak into local payload."""
    p = _local_provider()
    body_none = p._payload(_req(), model="qwen-local", stream=False)
    body_prefs = p._payload(
        _req(provider_prefs={"ignore": ["SomeProvider"]}), model="qwen-local", stream=False
    )
    # The two dicts must be equal — prefs do not appear in the local payload.
    assert body_none == body_prefs


# ---- P2: LLMProviderUnavailable classification ------------------------------


def _make_error_resp(status: int, message: str, err_type: str = "") -> httpx.Response:
    payload = {"error": {"message": message, "type": err_type}} if err_type else {"error": {"message": message}}
    return httpx.Response(status, json=payload)


@pytest.mark.parametrize(
    "message,err_type",
    [
        ("No cookie auth credentials found for this model", ""),
        ("No allowed providers", ""),
        ("No instances available", ""),
        ("No endpoints found for this model", ""),
        ("Provider returned error: upstream timeout", ""),
        ("This content requires moderation", ""),
        # err_type startswith "provider"
        ("something", "provider_error"),
        ("something", "provider_timeout"),
    ],
)
async def test_provider_rejection_classifies_to_provider_unavailable(message, err_type):
    def handler(request: httpx.Request) -> httpx.Response:
        return _make_error_resp(400, message, err_type)

    p = _openrouter_provider(handler)
    with pytest.raises(LLMProviderUnavailable) as exc:
        await p.complete(_req(), model="m")
    assert message in str(exc.value)


async def test_provider_unavailable_is_transient_subclass():
    """LLMProviderUnavailable must be a LLMTransientError (inherits backoff path)."""
    exc = LLMProviderUnavailable("test", provider="test")
    assert isinstance(exc, LLMTransientError)
    assert isinstance(exc, LLMError)


async def test_unrelated_4xx_stays_llm_error():
    """A generic 4xx with an unrelated message must NOT be over-captured."""
    def handler(request: httpx.Request) -> httpx.Response:
        return _make_error_resp(400, "something specific broke", "invalid_request")

    p = _openrouter_provider(handler)
    with pytest.raises(LLMError) as exc:
        await p.complete(_req(), model="m")
    # Must NOT be LLMProviderUnavailable
    assert not isinstance(exc.value, LLMProviderUnavailable)


async def test_auth_error_not_captured_as_provider_unavailable():
    """401 auth errors must remain LLMAuthError, not get reclassified."""
    from disco.core.llm.errors import LLMAuthError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, json={"error": {"message": "Invalid API Key", "type": "authentication_error"}}
        )

    p = _openrouter_provider(handler)
    with pytest.raises(LLMAuthError):
        await p.complete(_req(), model="m")
