from __future__ import annotations

import pytest

from disco.agent_server.ai_chat_host_service import (
    AI_CHAT_SERVICE_NAME,
    estimate_ai_chat_usage,
    read_ai_chat_usage,
)
from disco.core.host_services import (
    HostServiceContext,
    HostServicePayloadError,
    call_host_service,
)


@pytest.mark.asyncio
async def test_ai_chat_is_bounded_normalized_and_tool_free() -> None:
    captured: dict[str, object] = {}

    async def complete(payload: dict[str, object]) -> dict[str, object]:
        captured.update(payload)
        return {
            "ok": True,
            "text": "hello",
            "finish_reason": "stop",
            "usage": {"input_tokens": 9, "output_tokens": 2},
        }

    result = await call_host_service(
        AI_CHAT_SERVICE_NAME,
        {"messages": [{"role": "user", "content": "Hi"}]},
        HostServiceContext(ai_chat_complete=complete),
    )

    assert result["text"] == "hello"
    assert captured == {
        "messages": [{"role": "user", "content": "Hi"}],
        "max_tokens": 512,
        "temperature": 0.0,
    }
    assert "model" not in captured
    assert "tools" not in captured


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"messages": [{"role": "assistant", "content": "no user"}]},
        {"messages": [{"role": "tool", "content": "escape"}]},
        {"messages": [{"role": "user", "content": "x"}], "model": "attacker"},
        {"messages": [{"role": "user", "content": "x"}], "max_tokens": 4097},
        {"messages": [{"role": "user", "content": "x"}], "temperature": 2.1},
        {"messages": [{"role": "user", "content": "x" * 8193}]},
    ],
)
async def test_ai_chat_rejects_unbounded_or_capability_broadening_payloads(
    payload: dict[str, object],
) -> None:
    with pytest.raises(HostServicePayloadError):
        await call_host_service(AI_CHAT_SERVICE_NAME, payload, HostServiceContext())


@pytest.mark.asyncio
async def test_ai_chat_fails_safely_without_runtime() -> None:
    result = await call_host_service(
        AI_CHAT_SERVICE_NAME,
        {"messages": [{"role": "user", "content": "Hi"}]},
        HostServiceContext(),
    )
    assert result == {"ok": False, "error": "ai_unavailable"}


def test_ai_chat_metering_reserves_bounded_output_and_reads_exact_usage() -> None:
    estimate = estimate_ai_chat_usage(
        {"messages": [{"role": "user", "content": "hello"}], "max_tokens": 73}
    )
    assert estimate.input_tokens >= len("hello")
    assert estimate.output_tokens == 73
    actual = read_ai_chat_usage({"usage": {"input_tokens": 11, "output_tokens": 7}})
    assert actual.input_tokens == 11
    assert actual.output_tokens == 7


@pytest.mark.parametrize(
    "result",
    [
        {},
        {"usage": None},
        {"usage": {"input_tokens": -1, "output_tokens": 1}},
        {"usage": {"input_tokens": 1, "output_tokens": True}},
    ],
)
def test_ai_chat_usage_reader_rejects_malformed_results(result: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        read_ai_chat_usage(result)
