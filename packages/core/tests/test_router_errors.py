"""Errors & context-window classification — llm-router-contract.md §10.3.

Cross-tested against the event contract's dependency: the condenser calls
is_context_window_exceeded() on whatever the router raises.
"""

from __future__ import annotations

import pytest
from disco.core import LLMMessage
from disco.core.llm import (
    CapabilityProfile,
    CompletionRequest,
    LLMAuthError,
    LLMContentFiltered,
    LLMContextWindowExceeded,
    LLMTransientError,
    ModelRole,
    is_context_window_exceeded,
)
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


async def test_auth_error_is_terminal_no_retry():
    local = FakeModelProvider("ollama", raises=LLMAuthError("bad key"))
    router, _sink, _ = build_router(local=local)
    with pytest.raises(LLMAuthError):
        await router.complete(_req())
    assert local.calls == 1


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
