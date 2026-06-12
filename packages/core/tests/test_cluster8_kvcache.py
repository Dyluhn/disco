"""Cluster 8 — KV-cache hardening: deterministic tool-call serialization +
cached-token measurement."""

from __future__ import annotations

import json

from disco.core.llm.openai_provider import OpenAIProvider


def _adapter():
    return OpenAIProvider("http://x/v1", api_key=None)


# ---- deterministic serialization (stable cache prefix) ----------------------


def test_tool_call_arguments_serialize_with_sorted_keys():
    from disco.core.events import LLMMessage

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
    from disco.core.llm.types import CapabilityProfile, CompletionRequest, ModelRole

    req = CompletionRequest(
        profile=CapabilityProfile(role=ModelRole.AGENT_DRIVER), messages=[]
    )
    resp = adapter._to_response(req, "m", data)
    assert resp.usage.cached_tokens == 900
    assert resp.usage.input_tokens == 1000


# ---- GAP H: prompt-cache markers --------------------------------------------


def _req(system: str = "You are an agent.", tools=None):
    from disco.core.events import LLMMessage
    from disco.core.llm.types import (
        CapabilityProfile,
        CompletionRequest,
        ModelRole,
        ToolSpec,
    )

    tool_specs = [ToolSpec(name=n, description="", parameters_schema={}) for n in (tools or [])]
    return CompletionRequest(
        profile=CapabilityProfile(role=ModelRole.AGENT_DRIVER),
        messages=[
            LLMMessage(role="system", content=system),
            LLMMessage(role="user", content="hi"),
        ],
        tools=tool_specs,
    )


def test_prompt_cache_key_is_stable_for_an_identical_prefix():
    adapter = _adapter()
    a = adapter._payload(_req(tools=["shell", "file_write"]), "qwen", stream=False)
    # order-insensitive: sorted tool names → same key
    b = adapter._payload(_req(tools=["file_write", "shell"]), "qwen", stream=False)
    assert a["prompt_cache_key"].startswith("pmx-")
    assert a["prompt_cache_key"] == b["prompt_cache_key"]  # same prefix → same bucket


def test_prompt_cache_key_differs_when_the_prefix_changes():
    adapter = _adapter()
    a = adapter._payload(_req(system="You are an agent."), "qwen", stream=False)
    b = adapter._payload(_req(system="You are a DIFFERENT agent."), "qwen", stream=False)
    assert a["prompt_cache_key"] != b["prompt_cache_key"]


def test_local_model_keeps_plain_string_content_no_cache_control():
    adapter = _adapter()
    body = adapter._payload(_req(tools=["shell"]), "qwen3.6-27b", stream=False)
    # local/OpenAI: system content stays a plain string (block arrays would be rejected)
    sys_msg = next(m for m in body["messages"] if m["role"] == "system")
    assert isinstance(sys_msg["content"], str)
    assert "cache_control" not in body["tools"][0]


def test_anthropic_model_gets_cache_control_breakpoints():
    adapter = _adapter()
    req = _req(tools=["shell", "file_write"])
    body = adapter._payload(req, "anthropic/claude-3.5-sonnet", stream=False)
    sys_msg = next(m for m in body["messages"] if m["role"] == "system")
    # system prompt → a single cacheable text block
    assert isinstance(sys_msg["content"], list)
    assert sys_msg["content"][0]["cache_control"] == {"type": "ephemeral"}
    # the tool surface → a breakpoint on the last tool
    assert body["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    # still also routed by key
    assert body["prompt_cache_key"].startswith("pmx-")
