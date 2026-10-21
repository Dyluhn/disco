"""Distinguishable HTTP fixtures prove dispatch/wire behavior, not model quality."""

import json

import httpx
import pytest
from disco.core import LLMMessage
from disco.core.llm import (
    CapabilityProfile,
    CompletionRequest,
    DefaultLLMRouter,
    ModelEntry,
    ModelRole,
    Requirement,
    RouterConfig,
)
from disco.core.llm.openai_provider import OpenAIProvider
from disco.core.llm.request_policy import RequestPolicy


@pytest.mark.parametrize("policy_state", ["unknown", "supported", "unsupported"])
@pytest.mark.parametrize("thinking", [False, True])
@pytest.mark.parametrize("delivery", ["complete", "stream"])
async def test_three_specialists_dispatch_to_assigned_endpoints_with_explicit_policies(
    policy_state, thinking, delivery
):
    seen = []
    roles = (ModelRole.RAG_ANSWERER, ModelRole.QUERY_REWRITER, ModelRole.SUMMARIZER)
    models, providers, assignments = {}, {}, {}
    policy = {
        "unknown": RequestPolicy(),
        "supported": RequestPolicy(
            reasoning_enabled={"custom_reasoning": True},
            reasoning_disabled={"custom_reasoning": False},
        ),
        "unsupported": RequestPolicy(reasoning_enabled={}, reasoning_disabled={}),
    }[policy_state]

    def handle(request):
        body = json.loads(request.content)
        seen.append((request.url.host, body))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": request.url.host}, "finish_reason": "stop"}]},
        )

    for index, role in enumerate(roles):
        key = f"service-{index}"
        models[key] = ModelEntry(
            model_id=f"unfamiliar-{index}",
            provider=key,
            context_window=8192,
            capabilities=frozenset({Requirement.JSON_MODE})
            if policy_state == "supported"
            else frozenset(),
        )
        assignments[role] = key
        providers[key] = OpenAIProvider(
            f"https://endpoint-{index}.invalid/v1",
            name=key,
            request_policy=policy,
            transport=httpx.MockTransport(handle),
        )
    router = DefaultLLMRouter(
        RouterConfig(models=models, default_model="service-0", assignments=assignments), providers
    )
    for index, role in enumerate(roles):
        req = CompletionRequest(
            profile=CapabilityProfile(role=role),
            messages=[LLMMessage(role="user", content=f"Task for {role.value}")],
            enable_thinking=thinking,
        )
        if delivery == "complete":
            result = await router.complete(req)
        else:
            chunks = [chunk async for chunk in router.stream_complete(req)]
            result = chunks[-1].final
        assert result.text == f"endpoint-{index}.invalid"
        assert result.model_used == f"unfamiliar-{index}"
        assert result.routing.provider == f"service-{index}"
        host, body = seen[-1]
        assert host == f"endpoint-{index}.invalid"
        assert body["model"] == f"unfamiliar-{index}"
        if policy_state == "supported":
            assert body["custom_reasoning"] is thinking
        else:
            assert "custom_reasoning" not in body
    assert len(seen) == 3  # catalogue absence creates no detection request


@pytest.mark.parametrize("fallback_state", ["ready", "unavailable", "disabled"])
async def test_transient_failure_reaches_only_configured_fallback(fallback_state):
    from disco.core.llm import LLMTransientError
    from disco.core.llm.config import ROLE_FALLBACK_PROVIDER_KEY, RoleFallbackSettings

    seen = []

    def handle(request):
        seen.append((request.url.host, json.loads(request.content)["model"]))
        if request.url.host == "primary.invalid" or fallback_state == "unavailable":
            return httpx.Response(503, json={"error": {"message": "temporarily unavailable"}})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "fallback answer"}, "finish_reason": "stop"}]
            },
        )

    config = RouterConfig(
        models={
            "main": ModelEntry(model_id="primary-model", provider="primary", context_window=8192)
        },
        default_model="main",
        role_fallback=RoleFallbackSettings(
            enabled=fallback_state != "disabled",
            base_url="https://fallback.invalid/v1",
            model="configured-fallback",
        ),
    )
    providers = {
        "primary": OpenAIProvider(
            "https://primary.invalid/v1", name="primary", transport=httpx.MockTransport(handle)
        ),
        ROLE_FALLBACK_PROVIDER_KEY: OpenAIProvider(
            "https://fallback.invalid/v1", name="fallback", transport=httpx.MockTransport(handle)
        ),
    }
    router = DefaultLLMRouter(config, providers)
    req = CompletionRequest(
        profile=CapabilityProfile(role=ModelRole.QUERY_REWRITER),
        messages=[LLMMessage(role="user", content="Rewrite the query")],
    )
    if fallback_state == "ready":
        result = await router.complete(req)
        assert result.text == "fallback answer"
        assert result.routing.path == "role_fallback"
        assert result.model_used == "configured-fallback"
    else:
        with pytest.raises(LLMTransientError):
            await router.complete(req)
    primary_calls = [item for item in seen if item[0] == "primary.invalid"]
    fallback_calls = [item for item in seen if item[0] == "fallback.invalid"]
    assert len(primary_calls) == 8
    assert all(model == "primary-model" for _, model in primary_calls)
    if fallback_state == "disabled":
        assert not fallback_calls
    else:
        assert 1 <= len(fallback_calls) <= 8
        assert all(model == "configured-fallback" for _, model in fallback_calls)
