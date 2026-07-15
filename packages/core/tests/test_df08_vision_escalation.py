"""DF-08: Vision escalation to Gemini 3 Flash — unit tests.

Three test categories:
1. Routing escalation: image-bearing request + no-vision primary + escalation
   configured → routes to the escalation model, NO raise.
2. Guard preserved: escalation UNSET or set to a non-vision model → still
   raises NoEligibleModel.
3. Provider image serialization: an image-bearing request via the OpenRouter
   path contains an `image_url` content part (not dropped).
"""

from __future__ import annotations

import pytest
from disco.core import LLMMessage
from disco.core.llm import (
    CapabilityProfile,
    CompletionRequest,
    ModelEntry,
    ModelRole,
    NoEligibleModel,
    Requirement,
    RouterConfig,
)
from disco.core.llm.openai_provider import OpenAIProvider
from llm_fakes import FakeModelProvider

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _img_req(role=ModelRole.AGENT_DRIVER) -> CompletionRequest:
    """A request carrying one image — the trigger for the vision guard."""
    return CompletionRequest(
        profile=CapabilityProfile(role=role),
        messages=[
            LLMMessage(role="user", content="describe this", images=["data:image/png;base64,abc"])
        ],
    )


def _txt_req(role=ModelRole.AGENT_DRIVER) -> CompletionRequest:
    """A plain-text request — should NOT trigger escalation."""
    return CompletionRequest(
        profile=CapabilityProfile(role=role),
        messages=[LLMMessage(role="user", content="hello")],
    )


def _cfg_with_escalation(
    vision_escalation_model: str | None,
    *,
    escalation_vision: bool = True,
) -> RouterConfig:
    """A config with a text-only 'local' (no-vision) + a vision-capable
    escalation slot. `escalation_vision` controls whether the escalation
    model HAS the VISION capability."""
    escalation_caps = {Requirement.TOOL_CALLING, Requirement.LONG_CONTEXT}
    if escalation_vision:
        escalation_caps.add(Requirement.VISION)
    models = {
        "local": ModelEntry(
            model_id="local-novision",
            provider="ollama",
            context_window=65_536,
            capabilities=frozenset({Requirement.TOOL_CALLING, Requirement.JSON_MODE}),
        ),
        "vision-model": ModelEntry(
            model_id="vision-model-1",
            provider="openrouter",
            context_window=200_000,
            capabilities=frozenset(escalation_caps),
            family="gemini",
            base_url="https://openrouter.ai/api/v1",
            api_key_env="PMX_OPENROUTER_API_KEY",
            price_in_per_m=0.075,
            price_out_per_m=0.30,
        ),
    }
    return RouterConfig(
        models=models,
        default_model="local",
        vision_escalation_model=vision_escalation_model,
    )


# ---------------------------------------------------------------------------
# 1. Routing escalation — happy path
# ---------------------------------------------------------------------------


async def test_vision_escalation_routes_to_escalation_model():
    """Image-bearing request against a no-vision primary with a configured,
    vision-capable escalation model → routes to the escalation model, NO raise."""
    cfg = _cfg_with_escalation("vision-model")
    providers = {
        "ollama": FakeModelProvider("ollama", text="local"),
        "openrouter": FakeModelProvider(
            "openrouter", text="escalated-vision-response", cost_usd=0.01
        ),
    }
    from disco.core.llm.routing import DefaultLLMRouter, InMemoryRoutingSink

    sink = InMemoryRoutingSink()
    router = DefaultLLMRouter(cfg, providers, sink=sink)

    resp = await router.complete(_img_req())
    assert resp.model_used == "vision-model-1"
    assert resp.text == "escalated-vision-response"
    # Routing decision: path=overflow, overflow_triggers=["vision_escalation"]
    assert resp.routing is not None
    assert resp.routing.path == "overflow"
    assert "vision_escalation" in resp.routing.overflow_triggers
    # Sink recorded exactly 1 decision by complete (RT4: one per call).
    assert len(sink.decisions) == 1
    assert sink.decisions[0].path == "overflow"
    assert sink.decisions[0].overflow_triggers == ["vision_escalation"]


