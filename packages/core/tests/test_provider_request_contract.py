"""Ordered provider-request goldens across planning, retry, and resume."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable

import httpx
import pytest
from disco.core import (
    CondensationEvent,
    EventSource,
    LLMMessage,
    MessageEvent,
    RuntimeConstraintEvent,
    View,
)
from disco.core.llm import (
    CallContext,
    CapabilityProfile,
    CompletionRequest,
    DefaultLLMRouter,
    ModelEntry,
    ModelRole,
    OperatingMode,
    Requirement,
    RouterConfig,
    ToolSpec,
)
from disco.core.llm.openai_provider import OpenAIProvider
from disco.core.llm.prompts import DriverPrompts

_PLANNING_SHA256 = "77d82107668389e9bed99861f80d3730cc06c1850648352e7106bc13c060a3c4"
_RESUME_SHA256 = "c556039d4df314601fd632a00567f04e4cea51bd11ac77b81f84c3123e4e519b"
_EXPECTED_KEYS = (
    "model",
    "messages",
    "temperature",
    "stream",
    "max_tokens",
    "tools",
    "prompt_cache_key",
)


def _tools() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="read_state",
            description="Read state",
            parameters_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            name="submit_plan",
            description="Submit plan",
            parameters_schema={
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "items": {"type": "string"},
                    }
                },
                "required": ["steps"],
                "additionalProperties": False,
            },
        ),
    ]


def _config() -> RouterConfig:
    return RouterConfig(
        models={
            "driver": ModelEntry(
                model_id="qwen-driver",
                provider="fake",
                context_window=65_536,
                capabilities=frozenset({Requirement.TOOL_CALLING, Requirement.JSON_MODE}),
                family="qwen",
            )
        },
        default_model="driver",
    )


def _success() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "qwen-driver",
            "choices": [
                {
                    "message": {"content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        },
    )


def _router(handler: Callable[[httpx.Request], httpx.Response]) -> DefaultLLMRouter:
    provider = OpenAIProvider(
        "http://fake/v1",
        name="fake",
        transport=httpx.MockTransport(handler),
    )
    return DefaultLLMRouter(
        _config(),
        {"fake": provider},
        prompt_provider=DriverPrompts(),
    )


def _request(
    *,
    mode: OperatingMode,
    messages: list[LLMMessage],
    tools: list[ToolSpec],
    max_tokens: int,
    request_id: str,
) -> CompletionRequest:
    return CompletionRequest(
        profile=CapabilityProfile(
            role=ModelRole.AGENT_DRIVER,
            mode=mode,
        ),
        messages=messages,
        tools=tools,
        max_tokens=max_tokens,
        metadata={"driver_context_window": 65_536},
        request_id=request_id,
    )


def _assert_request(
    raw: bytes,
    *,
    digest: str,
    roles: list[str],
    max_tokens: int,
) -> None:
    payload = json.loads(raw)
    assert hashlib.sha256(raw).hexdigest() == digest
    assert tuple(payload) == _EXPECTED_KEYS
    assert payload["model"] == "qwen-driver"
    assert payload["max_tokens"] == max_tokens
    assert payload["temperature"] == 0.0
    assert payload["stream"] is False
    assert [message["role"] for message in payload["messages"]] == roles
    assert [tool["function"]["name"] for tool in payload["tools"]] == [
        "read_state",
        "submit_plan",
    ]
    assert payload["tools"][0]["function"]["parameters"] == {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "additionalProperties": False,
    }
    assert b"Build Library" not in raw
    assert b"reference-pack" not in raw


def _budget_values(estimate: object) -> tuple[int | None, ...]:
    return tuple(
        getattr(estimate, name)
        for name in (
            "driver_context_window",
            "max_output_tokens",
            "canonical_payload_bytes",
            "messages_json_bytes",
            "tools_json_bytes",
            "message_count",
            "tool_count",
        )
    )


async def test_planning_request_matches_accepted_ordered_bytes() -> None:
    captured: list[tuple[bytes, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(
            (
                request.read(),
                request.headers.get("x-disco-conversation"),
            )
        )
        return _success()

    router = _router(handler)
    request = _request(
        mode=OperatingMode.PLANNING,
        messages=[LLMMessage(role="user", content="Design the migration.")],
        tools=_tools(),
        max_tokens=321,
        request_id="req_11111111111111111111111111111111",
    )
    context = CallContext(conversation_id="golden-plan")

    estimate = router.request_budget_preview(request, context=context)
    response = await router.complete(request, context=context)

    assert estimate is not None
    assert _budget_values(estimate) == (65_536, 321, 11_213, 10_600, 430, 2, 2)
    assert response.routing is not None and response.routing.attempt == 1
    assert captured[0][1] == "golden-plan"
    _assert_request(
        captured[0][0],
        digest=_PLANNING_SHA256,
        roles=["system", "user"],
        max_tokens=321,
    )


def _resumed_messages() -> list[LLMMessage]:
    events = [
        MessageEvent(
            seq=10,
            source=EventSource.USER,
            message=LLMMessage(role="user", content="build me a thing"),
        ),
        RuntimeConstraintEvent(
            seq=20,
            constraint_key="sandbox.host_signal_prohibited",
            guidance="host signals are not permitted",
            alternative="use shell_kill_process",
            capability_generation="process-backend:v1",
        ),
        MessageEvent(
            seq=30,
            source=EventSource.AGENT,
            message=LLMMessage(
                role="assistant",
                content="attempting kill 1234",
            ),
        ),
        CondensationEvent(
            seq=40,
            forgotten_start_seq=10,
            forgotten_end_seq=35,
            summary="[prior work compacted]",
        ),
        MessageEvent(
            seq=50,
            source=EventSource.USER,
            message=LLMMessage(role="user", content="continue"),
        ),
    ]
    return View.of(events).messages


async def test_long_horizon_resume_retries_the_exact_compacted_request() -> None:
    captured: list[tuple[bytes, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(
            (
                request.read(),
                request.headers.get("x-disco-conversation"),
            )
        )
        if len(captured) == 1:
            return httpx.Response(
                503,
                json={
                    "error": {
                        "message": "temporarily unavailable",
                        "type": "server_error",
                    }
                },
            )
        return _success()

    router = _router(handler)
    request = _request(
        mode=OperatingMode.LONG_HORIZON,
        messages=_resumed_messages(),
        tools=_tools(),
        max_tokens=777,
        request_id="req_22222222222222222222222222222222",
    )
    context = CallContext(conversation_id="golden-resume")

    estimate = router.request_budget_preview(request, context=context)
    response = await router.complete(request, context=context)

    assert estimate is not None
    assert _budget_values(estimate) == (65_536, 777, 15_092, 14_479, 430, 5, 2)
    assert response.routing is not None and response.routing.attempt == 2
    assert len(captured) == 2
    assert captured[0] == captured[1]
    assert captured[0][1] == "golden-resume"
    assert captured[0][0].decode().count("sandbox.host_signal_prohibited") == 1
    _assert_request(
        captured[0][0],
        digest=_RESUME_SHA256,
        roles=["system", "user", "user", "user", "user"],
        max_tokens=777,
    )


@pytest.mark.parametrize(
    "mutation",
    ("drop_constraint", "reorder_constraint", "drop_schema", "reorder_tools"),
)
async def test_resume_golden_rejects_constraint_or_tool_drift(
    mutation: str,
) -> None:
    messages = _resumed_messages()
    tools = _tools()
    if mutation == "drop_constraint":
        messages = [
            message
            for message in messages
            if "sandbox.host_signal_prohibited" not in message.content
        ]
    elif mutation == "reorder_constraint":
        index = next(
            i
            for i, message in enumerate(messages)
            if "sandbox.host_signal_prohibited" in message.content
        )
        messages.insert(0, messages.pop(index))
    elif mutation == "drop_schema":
        tools[0] = tools[0].model_copy(update={"parameters_schema": {}})
    else:
        tools.reverse()

    captured: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.read())
        return _success()

    request = _request(
        mode=OperatingMode.LONG_HORIZON,
        messages=messages,
        tools=tools,
        max_tokens=777,
        request_id="req_22222222222222222222222222222222",
    )
    await _router(handler).complete(
        request,
        context=CallContext(conversation_id="golden-resume"),
    )

    assert hashlib.sha256(captured[0]).hexdigest() != _RESUME_SHA256
