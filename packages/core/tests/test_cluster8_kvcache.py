"""Cluster 8 — KV-cache hardening: deterministic tool-call serialization +
cached-token measurement."""

from __future__ import annotations

import json

from perpleximanus.core.llm.openai_provider import OpenAIProvider


def _adapter():
    return OpenAIProvider("http://x/v1", api_key=None)


# ---- deterministic serialization (stable cache prefix) ----------------------


def test_tool_call_arguments_serialize_with_sorted_keys():
    from perpleximanus.core.events import LLMMessage

    adapter = _adapter()
    # An assistant message carrying a tool_call with unsorted dict args.
    msg = LLMMessage(
        role="assistant",
        content="",
        tool_calls=[{"id": "c1", "name": "shell", "arguments": {"b": 2, "a": 1, "m": 3}}],
    )
    out = adapter._message(msg)
    args_str = out["tool_calls"][0]["function"]["arguments"]
    # Keys appear in sorted order, byte-stable across runs.
    assert args_str == json.dumps({"a": 1, "b": 2, "m": 3}, sort_keys=True)
    assert list(json.loads(args_str).keys()) == ["a", "b", "m"]


# ---- cached-token measurement -----------------------------------------------


def test_cached_tokens_from_openai_shape():
    usage = {"prompt_tokens": 1000, "prompt_tokens_details": {"cached_tokens": 800}}
    assert OpenAIProvider._cached_tokens(usage) == 800


def test_cached_tokens_from_anthropic_shape():
    usage = {"prompt_tokens": 1000, "cache_read_input_tokens": 600}
    assert OpenAIProvider._cached_tokens(usage) == 600


def test_cached_tokens_zero_when_unreported():
    assert OpenAIProvider._cached_tokens({"prompt_tokens": 1000}) == 0
    assert OpenAIProvider._cached_tokens({}) == 0


def test_response_surfaces_cached_tokens():
    adapter = _adapter()
    data = {
        "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 1000,
            "completion_tokens": 10,
            "prompt_tokens_details": {"cached_tokens": 900},
        },
        "model": "m",
    }
    from perpleximanus.core.llm.types import CapabilityProfile, CompletionRequest, ModelRole

    req = CompletionRequest(
        profile=CapabilityProfile(role=ModelRole.AGENT_DRIVER), messages=[]
    )
    resp = adapter._to_response(req, "m", data)
    assert resp.usage.cached_tokens == 900
    assert resp.usage.input_tokens == 1000