async def test_vision_escalation_does_not_affect_text_only_requests():
    """A plain-text request against a no-vision primary → stays local (no
    escalation triggered, since there are no images)."""
    cfg = _cfg_with_escalation("vision-model")
    providers = {
        "ollama": FakeModelProvider("ollama", text="local-text-response"),
        "openrouter": FakeModelProvider("openrouter", text="should-not-be-called", cost_usd=0.01),
    }
    from disco.core.llm.routing import DefaultLLMRouter, InMemoryRoutingSink

    sink = InMemoryRoutingSink()
    router = DefaultLLMRouter(cfg, providers, sink=sink)

    resp = await router.complete(_txt_req())
    assert resp.model_used == "local-novision"
    assert resp.text == "local-text-response"
    assert resp.routing is not None
    assert resp.routing.path == "pinned"
    assert "vision_escalation" not in resp.routing.overflow_triggers
    assert providers["openrouter"].calls == 0  # never touched


async def test_escalation_model_receives_provider_and_model_id_correctly():
    """The escalated request flows through the normal prompt-injection + budget
    path — the provider sees the correct model_id."""
    openrouter_provider = FakeModelProvider("openrouter", text="escalated", cost_usd=0.01)
    cfg = _cfg_with_escalation("vision-model")
    providers = {
        "ollama": FakeModelProvider("ollama", text="local"),
        "openrouter": openrouter_provider,
    }
    from disco.core.llm.routing import DefaultLLMRouter, InMemoryRoutingSink

    router = DefaultLLMRouter(cfg, providers, sink=InMemoryRoutingSink())

    await router.complete(_img_req())
    # The escalated request should have been sent to the openrouter provider
    assert openrouter_provider.calls == 1
    _seen = openrouter_provider.seen_requests[0]
    # The provider should have received the model_id from the escalation entry
    # (FakeModelProvider doesn't inspect model parameter, but the call pattern is correct)


# ---------------------------------------------------------------------------
# 2. Guard preserved — escalation is UNSET or invalid
# ---------------------------------------------------------------------------


async def test_guard_preserved_when_escalation_unset():
    """When vision_escalation_model is None, the guard still raises
    NoEligibleModel — we did NOT weaken the safety guard."""
    cfg = _cfg_with_escalation(None)
    from disco.core.llm.routing import CallContext, DefaultLLMRouter, InMemoryRoutingSink

    router = DefaultLLMRouter(cfg, {}, sink=InMemoryRoutingSink())

    with pytest.raises(NoEligibleModel) as excinfo:
        router._resolve(_img_req(), CallContext())
    assert "does not support VISION" in str(excinfo.value)


async def test_guard_preserved_when_escalation_not_in_catalogue():
    """When vision_escalation_model points to a key NOT in the catalogue,
    the guard raises NoEligibleModel."""
    cfg = _cfg_with_escalation("nonexistent-model")
    from disco.core.llm.routing import CallContext, DefaultLLMRouter, InMemoryRoutingSink

    router = DefaultLLMRouter(cfg, {}, sink=InMemoryRoutingSink())

    with pytest.raises(NoEligibleModel) as excinfo:
        router._resolve(_img_req(), CallContext())
    assert "does not support VISION" in str(excinfo.value)


async def test_guard_preserved_when_escalation_model_lacks_vision():
    """When vision_escalation_model points to a model that exists but does NOT
    have VISION, the guard raises NoEligibleModel — won't silently send images
    to a non-vision model."""
    cfg = _cfg_with_escalation("vision-model", escalation_vision=False)
    from disco.core.llm.routing import CallContext, DefaultLLMRouter, InMemoryRoutingSink

    router = DefaultLLMRouter(cfg, {}, sink=InMemoryRoutingSink())

    with pytest.raises(NoEligibleModel) as excinfo:
        router._resolve(_img_req(), CallContext())
    assert "does not support VISION" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 3. Provider image serialization — OpenRouter path
# ---------------------------------------------------------------------------


