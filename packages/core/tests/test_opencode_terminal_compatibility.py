"""OpenCode Go terminal-role compatibility must avoid a hidden driver requery."""

from __future__ import annotations

import json

import httpx
from disco.core import LLMMessage, View
from disco.core.llm import DefaultLLMRouter, ModelEntry, Requirement, RouterConfig
from disco.core.llm.openai_provider import OpenAIProvider
from disco.core.llm.request_policy import RequestPolicy
from disco.core.loop.agent import BuildAgent
from disco.core.loop.control import Disp
from loop_fakes import build_loop


def _tool_call_sse() -> bytes:
    chunks = [
        {
            "model": "deepseek-v4-flash",
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_finish",
                                "type": "function",
                                "function": {
                                    "name": "finish",
                                    "arguments": '{"summary":"ok"}',
                                },
                            }
                        ]
                    }
                }
            ],
        },
        {
            "model": "deepseek-v4-flash",
            "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        },
    ]
    return (
        "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    ).encode()


async def test_assistant_terminal_history_completes_in_one_call_without_provider_repair(
    caplog,
) -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if body["messages"][-1]["role"] != "user":
            return httpx.Response(
                400,
                json={"error": {"message": "redacted", "type": "invalid_request"}},
            )
        return httpx.Response(
            200,
            content=_tool_call_sse(),
            headers={"content-type": "text/event-stream"},
        )

    provider = OpenAIProvider(
        "https://opencode.ai/zen/go/v1",
        name="opencode-go",
        request_policy=RequestPolicy(require_user_continuation=True),
        transport=httpx.MockTransport(handler),
    )
    config = RouterConfig(
        models={
            "go": ModelEntry(
                model_id="deepseek-v4-flash",
                provider="opencode-go",
                context_window=128_000,
                capabilities=frozenset({Requirement.TOOL_CALLING}),
            )
        },
        default_model="go",
    )
    agent = BuildAgent(DefaultLLMRouter(config, {"opencode-go": provider}), conversation_id="conv")
    loop, _store = build_loop(agent)
    view = View(
        messages=[
            LLMMessage(role="user", content="Build the site."),
            LLMMessage(role="assistant", content="I will continue the build."),
        ],
        visible_seqs=[],
        total_events=0,
        forgotten_count=0,
    )

    caplog.set_level("INFO", logger="disco.span")
    step, disp = await loop._driver.drive_step(view, [])

    assert disp == Disp.FALLTHROUGH
    assert step is not None and step.tool_call is not None
    assert step.tool_call.tool_name == "finish"
    assert len(bodies) == 1
    assert [message["role"] for message in bodies[0]["messages"][-2:]] == [
        "assistant",
        "user",
    ]
    repairs = [
        getattr(record, "_fields", {})
        for record in caplog.records
        if getattr(record, "_fields", {}).get("span") == "agent.repair"
    ]
    assert repairs == []
