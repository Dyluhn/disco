"""Errors & context-window classification — llm-router-contract.md §10.3.

Cross-tested against the event contract's dependency: the condenser calls
is_context_window_exceeded() on whatever the router raises.
"""

from __future__ import annotations

import asyncio

import pytest
from disco.core import LLMMessage
from disco.core.inspect import registry
from disco.core.llm import (
    CallContext,
    CapabilityProfile,
    CompletionRequest,
    LLMAuthError,
    LLMContentFiltered,
    LLMContextWindowExceeded,
    LLMTransientError,
    ModelRole,
    is_context_window_exceeded,
)
from disco.core.llm import routing as routing_module
from llm_fakes import FakeModelProvider, build_router


def _req(role=ModelRole.AGENT_DRIVER):
    return CompletionRequest(
        profile=CapabilityProfile(role=role),
        messages=[LLMMessage(role="user", content="x")],
    )


async def test_context_window_error_propagates_and_is_classified():
    """The condenser depends on this: the raised error classifies True."""
    local = FakeModelProvider("ollama", raises=LLMContextWindowExceeded("ctx too big"))
    router, _sink, _ = build_router(local=local)
    with pytest.raises(LLMContextWindowExceeded) as excinfo:
        await router.complete(_req())
    # The event contract's hook (event-state-contract §5.3) must recognize it.
    assert is_context_window_exceeded(excinfo.value) is True


async def test_context_window_error_is_terminal_no_retry():
    local = FakeModelProvider("ollama", raises=LLMContextWindowExceeded("ctx"))
    router, _sink, _ = build_router(local=local)
    with pytest.raises(LLMContextWindowExceeded):
        await router.complete(_req())
    assert local.calls == 1  # no retry on a terminal error


async def test_auth_error_retries_once_then_terminal(monkeypatch):
    monkeypatch.setattr(routing_module, "_AUTH_RETRY_DELAY_S", 0)
    local = FakeModelProvider("ollama", raises=LLMAuthError("bad key"))
    router, _sink, _ = build_router(local=local)
    with pytest.raises(LLMAuthError) as excinfo:
        await router.complete(_req())
    assert str(excinfo.value) == "bad key"
    assert local.calls == 2


async def test_content_filter_is_terminal_and_typed():
    local = FakeModelProvider("ollama", raises=LLMContentFiltered("refused"))
    router, _sink, _ = build_router(local=local)
    with pytest.raises(LLMContentFiltered):
        await router.complete(_req())
    assert local.calls == 1


async def test_transient_on_local_only_role_retries_then_raises():
    """A local-only role (no overflow) retries the transient error up to the
    attempt cap, then raises — there is no escalation path."""
    local = FakeModelProvider("ollama", raises=LLMTransientError("flaky"))
    router, _sink, _ = build_router(local=local)
    with pytest.raises(LLMTransientError):
        await router.complete(_req(role=ModelRole.RAG_ANSWERER))  # local-only
    assert local.calls > 1  # retried


async def test_transient_then_success_when_provider_recovers():
    """raises_times=1 → fails once, succeeds on retry (local-only role)."""
    local = FakeModelProvider(
        "ollama", raises=LLMTransientError("blip"), raises_times=1, text="recovered"
    )
    router, _sink, _ = build_router(local=local)
    resp = await router.complete(_req(role=ModelRole.RAG_ANSWERER))
    assert resp.text == "recovered"
    assert resp.routing.attempt == 2


async def test_inspect_records_each_transient_provider_attempt_without_model_content(monkeypatch):
    monkeypatch.setenv("DISCO_INSPECT", "1")
    registry().clear()
    local = FakeModelProvider(
        "ollama", raises=LLMTransientError("provider detail must not be recorded"), raises_times=2
    )
    router, _sink, _ = build_router(local=local)

    response = await router.complete(
        _req(role=ModelRole.RAG_ANSWERER),
        context=CallContext(conversation_id="attempt-success"),
    )

    assert response.text == "ok"
    snapshot = registry().snapshot("attempt-success")
    assert snapshot is not None
    rows = snapshot["model_attempts"]
    assert [row["outcome"] for row in rows] == [
        "started",
        "error",
        "started",
        "error",
        "started",
        "success",
    ]
    assert [row["attempt"] for row in rows] == [1, 1, 2, 2, 3, 3]
    assert [row["call_ordinal"] for row in rows] == [1, 1, 2, 2, 3, 3]
    assert [row["retry_scheduled"] for row in rows] == [None, True, None, True, None, False]
    assert all(row["stage"] == "router" for row in rows)
    assert all(row["latency_ms"] is None or row["latency_ms"] >= 0 for row in rows)
    assert all("provider detail" not in str(row) for row in rows)
    assert all("messages" not in row and "request" not in row for row in rows)
    assert snapshot["model_io"] == []


async def test_inspect_records_terminal_provider_attempt_error(monkeypatch):
    monkeypatch.setenv("DISCO_INSPECT", "1")
    registry().clear()
    local = FakeModelProvider("ollama", raises=LLMTransientError("do not retain"))
    router, _sink, _ = build_router(local=local)

    with pytest.raises(LLMTransientError):
        await router.complete(
            _req(role=ModelRole.RAG_ANSWERER),
            context=CallContext(conversation_id="attempt-terminal"),
        )

    snapshot = registry().snapshot("attempt-terminal")
    assert snapshot is not None
    rows = snapshot["model_attempts"]
    assert len(rows) == 10  # five actual provider calls, start + terminal each
    assert rows[-1]["outcome"] == "error"
    assert rows[-1]["error_class"] == "LLMTransientError"
    assert rows[-1]["retry_scheduled"] is False
    assert [row["call_ordinal"] for row in rows] == [1, 1, 2, 2, 3, 3, 4, 4, 5, 5]
    assert snapshot["model_io"] == []


async def test_inspect_records_cancellation_without_retry(monkeypatch):
    monkeypatch.setenv("DISCO_INSPECT", "1")
    registry().clear()
    local = FakeModelProvider("ollama", raises=asyncio.CancelledError())
    router, _sink, _ = build_router(local=local)

    with pytest.raises(asyncio.CancelledError):
        await router.complete(
            _req(role=ModelRole.RAG_ANSWERER),
            context=CallContext(conversation_id="attempt-cancelled"),
        )

    rows = registry().snapshot("attempt-cancelled")["model_attempts"]
    assert [row["outcome"] for row in rows] == ["started", "cancelled"]
    assert rows[-1]["error_class"] == "CancelledError"
    assert rows[-1]["retry_scheduled"] is False
