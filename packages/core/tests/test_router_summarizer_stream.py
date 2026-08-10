"""Summarizer + streaming — llm-router-contract.md §10.5.

RouterSummarizer routes a SUMMARIZER-role call and returns the model's text
(cross-tested against the event contract's condenser, which must accept it).
stream_complete yields deltas then a terminal chunk whose final equals the
non-streamed result for the same input.
"""

from __future__ import annotations

from disco.core import LLMMessage, NoOpCondenser, View
from disco.core.llm import (
    CallContext,
    CapabilityProfile,
    CompletionRequest,
    DefaultLLMRouter,
    ModelRole,
    RouterSummarizer,
    RoutingDecision,
)
from llm_fakes import FakeModelProvider, build_router, simple_config


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
