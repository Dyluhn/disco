"""Wire-contract guard: tool results must immediately follow their tool call.

MiniMax rejects a message list where a ``role:"tool"`` result is not adjacent to
the assistant ``tool_calls`` that declared it (``bad_request_error`` 2013). The
render pipeline can splice messages between the pair or drop a turn entirely;
``_normalize_tool_call_ordering`` repairs the wire list at the serialization
boundary. These tests pin the four behaviors — no-op when healthy, re-adjacency
after a splice, orphan-drop, and stub-synthesis — plus end-to-end wiring through
``_payload``.
"""

from __future__ import annotations

import json

import httpx
from disco.core.events import LLMMessage
from disco.core.llm.openai_provider import (
    OpenAIProvider,
    _normalize_tool_call_ordering,
)
from disco.core.llm.types import CapabilityProfile, CompletionRequest, ModelRole


def _asst(cid: str, name: str = "do_thing") -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": cid, "type": "function", "function": {"name": name, "arguments": "{}"}}],
    }


def _tool(cid: str, content: str = "ok") -> dict:
    return {"role": "tool", "tool_call_id": cid, "content": content}


def _roles(msgs: list[dict]) -> list[str]:
    return [m["role"] for m in msgs]


def test_healthy_list_is_a_noop() -> None:
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "q"},
        _asst("a1"),
        _tool("a1"),
        {"role": "assistant", "content": "done"},
    ]
    out = _normalize_tool_call_ordering(msgs)
    # Identical ordering AND identical objects on the healthy path.
    assert out == msgs
    assert [id(m) for m in out] == [id(m) for m in msgs]


def test_spliced_message_pulls_result_adjacent() -> None:
    # An environment/system message got injected between the tool call and its
    # result (the workflow-handoff / snapshot-splice failure mode).
    spliced = {"role": "user", "content": "[environment update]"}
    msgs = [_asst("a1"), spliced, _tool("a1"), {"role": "user", "content": "next"}]
    out = _normalize_tool_call_ordering(msgs)
    assert _roles(out) == ["assistant", "tool", "user", "user"]
    # The tool result now immediately follows its call; the spliced msg moved after.
    assert out[1]["tool_call_id"] == "a1"
    assert out[2] is spliced


def test_orphan_tool_result_is_dropped() -> None:
    # The declaring assistant turn was condensed away; the bare result remains.
    msgs = [{"role": "user", "content": "q"}, _tool("ghost"), {"role": "assistant", "content": "hi"}]
    out = _normalize_tool_call_ordering(msgs)
    assert _roles(out) == ["user", "assistant"]
    assert all(m.get("tool_call_id") != "ghost" for m in out)


def test_missing_result_gets_stub() -> None:
    # A tool_call whose observation was dropped: synthesize a stub so the request
    # is well-formed instead of 400ing.
    msgs = [_asst("a1"), {"role": "user", "content": "continue"}]
    out = _normalize_tool_call_ordering(msgs)
    assert _roles(out) == ["assistant", "tool", "user"]
    assert out[1]["tool_call_id"] == "a1"
    assert "unavailable" in out[1]["content"]


def test_parallel_calls_results_ordered_by_declaration() -> None:
    asst = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": "a1", "type": "function", "function": {"name": "f", "arguments": "{}"}},
            {"id": "a2", "type": "function", "function": {"name": "g", "arguments": "{}"}},
        ],
    }
    # Results present but reversed and split by a stray message.
    msgs = [asst, _tool("a2", "second"), {"role": "user", "content": "noise"}, _tool("a1", "first")]
    out = _normalize_tool_call_ordering(msgs)
    assert _roles(out) == ["assistant", "tool", "tool", "user"]
    assert out[1]["tool_call_id"] == "a1"  # declaration order, not arrival order
    assert out[2]["tool_call_id"] == "a2"


def test_payload_wires_the_normalizer() -> None:
    # End-to-end through the provider: a broken (spliced) LLMMessage history is
    # repaired in the serialized body handed to the transport.
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"model": "m1", "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {}},
        )

    provider = OpenAIProvider("http://fake/v1", name="fake", transport=httpx.MockTransport(handler))
    req = CompletionRequest(
        profile=CapabilityProfile(role=ModelRole.RAG_ANSWERER),
        messages=[
            LLMMessage(role="user", content="q"),
            LLMMessage(role="assistant", content="", tool_calls=[{"id": "a1", "name": "do_thing", "arguments": {}}]),
            LLMMessage(role="user", content="[spliced environment update]"),
            LLMMessage(role="tool", content="result-body", tool_call_id="a1"),
        ],
        max_tokens=50,
        temperature=0.0,
    )

    import asyncio

    asyncio.run(provider.complete(req, model="m1"))
    wire = captured["body"]["messages"]
    roles = [m["role"] for m in wire]
    # The tool result immediately follows the assistant tool_call in the wire body.
    ai = roles.index("assistant")
    assert wire[ai].get("tool_calls"), "assistant tool_calls preserved"
    assert roles[ai + 1] == "tool"
    assert wire[ai + 1]["tool_call_id"] == "a1"


def test_opencode_go_adds_neutral_user_continuation_after_tool_result() -> None:
    req = CompletionRequest(
        profile=CapabilityProfile(role=ModelRole.AGENT_DRIVER),
        messages=[
            LLMMessage(role="user", content="q"),
            LLMMessage(
                role="assistant",
                content="",
                tool_calls=[{"id": "a1", "name": "do_thing", "arguments": {}}],
            ),
            LLMMessage(role="tool", content="result-body", tool_call_id="a1"),
        ],
    )

    go = OpenAIProvider("https://opencode.ai/zen/go/v1", name="opencode-go")
    go_wire = go._payload(req, "deepseek-v4-flash", stream=False)["messages"]
    assert [message["role"] for message in go_wire[-2:]] == ["tool", "user"]
    assert go_wire[-1]["content"] == "Continue from the tool result above."

    conforming = OpenAIProvider("https://api.openai.com/v1", name="openai")
    conforming_wire = conforming._payload(req, "m1", stream=False)["messages"]
    assert conforming_wire[-1]["role"] == "tool"
