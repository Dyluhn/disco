"""GLM 5.3's always-on thinking must respect a request for lightweight review."""

import pytest
from disco.core import LLMMessage
from disco.core.llm import CapabilityProfile, CompletionRequest, ModelRole
from disco.core.llm.openai_provider import OpenAIProvider


@pytest.mark.parametrize("url", ["https://opencode.ai/zen/go/v1", "https://api.z.ai/api/paas/v4"])
@pytest.mark.parametrize("model", ["glm-5.3", "glm-5.3-flash"])
@pytest.mark.parametrize("stream", [False, True])
def test_lightweight_glm53_uses_supported_effort_instead_of_ignoring_request(url, model, stream):
    request = CompletionRequest(
        profile=CapabilityProfile(role=ModelRole.RAG_ANSWERER),
        messages=[LLMMessage(role="user", content="Review this evidence.")],
        enable_thinking=False,
    )
    body = OpenAIProvider(url)._payload(request, model, stream=stream)
    assert body["reasoning_effort"] == "low"
    assert body["thinking"] == {"type": "enabled"}
    assert "chat_template_kwargs" not in body


@pytest.mark.parametrize(
    "model,url,thinking",
    [
        ("qwen3.8-flash", "https://opencode.ai/zen/go/v1", False),
        ("glm-5.2", "https://opencode.ai/zen/go/v1", False),
        ("glm-5.3-flash", "https://example.test/v1", False),
        ("glm-5.3-flash", "https://opencode.ai/zen/go/v1", True),
        ("glm-5.3-flash", "https://opencode.ai/zen/go/v1", None),
    ],
)
def test_other_models_hosts_and_explicit_thinking_keep_their_existing_policy(model, url, thinking):
    request = CompletionRequest(
        profile=CapabilityProfile(role=ModelRole.RAG_ANSWERER),
        messages=[LLMMessage(role="user", content="Review this evidence.")],
        enable_thinking=thinking,
    )
    body = OpenAIProvider(url)._payload(request, model, stream=False)
    assert "reasoning_effort" not in body
    assert "thinking" not in body


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("enabled", [False, True, None])
def test_qwen38_on_opencode_forwards_explicit_nonthinking_without_glm_policy(stream, enabled):
    request = CompletionRequest(
        profile=CapabilityProfile(role=ModelRole.RAG_ANSWERER),
        messages=[LLMMessage(role="user", content="Compare the evidence.")],
        enable_thinking=enabled,
    )
    body = OpenAIProvider("https://opencode.ai/zen/go/v1")._payload(
        request, "qwen3.8-flash", stream=stream
    )
    assert (body.get("enable_thinking") is False) == (enabled is False)
    assert "chat_template_kwargs" not in body
    assert "reasoning_effort" not in body


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "model",
    [
        "deepseek-v4-flash:0731",
        "deepseek-v4-flash",
        "deepseek-v4.1-flash",
        "deepseek-v4.1-flash:cloud",
    ],
)
def test_ollama_deepseek_flash_forwards_explicit_nonthinking(stream, model):
    request = CompletionRequest(
        profile=CapabilityProfile(role=ModelRole.RAG_ANSWERER),
        messages=[LLMMessage(role="user", content="Review this evidence.")],
        enable_thinking=False,
    )
    body = OpenAIProvider("https://ollama.com/v1")._payload(request, model, stream=stream)
    assert body["reasoning_effort"] == "none"
    assert "thinking" not in body and "chat_template_kwargs" not in body


@pytest.mark.parametrize(
    "model,url,thinking",
    [
        ("deepseek-v4-flash:0731", "https://ollama.com/v1", True),
        ("deepseek-v4-flash:0731", "https://ollama.com/v1", None),
        ("deepseek-v4-flash:0731", "https://example.test/v1", False),
        ("deepseek-v4-flash:0731", "https://ollama.com.example.test/v1", False),
        ("deepseek-v4-pro:0813", "https://ollama.com/v1", False),
        ("qwen3.8-flash", "https://ollama.com/v1", False),
    ],
)
def test_ollama_nonthinking_mapping_does_not_change_other_policies(model, url, thinking):
    request = CompletionRequest(
        profile=CapabilityProfile(role=ModelRole.RAG_ANSWERER),
        messages=[LLMMessage(role="user", content="Review this evidence.")],
        enable_thinking=thinking,
    )
    body = OpenAIProvider(url)._payload(request, model, stream=False)
    assert "reasoning_effort" not in body
