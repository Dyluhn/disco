"""Fault / chaos injection (plan Phase 4) — inject the REAL typed errors at the
REAL seams and assert the EXISTING resilience fires: the router's same-model
retry, the sandbox session's _recreate, and the grounding pipeline's graceful
degradation to an honest empty answer.

These differ from the per-package unit tests (`test_router_errors.py`,
`test_agent_tools.py`) by injecting through the same `ModelProvider` /
`SandboxService` boundaries the harness composition root uses — so a regression
that wires AROUND resilience (swaps a provider, drops a check) is caught here.

Run: uv run pytest development/harness/tests/test_faults.py
"""

from __future__ import annotations

import pytest
from disco.core import LLMMessage
from disco.core.llm import (
    CapabilityProfile,
    CompletionRequest,
    LLMAuthError,
    LLMContentFiltered,
    LLMTransientError,
    ModelRole,
)
from disco.core.llm.routing import _MAX_ATTEMPTS
from disco.retrieval.grounding import GroundingPipeline
from disco.retrieval.models import RetrievalResult
from disco.tools.sandbox import SandboxError, SandboxSession, SandboxSpec
from harness.faults import FaultyProvider, FaultySandboxService, build_faulty_router


def _req(role=ModelRole.RAG_ANSWERER) -> CompletionRequest:
    # a local-only role: a transient retries the SAME model, never escalates.
    return CompletionRequest(
        profile=CapabilityProfile(role=role),
        messages=[LLMMessage(role="user", content="x")],
    )


# ---- router: transient retries, terminal propagates -------------------------


async def test_router_retries_transient_then_succeeds():
    """One transient blip → the router retries the same model and the second call
    succeeds. `routing.attempt == 2` is the OBSERVABLE proof a retry happened."""
    provider = FaultyProvider(errors=[LLMTransientError("429 blip")], text="recovered")
    router, _sink = build_faulty_router(provider)

    resp = await router.complete(_req())

    assert resp.text == "recovered"
    assert provider.calls == 2  # failed once, retried once
    assert resp.routing.attempt == 2  # the retry is recorded, not hidden


async def test_router_exhausts_transient_and_raises():
    """A provider that is transient on EVERY call retries up to the cap, then raises
    the transient (no escalation on a local-only role) — it never wedges or hangs."""
    provider = FaultyProvider(errors=[LLMTransientError("down")] * (_MAX_ATTEMPTS + 2))
    router, _sink = build_faulty_router(provider)

    with pytest.raises(LLMTransientError):
        await router.complete(_req())

    assert provider.calls == _MAX_ATTEMPTS  # tried exactly the cap, no more


async def test_router_auth_error_retries_once_then_succeeds():
    """A single auth race (for example a just-rotated token) gets one retry."""
    provider = FaultyProvider(errors=[LLMAuthError("stale token")], text="recovered")
    router, _sink = build_faulty_router(provider)

    response = await router.complete(_req())

    assert response.text == "recovered"
    assert provider.calls == 2


async def test_router_repeated_auth_error_is_terminal_after_one_retry():
    """Persistent bad credentials stop after the one bounded auth retry."""
    provider = FaultyProvider(errors=[LLMAuthError("bad key"), LLMAuthError("bad key")])
    router, _sink = build_faulty_router(provider)

    with pytest.raises(LLMAuthError):
        await router.complete(_req())

    assert provider.calls == 2


async def test_router_content_filter_is_terminal_no_retry():
    provider = FaultyProvider(errors=[LLMContentFiltered("refused")])
    router, _sink = build_faulty_router(provider)

    with pytest.raises(LLMContentFiltered):
        await router.complete(_req())

    assert provider.calls == 1


async def test_router_stream_path_also_retries_transient():
    """The watch-it-write path uses stream_complete — its resilience must match the
    non-streaming path, or a streamed build would crash where a sync one recovers."""
    provider = FaultyProvider(errors=[LLMTransientError("blip")], text="streamed-ok")
    router, _sink = build_faulty_router(provider)

    chunks = [c async for c in router.stream_complete(_req())]

    assert chunks[-1].done and chunks[-1].final is not None
    assert chunks[-1].final.text == "streamed-ok"
    assert provider.calls == 2


async def test_router_does_not_retry_a_transient_that_fires_mid_stream():
    """SAFETY GUARD: once a chunk has been emitted downstream (watch-it-write body in
    the UI), a transient must PROPAGATE, not retry — replaying would duplicate the
    streamed content. The router restarts a stream ONLY before the first chunk."""
    provider = FaultyProvider(
        text="abcdef", stream_raise_after_chunk=LLMTransientError("died mid-stream")
    )
    router, _sink = build_faulty_router(provider)

    got = []
    with pytest.raises(LLMTransientError):
        async for c in router.stream_complete(_req()):
            got.append(c)

    assert provider.calls == 1  # NOT retried — the partial stream was not replayed
    assert [c.delta_text for c in got] == ["abc"]  # the one partial chunk, exactly once


# ---- sandbox: a mid-session death heals, never wedges -----------------------


async def test_sandbox_session_recreates_on_mid_session_death():
    """The first box dies after its first exec. The session must: catch the typed
    death, tear down the corpse, create a fresh box (generation++), surface a CLEAN
    `SandboxError` ('retry the action') — NOT the raw `SandboxUnavailableError` and
    NOT a wedge — and the NEXT call lands on the healthy box."""
    svc = FaultySandboxService(first_dies_after=1)
    session = SandboxSession(svc, SandboxSpec())

    first = await session.exec_shell("step 1", timeout_s=5)
    assert "inst-1" in first.stdout and session.generation == 1

    with pytest.raises(SandboxError) as ei:
        await session.exec_shell("step 2", timeout_s=5)  # the box dies here
    assert "re-created" in str(ei.value)
    assert session.generation == 2 and len(svc.created) == 2
    assert svc.created[0].destroyed is True  # the dead box was torn down

    third = await session.exec_shell("step 3", timeout_s=5)
    assert "inst-2" in third.stdout  # fresh box, session healed


# ---- grounding: extraction blocked everything → honest empty, never a crash -


class _NoNLI:
    """An NLI stub. Never reached when there are no passages (no claims to verify),
    but the pipeline requires one."""

    def entail(self, premise: str, hypothesis: str) -> str:
        return "neutral"

    def score(self, premise: str, hypothesis: str) -> float:
        return 0.0


async def test_grounding_degrades_to_empty_answer_when_all_extraction_blocked():
    """The `passages=[]` failure class that bit this session: every source blocked /
    paywalled → retrieval yields zero passages. The pipeline must NOT crash; it
    produces an HONEST empty answer (no claims, no fabricated prose) that the eval
    gate then refuses to pass. A model that returns text anyway has nothing to cite,
    so `extract_claims` finds nothing and the answer stays empty."""
    provider = FaultyProvider(text="I cannot answer without sources.")
    router, _sink = build_faulty_router(provider)
    pipeline = GroundingPipeline(router, _NoNLI())

    empty = RetrievalResult(passages=[], all_hits=[], extracted=[], issued_queries=[])
    answer = await pipeline.answer("anything", empty)

    assert answer.answer_markdown == ""  # honest empty, not fabricated
    assert answer.claims == []
    assert answer.passages == []