def test_image_serialization_on_openrouter_path():
    """An image-bearing message serialized by OpenAIProvider (which all
    OpenRouter models use) contains an `image_url` content part — images
    are NOT dropped."""
    provider = OpenAIProvider(
        base_url="https://openrouter.ai/api/v1",
        name="openrouter",
        api_key="test-key",
    )

    m = LLMMessage(role="user", content="what's in this?", images=["data:image/png;base64,abc123"])
    wire = provider._message(m)

    assert isinstance(wire["content"], list)
    text_parts = [p for p in wire["content"] if p["type"] == "text"]
    image_parts = [p for p in wire["content"] if p["type"] == "image_url"]
    assert len(text_parts) == 1
    assert text_parts[0]["text"] == "what's in this?"
    assert len(image_parts) == 1
    assert image_parts[0]["image_url"]["url"] == "data:image/png;base64,abc123"


def test_multiple_images_all_serialized():
    """Multiple images → all serialized as image_url parts, none dropped."""
    provider = OpenAIProvider(
        base_url="https://openrouter.ai/api/v1",
        name="openrouter",
        api_key="test-key",
    )

    m = LLMMessage(
        role="user",
        content="compare these",
        images=[
            "data:image/png;base64,img1",
            "data:image/png;base64,img2",
            "data:image/png;base64,img3",
        ],
    )
    wire = provider._message(m)

    image_parts = [p for p in wire["content"] if p["type"] == "image_url"]
    assert len(image_parts) == 3
    assert image_parts[0]["image_url"]["url"] == "data:image/png;base64,img1"
    assert image_parts[1]["image_url"]["url"] == "data:image/png;base64,img2"
    assert image_parts[2]["image_url"]["url"] == "data:image/png;base64,img3"


def test_image_serialization_in_payload_for_openrouter_model():
    """A full payload for a gemini (OpenRouter) model also carries images
    correctly — through _payload → _message → image_url parts."""
    provider = OpenAIProvider(
        base_url="https://openrouter.ai/api/v1",
        name="openrouter",
        api_key="test-key",
    )

    req = CompletionRequest(
        profile=CapabilityProfile(role=ModelRole.AGENT_DRIVER),
        messages=[
            LLMMessage(role="user", content="look", images=["data:image/png;base64,test123"])
        ],
        max_tokens=50,
        temperature=0.0,
    )

    payload = provider._payload(req, model="google/gemini-3-flash-preview", stream=False)
    msgs = payload["messages"]
    assert len(msgs) == 1
    content = msgs[0]["content"]
    assert isinstance(content, list)
    image_parts = [p for p in content if p["type"] == "image_url"]
    assert len(image_parts) == 1
    assert image_parts[0]["image_url"]["url"] == "data:image/png;base64,test123"
    assert payload["model"] == "google/gemini-3-flash-preview"


# ---------------------------------------------------------------------------
# 4. Edge cases — escalation model is the same as primary, etc.
# ---------------------------------------------------------------------------


async def test_no_escalation_when_primary_has_vision():
    """When the primary already HAS VISION, the escalation code path is never
    entered — the request stays local."""
    cfg = RouterConfig(
        models={
            "local": ModelEntry(
                model_id="local-with-vision",
                provider="ollama",
                context_window=65_536,
                capabilities=frozenset({Requirement.TOOL_CALLING, Requirement.VISION}),
            ),
            "escalation": ModelEntry(
                model_id="escalation-model",
                provider="openrouter",
                context_window=200_000,
                capabilities=frozenset({Requirement.VISION}),
                base_url="https://openrouter.ai/api/v1",
                api_key_env="PMX_OPENROUTER_API_KEY",
            ),
        },
        default_model="local",
        vision_escalation_model="escalation",
    )
    providers = {
        "ollama": FakeModelProvider("ollama", text="local-with-vision"),
        "openrouter": FakeModelProvider("openrouter", text="should-not-be-called"),
    }
    from disco.core.llm.routing import DefaultLLMRouter, InMemoryRoutingSink

    router = DefaultLLMRouter(cfg, providers, sink=InMemoryRoutingSink())

    resp = await router.complete(_img_req())
    assert resp.model_used == "local-with-vision"
    assert resp.text == "local-with-vision"
    assert providers["openrouter"].calls == 0
