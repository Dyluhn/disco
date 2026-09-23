"""Request controls depend on explicit options, never vendor/model recognition."""

import json

import httpx
import pytest
from disco.core import LLMMessage
from disco.core.llm import CapabilityProfile, CompletionRequest, ModelRole
from disco.core.llm.openai_provider import OpenAIProvider
from disco.core.llm.request_policy import RequestPolicy
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError


def request(enabled=None, **kwargs):
    return CompletionRequest(
        profile=CapabilityProfile(role=ModelRole.RAG_ANSWERER),
        messages=[LLMMessage(role="user", content="Read this evidence.")],
        enable_thinking=enabled,
        **kwargs,
    )


@given(
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-", min_size=1, max_size=40),
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-/:", min_size=1, max_size=80),
)
def test_arbitrary_endpoint_and_model_names_cannot_select_wire_options(host, model):
    policy = RequestPolicy(
        reasoning_disabled={"custom_reasoning": {"active": False}},
        reasoning_enabled={"custom_reasoning": {"active": True}},
    )
    provider = OpenAIProvider(f"https://{host}.example/v1", name=model, request_policy=policy)
    for enabled in (False, True, None):
        body = provider._payload(request(enabled), model, stream=True)
        expected = OpenAIProvider("https://neutral.example/v1", request_policy=policy)._payload(
            request(enabled), model, stream=True
        )
        assert body == expected
        assert ("custom_reasoning" in body) == (enabled is not None)


@pytest.mark.parametrize(
    "url",
    [
        "https://ollama.com/v1",
        "https://api.deepseek.com/v1",
        "https://opencode.ai/zen/go/v1",
        "https://api.z.ai/v4",
        "https://openrouter.ai/api/v1",
        "http://localhost:8080/v1",
        "https://unknown.example/v1",
    ],
)
@pytest.mark.parametrize(
    "model",
    ["glm-5.3-flash", "deepseek-v4.1-flash", "qwen3.8-flash", "anthropic/claude", "future-model"],
)
def test_old_special_case_names_have_no_implicit_effect(url, model):
    actual = OpenAIProvider(url, name="openrouter")._payload(request(False), model, stream=True)
    expected = OpenAIProvider("https://neutral.example/v1")._payload(
        request(False), model, stream=True
    )
    assert actual == expected
    for key in (
        "thinking",
        "enable_thinking",
        "reasoning_effort",
        "chat_template_kwargs",
        "provider",
    ):
        assert key not in actual


@pytest.mark.parametrize(
    "extension",
    [
        {"reasoning_effort": "none"},
        {"reasoning_effort": "low"},
        {"thinking": {"type": "disabled"}},
        {"chat_template_kwargs": {"enable_thinking": False}},
        {"new_server_option": {"budget": 12, "enabled": False}},
    ],
)
@pytest.mark.parametrize("stream", [False, True])
async def test_arbitrary_declared_extensions_reach_the_http_boundary(extension, stream):
    seen = []

    def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        )

    provider = OpenAIProvider(
        "https://unknown.example/v1",
        request_policy=RequestPolicy(reasoning_disabled=extension),
        transport=httpx.MockTransport(handler),
    )
    if stream:
        chunks = [c async for c in provider.stream_complete(request(False), model="new-model")]
        assert chunks[-1].final.text == "ok"
    else:
        assert (await provider.complete(request(False), model="new-model")).text == "ok"
    for key, value in extension.items():
        assert seen[0][key] == value


def test_models_sharing_an_endpoint_keep_separate_policies():
    provider = OpenAIProvider(
        "https://neutral.example/v1",
        model_policies={
            "a": RequestPolicy(reasoning_disabled={"reasoning_effort": "none"}),
            "b": RequestPolicy(reasoning_disabled={"reasoning_effort": "low"}),
        },
    )
    assert provider._payload(request(False), "a", stream=False)["reasoning_effort"] == "none"
    assert provider._payload(request(False), "b", stream=False)["reasoning_effort"] == "low"
    assert "reasoning_effort" not in provider._payload(request(False), "c", stream=False)


def test_defaults_overrides_assist_and_nested_options_are_not_mutated():
    policy = RequestPolicy(
        body={"option": {"retain": 1}},
        default_reasoning=True,
        reasoning_enabled={"option": {"enabled": True}},
        reasoning_disabled={"option": {"enabled": False}},
    )
    provider = OpenAIProvider("https://neutral.example/v1", request_policy=policy)
    assert provider._payload(request(), "m", stream=False)["option"] == {
        "retain": 1,
        "enabled": True,
    }
    assert (
        provider._payload(request(True, assist=True, attempt=2), "m", stream=False)["option"][
            "enabled"
        ]
        is False
    )
    body = provider._payload(request(False), "m", stream=False)
    body["option"]["retain"] = 999
    assert policy.body == {"option": {"retain": 1}}


@pytest.mark.parametrize("field", ["body", "reasoning_enabled", "reasoning_disabled"])
@pytest.mark.parametrize("key", ["messages", "model", "stream", "tools", "max_tokens", "input"])
def test_extensions_cannot_replace_authoritative_request_fields(field, key):
    with pytest.raises(ValidationError):
        RequestPolicy(**{field: {key: "override"}})


def test_responses_controls_are_explicit_and_token_budget_is_exact():
    policy = RequestPolicy(reasoning_disabled={"reasoning": {"effort": "low"}})
    req = request(False, max_tokens=50)
    a = OpenAIProvider("https://api.meta.ai/v1/responses", request_policy=policy)._payload(
        req, "m", stream=False
    )
    b = OpenAIProvider("https://new.example/v1/responses", request_policy=policy)._payload(
        req, "m", stream=False
    )
    assert a == b
    assert a["reasoning"] == {"effort": "low"}
    assert a["max_output_tokens"] == 50
