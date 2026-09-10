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
    assert len(rows) == 16  # eight actual provider calls, start + terminal each
    assert rows[-1]["outcome"] == "error"
    assert rows[-1]["error_class"] == "LLMTransientError"
    assert rows[-1]["retry_scheduled"] is False
    assert [row["call_ordinal"] for row in rows] == [1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7, 8, 8]
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


# --- jittered retry backoff --------------------------------------------------
#
# The conftest collapses this schedule to zero suite-wide so retry tests do not
# spend real wall-clock asleep. These tests restore it and assert the REQUESTED
# delays through a patched `asyncio.sleep`, so nothing here actually sleeps.


def test_retry_backoff_schedule_is_exponential_capped_and_jittered():
    from disco.core.llm._router_backoff import retry_backoff_delay_s

    for attempt, base in ((1, 2.0), (2, 4.0), (3, 8.0), (4, 16.0), (5, 32.0), (6, 45.0), (9, 45.0)):
        delay = retry_backoff_delay_s(attempt)
        assert base * 0.75 <= delay <= base * 1.25, (attempt, delay)

    # Jitter is real: concurrent research lanes must not resynchronize onto the
    # same retry instant.
    assert len({retry_backoff_delay_s(3) for _ in range(30)}) > 1
    # A non-positive base disables the wait outright (how the suite stays fast).
    assert retry_backoff_delay_s(4, base_s=0.0) == 0.0


def _capture_sleeps(monkeypatch) -> list[float]:
    monkeypatch.setattr(routing_module, "_RETRY_BACKOFF_BASE_S", 2.0)
    slept: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    return slept


async def test_transient_retries_wait_out_a_backoff_and_never_after_the_last(monkeypatch):
    slept = _capture_sleeps(monkeypatch)
    local = FakeModelProvider("ollama", raises=LLMTransientError("flaky"))
    router, _sink, _ = build_router(local=local)

    with pytest.raises(LLMTransientError):
        await router.complete(_req(role=ModelRole.RAG_ANSWERER))

    assert local.calls == 8  # _MAX_ATTEMPTS
    assert len(slept) == 7  # seven gaps between eight attempts; none after the last
    for delay, base in zip(slept, (2.0, 4.0, 8.0, 16.0, 32.0, 45.0, 45.0), strict=True):
        assert base * 0.75 <= delay <= base * 1.25
    # A two-minute provider burst (observed 2026-09-04: HTTP 400 for ~120 s)
    # must be ridden out, not turned into a lost run: the ladder's nominal
    # waits alone exceed it.
    assert sum((2.0, 4.0, 8.0, 16.0, 32.0, 45.0, 45.0)) > 120.0


async def test_a_recovering_provider_pays_only_for_the_retries_it_needed(monkeypatch):
    slept = _capture_sleeps(monkeypatch)
    local = FakeModelProvider(
        "ollama", raises=LLMTransientError("blip"), raises_times=1, text="recovered"
    )
    router, _sink, _ = build_router(local=local)

    resp = await router.complete(_req(role=ModelRole.RAG_ANSWERER))

    assert resp.text == "recovered"
    assert len(slept) == 1 and 1.5 <= slept[0] <= 2.5


async def test_backoff_keeps_the_attempt_events_correct(monkeypatch):
    monkeypatch.setenv("DISCO_INSPECT", "1")
    registry().clear()
    slept = _capture_sleeps(monkeypatch)
    local = FakeModelProvider("ollama", raises=LLMTransientError("blip"), raises_times=2)
    router, _sink, _ = build_router(local=local)

    await router.complete(
        _req(role=ModelRole.RAG_ANSWERER),
        context=CallContext(conversation_id="attempt-backoff"),
    )

    rows = registry().snapshot("attempt-backoff")["model_attempts"]
    # A scheduled retry still records retry_scheduled=True, and the sleep sits
    # between the recorded failure and the next attempt's start.
    assert [row["retry_scheduled"] for row in rows] == [None, True, None, True, None, False]
    assert len(slept) == 2
