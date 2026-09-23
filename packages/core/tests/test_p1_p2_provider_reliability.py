"""P1 + P2 driver-reliability tests.

P1: only explicitly configured routing options reach the wire.
P2: LLMProviderUnavailable classification + driver routing-retry arm.
"""

from __future__ import annotations

import httpx
import pytest
from disco.core.events import LLMMessage
from disco.core.llm.errors import LLMError, LLMProviderUnavailable, LLMTransientError
from disco.core.llm.openai_provider import OpenAIProvider
from disco.core.llm.request_policy import RequestPolicy
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


# Explicit routing extensions, never selected by endpoint names.


@pytest.mark.parametrize(
    "url,name",
    [
        ("https://openrouter.ai/api/v1", "my-provider"),
        ("https://some-other-host.com/v1", "openrouter-custom"),
        ("http://localhost:8080/v1", "local"),
        ("https://api.openai.com/v1", "openai"),
    ],
)
def test_no_implicit_routing_extensions(url, name):
    p = OpenAIProvider(url, name=name)
    assert "provider" not in p._payload(_req({"allow_fallbacks": True}), "m", stream=False)


def test_explicit_routing_options_apply_to_any_endpoint():
    options = {"provider": {"require_parameters": True, "allow_fallbacks": False}}
    p = OpenAIProvider("https://unknown.example/v1", request_policy=RequestPolicy(body=options))
    body = p._payload(_req({"allow_fallbacks": True}), "m", stream=False)
    assert body["provider"] == options["provider"]


# ---- P2: LLMProviderUnavailable classification ------------------------------


def _make_error_resp(status: int, message: str, err_type: str = "") -> httpx.Response:
    payload = (
        {"error": {"message": message, "type": err_type}}
        if err_type
        else {"error": {"message": message}}
    )
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
    expected = "provider openrouter returned HTTP 400"
    if err_type:
        expected += f" type={err_type}"
    assert str(exc.value) == expected
    assert message not in str(exc.value)


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
