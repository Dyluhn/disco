"""BP-00 V3 unit matrix: content-parts shaping, vision capability gate + guard,
and Anthropic cache-block stability with the images field present."""

import pytest
from disco.core import LLMMessage
from disco.core.llm.config import default_config
from disco.core.llm.config_store import ConfigStore
from disco.core.llm.errors import NoEligibleModel
from disco.core.llm.openai_provider import OpenAIProvider
from disco.core.llm.routing import CallContext, DefaultLLMRouter
from disco.core.llm.types import (
    CapabilityProfile,
    CompletionRequest,
    ModelRole,
    Requirement,
)


def test_openai_content_parts():
    """OpenAIProvider emits content-parts when images are present (golden JSON)."""
    provider = OpenAIProvider(base_url="http://test", api_key="test")

    # Message without images keeps the plain-string shape.
    m1 = LLMMessage(role="user", content="hello")
    assert provider._message(m1) == {"role": "user", "content": "hello"}

    # Message with images: text part first, one image_url part per image.
    m2 = LLMMessage(role="user", content="look at this", images=["data:image/png;base64,abc"])
    expected = {
        "role": "user",
        "content": [
            {"type": "text", "text": "look at this"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
        ],
    }
    assert provider._message(m2) == expected


def test_vision_capability_guard(monkeypatch):
    """[DF-08 updated] Router raises NoEligibleModel when images are sent to
    a non-vision model AND no valid vision escalation is configured. The guard
    stays intact — it just now has an escape hatch via vision_escalation_model."""
    monkeypatch.setenv("PMX_DRIVER_VISION", "0")
    config = default_config()
    # Disable the DF-08 vision escalation so the guard fires in its original
    # BP-00 form — proving we didn't weaken the barrier when escalation is unset.
    config = config.model_copy(update={"vision_escalation_model": None})

    providers = {"qwen": OpenAIProvider(base_url="http://test", api_key="test")}
    router = DefaultLLMRouter(config, providers)

    req = CompletionRequest(
        profile=CapabilityProfile(role=ModelRole.AGENT_DRIVER),
        messages=[LLMMessage(role="user", content="hi", images=["data:image/png;base64,abc"])],
    )

    with pytest.raises(NoEligibleModel) as excinfo:
        router._resolve(req, CallContext())

    assert "does not support VISION" in str(excinfo.value)


def test_config_vision_gate(monkeypatch):
    """driver-local advertises Requirement.VISION only when PMX_DRIVER_VISION=1."""
    monkeypatch.setenv("PMX_DRIVER_VISION", "1")
    cfg_on = default_config()
    assert Requirement.VISION in cfg_on.models["driver-local"].capabilities

    monkeypatch.setenv("PMX_DRIVER_VISION", "0")
    cfg_off = default_config()
    assert Requirement.VISION not in cfg_off.models["driver-local"].capabilities


def test_config_store_overrides_stale_persisted_file(tmp_path, monkeypatch):
    """[BP-00 regression — live failure 2026-06-10] A config file persisted BEFORE
    the vision rollout (driver-local without VISION) must not pin the driver
    text-only when PMX_DRIVER_VISION=1: load() applies the env overlay over the
    authoritative file."""
    monkeypatch.setenv("PMX_DRIVER_VISION", "0")
    store = ConfigStore(tmp_path / "cfg.json")
    store.save(default_config())  # persisted WITHOUT vision

    monkeypatch.setenv("PMX_DRIVER_VISION", "1")
    cfg = store.load()
    assert Requirement.VISION in cfg.models["driver-local"].capabilities


def test_config_store_strips_vision_when_gate_off(tmp_path, monkeypatch):
    """Symmetric direction: a file that persisted VISION stops advertising it
    once the deployment loses the mmproj (env != 1) — the guard must keep
    protecting a text-only llama-server from image requests."""
    monkeypatch.setenv("PMX_DRIVER_VISION", "1")
    store = ConfigStore(tmp_path / "cfg.json")
    store.save(default_config())  # persisted WITH vision

    monkeypatch.setenv("PMX_DRIVER_VISION", "0")
    cfg = store.load()
    assert Requirement.VISION not in cfg.models["driver-local"].capabilities


def test_anthropic_cache_stays_string():
    """_mark_anthropic_cache keeps working: system content stays string-shaped
    (images never appear on system messages), so the cache-block conversion holds."""
    provider = OpenAIProvider(base_url="http://test", api_key="test")

    m = LLMMessage(role="system", content="you are a bot")
    body = {
        "model": "claude-3-sonnet",
        "messages": [provider._message(m)],
    }
    provider._mark_anthropic_cache(body)

    assert isinstance(body["messages"][0]["content"], list)
    assert body["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
