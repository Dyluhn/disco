"""Summarizer + streaming — llm-router-contract.md §10.5.

RouterSummarizer routes a SUMMARIZER-role call and returns the model's text
(cross-tested against the event contract's condenser, which must accept it).
stream_complete yields deltas then a terminal chunk whose final equals the
non-streamed result for the same input.
"""

from __future__ import annotations

import asyncio

import pytest
from disco.core import LLMMessage, NoOpCondenser, View
from disco.core.inspect import registry
from disco.core.llm import (
    CallContext,
    CapabilityProfile,
    CompletionRequest,
    DefaultLLMRouter,
    LLMTransientError,
    ModelRole,
    RouterSummarizer,
    RoutingDecision,
    StreamChunk,
)
from llm_fakes import FakeModelProvider, build_router, simple_config


class _NoFinalProvider(FakeModelProvider):
    async def stream_complete(self, req, *, model):
        self.calls += 1
        self.seen_requests.append(req)
        yield StreamChunk(delta_text="partial")


async def test_summarizer_routes_summarizer_role_and_returns_text():
    """Async `summarize()` (v1.2 §5.2) routes a SUMMARIZER call."""
    summ_provider = FakeModelProvider("ollama", text="THE SUMMARY")
    providers = {"ollama": summ_provider, "openrouter": FakeModelProvider("openrouter")}
    router = DefaultLLMRouter(simple_config(), providers)
    summarizer = RouterSummarizer(router)

    out = await summarizer.summarize([LLMMessage(role="user", content="lots of history")])
    assert out == "THE SUMMARY"
    # It routed as the SUMMARIZER role (local-only, cheap model).
    assert summ_provider.seen_requests[0].profile.role == ModelRole.SUMMARIZER


async def test_summarizer_satisfies_the_event_contracts_summarizer_protocol():
    """Structural conformance: the (async) condenser seam accepts RouterSummarizer."""
    router, _sink, _ = build_router()
    summarizer = RouterSummarizer(router)
    # NoOpCondenser.condense (async) takes a Summarizer; this must wire cleanly.
    cond = NoOpCondenser()
    view = View.of([])
    assert await cond.condense([], view, summarizer=summarizer) is None  # no-op, accepts it


async def test_stream_deltas_reassemble_and_final_matches_complete():
    text = "the quick brown fox jumps"
    local = FakeModelProvider("ollama", text=text, cost_usd=0.0)
    providers = {"ollama": local, "openrouter": FakeModelProvider("openrouter")}
    router = DefaultLLMRouter(simple_config(), providers)
    req = CompletionRequest(
        profile=CapabilityProfile(role=ModelRole.AGENT_DRIVER),
        messages=[LLMMessage(role="user", content="go")],
        stream=True,
    )

    deltas: list[str] = []
    final = None
    async for chunk in router.stream_complete(req):
        if chunk.done:
            final = chunk.final
        else:
            deltas.append(chunk.delta_text)

    assert "".join(deltas) == text
    assert final is not None
    assert final.text == text
    assert final.routing is not None  # router attached the decision (RT1)

    # Final equals a non-streamed complete() for the same input (content-wise).
    nonstream = await router.complete(
        CompletionRequest(
            profile=CapabilityProfile(role=ModelRole.AGENT_DRIVER),
            messages=[LLMMessage(role="user", content="go")],
        )
    )
    assert final.text == nonstream.text
    assert final.usage == nonstream.usage
    assert final.model_used == nonstream.model_used


async def test_stream_success_observer_waits_for_the_terminal_final():
    observed: list[tuple[str, RoutingDecision, CallContext]] = []
    context = CallContext(conversation_id="conv-stream")
    providers = {
        "ollama": FakeModelProvider("ollama", text="done"),
        "openrouter": FakeModelProvider("openrouter"),
    }
    router = DefaultLLMRouter(
        simple_config(),
        providers,
    )
    router._bind_success_observer(
        lambda key, decision, ctx: observed.append((key, decision, ctx))
    )
    request = CompletionRequest(
        profile=CapabilityProfile(role=ModelRole.AGENT_DRIVER),
        messages=[LLMMessage(role="user", content="go")],
        stream=True,
    )

    chunks = []
    async for chunk in router.stream_complete(request, context=context):
        chunks.append(chunk)
        if not chunk.done:
            assert observed == []

    final = chunks[-1].final
    assert final is not None and final.routing is not None
    assert observed == [("local", final.routing, context)]


async def test_stream_inspect_records_transient_attempts_and_success(monkeypatch):
    monkeypatch.setenv("DISCO_INSPECT", "1")
    registry().clear()
    local = FakeModelProvider(
        "ollama", text="streamed", raises=LLMTransientError("hidden"), raises_times=2
    )
    router = DefaultLLMRouter(
        simple_config(),
        {"ollama": local, "openrouter": FakeModelProvider("openrouter")},
    )
    request = CompletionRequest(
        profile=CapabilityProfile(role=ModelRole.AGENT_DRIVER),
        messages=[LLMMessage(role="user", content="go")],
        stream=True,
    )

    chunks = [
        chunk
        async for chunk in router.stream_complete(
            request, context=CallContext(conversation_id="stream-attempts")
        )
    ]

    assert chunks[-1].done and chunks[-1].final is not None
    snapshot = registry().snapshot("stream-attempts")
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
    assert [row["retry_scheduled"] for row in rows] == [None, True, None, True, None, False]
    assert [row["call_ordinal"] for row in rows] == [1, 1, 2, 2, 3, 3]
    assert all(row["error_class"] in (None, "LLMTransientError") for row in rows)


async def test_stream_without_final_is_not_traced_as_success(monkeypatch):
    monkeypatch.setenv("DISCO_INSPECT", "1")
    registry().clear()
    local = _NoFinalProvider("ollama")
    router = DefaultLLMRouter(
        simple_config(),
        {"ollama": local, "openrouter": FakeModelProvider("openrouter")},
    )
    request = CompletionRequest(
        profile=CapabilityProfile(role=ModelRole.AGENT_DRIVER),
        messages=[LLMMessage(role="user", content="go")],
        stream=True,
    )

    chunks = [
        chunk
        async for chunk in router.stream_complete(
            request, context=CallContext(conversation_id="stream-incomplete")
        )
    ]

    assert [chunk.delta_text for chunk in chunks] == ["partial"]
    assert registry().snapshot("stream-incomplete")["model_attempts"][-1]["outcome"] == "incomplete"


async def test_stream_cancellation_is_observable_and_propagates(monkeypatch):
    monkeypatch.setenv("DISCO_INSPECT", "1")
    registry().clear()
    local = FakeModelProvider("ollama", raises=asyncio.CancelledError())
    router, _sink, _ = build_router(local=local)
    request = CompletionRequest(
        profile=CapabilityProfile(role=ModelRole.AGENT_DRIVER),
        messages=[LLMMessage(role="user", content="go")],
        stream=True,
    )

    with pytest.raises(asyncio.CancelledError):
        async for _chunk in router.stream_complete(
            request, context=CallContext(conversation_id="stream-cancelled")
        ):
            pass

    rows = registry().snapshot("stream-cancelled")["model_attempts"]
    assert [row["outcome"] for row in rows] == ["started", "cancelled"]
    assert rows[-1]["error_class"] == "CancelledError"
