"""W-35 driver pre-flight + W-33 encoder pre-flight — fail fast with a NAMED
reason, never a doomed loop / DR run on a dead model or a silently-degraded
encoder.

Hermetic: every network call is faked (a fake router / a MockTransport-backed
encoder client). No real endpoints are touched.
"""

from __future__ import annotations

import asyncio
import time

import httpx
from disco.agent_server import ConversationRuntime
from disco.core import (
    ConversationStatus,
    ErrorEvent,
    EventSource,
    LLMMessage,
    MessageEvent,
    PlanEvent,
    PlanStep,
    SqliteEventStore,
    StatusEvent,
)
from disco.core.llm import (
    CompletionResponse,
    DefaultLLMRouter,
    LLMAuthError,
    LLMTransientError,
    ModelEntry,
    ModelRole,
    RouterConfig,
    TokenUsage,
)
from disco.retrieval.live import TeiReranker

# ---- shared fakes -----------------------------------------------------------


class _FakeProvider:
    name = "fake"

    async def complete(self, req, *, model):
        return CompletionResponse(
            text="ok",
            tool_calls=[],
            usage=TokenUsage(input_tokens=1, output_tokens=1),
            finish_reason="stop",
            model_used=model,
            request_id=req.request_id,
            routing=None,
        )

    async def stream_complete(self, req, *, model):
        yield None  # unused

    def supports(self, requirement, *, model):
        return True


class _FakeNLI:
    def entail(self, premise, hypothesis):
        return "entail"

    def score(self, premise, hypothesis):
        return 0.9


def _cfg() -> RouterConfig:
    return RouterConfig(
        models={"m": ModelEntry(model_id="m", provider="fake", context_window=8192)},
        default_model="m",
    )


def _dead_reranker() -> TeiReranker:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    return TeiReranker("http://dead:8091", transport=httpx.MockTransport(handler))


# ---- W-35: driver pre-flight ------------------------------------------------


async def test_preflight_driver_blocks_on_auth_error():
    """A dead/unauthed driver → a NAMED reason; the caller will NOT start the loop."""
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(store)  # no injected router → preflight runs for real

    class _DeadRouter:
        async def complete(self, req, *, context=None):
            raise LLMAuthError("invalid api key", provider="x")

    rt._router_now = lambda **kw: _DeadRouter()
    reason = await rt._preflight_driver("c1")
    assert reason is not None
    assert "rejected the API key" in reason
    assert "invalid api key" in reason  # the provider's real reason is carried


async def test_preflight_driver_blocks_on_connect_error():
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(store)

    class _UnreachableRouter:
        async def complete(self, req, *, context=None):
            raise LLMTransientError("connection error: refused", provider="x")

    rt._router_now = lambda **kw: _UnreachableRouter()
    reason = await rt._preflight_driver("c1")
    assert reason is not None and "unreachable" in reason


async def test_preflight_driver_passes_and_caches_success():
    """A healthy driver passes; a SUCCESS is cached so back-to-back kicks don't
    each pay a live round-trip."""
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(store)

    class _OkRouter:
        def __init__(self):
            self.calls = 0

        async def complete(self, req, *, context=None):
            self.calls += 1

    ok = _OkRouter()
    rt._router_now = lambda **kw: ok
    assert await rt._preflight_driver("c1") is None
    assert await rt._preflight_driver("c1") is None
    assert ok.calls == 1  # second call served from the short-TTL success cache


async def test_preflight_driver_is_wall_bounded_on_black_hole():
    """P1-1: a black-holed driver (a call that NEVER returns) must FAIL FAST at
    the pre-flight deadline — NOT inherit the router's 5 transient retries × the
    provider's 180s timeout. Proven wall-bounded: the probe returns a NAMED
    timeout reason far under the would-be 5×180s, and nowhere near the fake's
    own (deliberately long) stall."""
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(store)  # no injected router → preflight runs for real

    class _BlackHoleRouter:
        async def complete(self, req, *, context=None):
            await asyncio.sleep(60)  # endpoint accepted but never answers

    rt._router_now = lambda **kw: _BlackHoleRouter()
    rt._DRIVER_PREFLIGHT_TIMEOUT_S = 0.1  # tiny bound for the test

    t0 = time.monotonic()
    reason = await rt._preflight_driver("c1")
    elapsed = time.monotonic() - t0

    assert reason is not None and "timed out" in reason
    assert "unreachable" in reason
    assert elapsed < 5.0  # bounded by the deadline, not the 60s stall / 5×180s


async def test_deep_research_kick_blocks_on_dead_driver():
    """P1-2: the DR INITIAL KICK pre-flights the driver. A dead/unauthed model
    emits StatusEvent(ERROR) and does NOT leave the conversation stuck RUNNING
    (the old bug: decompose set RUNNING then failed with only a MessageEvent)."""
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(store)  # no injected router → preflight active
    rt.set_surface("c1", "deep_research")

    async def _dead(cid, **kw):
        return "Driver 'm' rejected the API key: invalid api key"

    rt._preflight_driver = _dead  # type: ignore[assignment]

    await store.append(
        "c1",
        MessageEvent(
            source=EventSource.USER,
            message=LLMMessage(role="user", content="research X"),
        ),
    )
    await rt._propose_deep_research_plan("c1", await store.get_events("c1"))

    events = await store.get_events("c1")
    statuses = [e for e in events if isinstance(e, StatusEvent)]
    assert statuses and statuses[-1].status == ConversationStatus.ERROR
    assert "rejected the API key" in (statuses[-1].detail or "")
    # NEVER set RUNNING, and never emitted a plan → not stuck.
    assert not any(s.status == ConversationStatus.RUNNING for s in statuses)
    assert not any(isinstance(e, PlanEvent) for e in events)


async def test_deep_research_kick_decompose_failure_goes_error_not_stuck():
    """P1-2: even when the pre-flight passes, a decompose_query failure (e.g. a
    dead QUERY_REWRITER the RAG_ANSWERER probe didn't cover) must take the
    conversation to ERROR — NOT spin forever at RUNNING."""
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(store)
    rt.set_surface("c1", "deep_research")

    async def _ok(cid, **kw):
        return None  # pre-flight passes

    rt._preflight_driver = _ok  # type: ignore[assignment]

    # Make decompose_query blow up: the router its decompose uses raises.
    class _DeadRewriterRouter:
        async def complete(self, req, *, context=None):
            raise LLMAuthError("rewriter key rejected", provider="x")

        def stream_complete(self, req, *, context=None):  # pragma: no cover
            raise LLMAuthError("rewriter key rejected", provider="x")

    rt._router_now = lambda **kw: _DeadRewriterRouter()

    await store.append(
        "c1",
        MessageEvent(
            source=EventSource.USER,
            message=LLMMessage(role="user", content="research X"),
        ),
    )
    await rt._propose_deep_research_plan("c1", await store.get_events("c1"))

    events = await store.get_events("c1")
    statuses = [e for e in events if isinstance(e, StatusEvent)]
    assert statuses and statuses[-1].status == ConversationStatus.ERROR
    assert "decomposition failed" in (statuses[-1].detail or "").lower()


async def test_deep_research_preflights_rag_answerer_role():
    """P1-3: the DR answer stream pre-flights the role it GENERATES with —
    RAG_ANSWERER — not AGENT_DRIVER (which DR generation never uses)."""
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(
        store,
        router=DefaultLLMRouter(_cfg(), {"fake": _FakeProvider()}),
        research_providers={
            "search": object(),
            "extraction": object(),
            "reranker": _FakeNLI(),
            "embedder": None,
            "nli": _FakeNLI(),
        },
    )
    probed: dict[str, ModelRole] = {}

    async def _capture(cid, *, override=None, role=ModelRole.AGENT_DRIVER):
        probed["role"] = role
        return "stop here"  # short-circuit before the real stream

    rt._preflight_driver = _capture  # type: ignore[assignment]
    frames = [f async for f in rt.research_stream("q")]

    assert probed.get("role") == ModelRole.RAG_ANSWERER
    assert frames and frames[0]["type"] == "error"


async def test_preflight_driver_skipped_when_router_injected():
    """A pinned (test/dev) router has no real endpoint — preflight is a no-op."""
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(
        store, router=DefaultLLMRouter(_cfg(), {"fake": _FakeProvider()})
    )
    assert await rt._preflight_driver("c1") is None


async def test_run_with_persistence_emits_error_and_skips_loop_on_dead_driver():
    """The integration chokepoint: a failed pre-flight emits StatusEvent(ERROR)
    with the named reason AND the loop never runs."""
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(
        store, router=DefaultLLMRouter(_cfg(), {"fake": _FakeProvider()})
    )
    rt.set_surface("c1", "build")

    async def _fail(cid, **kw):
        return "Driver 'm' unreachable: connection error: refused"

    rt._preflight_driver = _fail  # type: ignore[assignment]

    class _Loop:
        def __init__(self):
            self.ran = False

        async def run(self):
            self.ran = True

    loop = _Loop()
    await rt._run_with_persistence("c1", loop)
    assert loop.ran is False  # the doomed loop never started

    events = await store.get_events("c1")
    errs = [
        e
        for e in events
        if isinstance(e, StatusEvent) and e.status == ConversationStatus.ERROR
    ]
    assert errs and "unreachable" in (errs[-1].detail or "")


# ---- W-33: encoder pre-flight (Deep Research) -------------------------------


async def test_preflight_encoders_names_unreachable_reranker():
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(
        store, router=DefaultLLMRouter(_cfg(), {"fake": _FakeProvider()})
    )
    deps = {"reranker": _dead_reranker(), "nli": _FakeNLI()}
    reason = await rt._dr._preflight_encoders(deps, required=("reranker", "nli"))
    assert reason is not None
    assert "reranker" in reason and "dead:8091" in reason


async def test_preflight_encoders_names_missing_nli():
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(
        store, router=DefaultLLMRouter(_cfg(), {"fake": _FakeProvider()})
    )
    # bundled/fake reranker (no probe) present, but NLI is None (declined encoder).
    deps = {"reranker": _FakeNLI(), "nli": None}
    reason = await rt._dr._preflight_encoders(deps, required=("reranker", "nli"))
    assert reason is not None and "nli" in reason


async def test_preflight_encoders_passes_when_all_present():
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(
        store, router=DefaultLLMRouter(_cfg(), {"fake": _FakeProvider()})
    )
    # Bundled encoders have no probe() → treated as present (raise on RAM fail).
    deps = {"reranker": _FakeNLI(), "nli": _FakeNLI()}
    assert await rt._dr._preflight_encoders(deps, required=("reranker", "nli")) is None


async def test_research_stream_emits_named_error_on_dead_encoder():
    """research_stream pre-flights the required encoders and emits ONE error frame
    (no doomed stream) when the reranker is unreachable."""
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(
        store,
        router=DefaultLLMRouter(_cfg(), {"fake": _FakeProvider()}),
        research_providers={
            "search": object(),
            "extraction": object(),
            "reranker": _dead_reranker(),
            "embedder": None,
            "nli": _FakeNLI(),
        },
    )
    frames = [f async for f in rt.research_stream("q")]
    assert len(frames) == 1
    assert frames[0]["type"] == "error"
    assert "reranker" in frames[0]["message"]


async def test_execute_deep_research_blocks_on_dead_encoder():
    """The DR report engine does NOT start on a dead required encoder: an
    ErrorEvent + StatusEvent(ERROR) name the reason instead of a wrong report."""
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(
        store,
        router=DefaultLLMRouter(_cfg(), {"fake": _FakeProvider()}),
        research_providers={
            "search": object(),
            "extraction": object(),
            "reranker": _dead_reranker(),
            "embedder": None,
            "nli": _FakeNLI(),
        },
    )
    await store.append(
        "c1",
        MessageEvent(
            source=EventSource.USER,
            message=LLMMessage(role="user", content="research X"),
        ),
    )
    plan = PlanEvent(summary="plan", steps=[PlanStep(title="q1")])
    await rt._dr._execute_deep_research("c1", plan)

    events = await store.get_events("c1")
    assert any(
        isinstance(e, ErrorEvent) and e.code == "deep_research_preflight"
        for e in events
    )
    errs = [
        e
        for e in events
        if isinstance(e, StatusEvent) and e.status == ConversationStatus.ERROR
    ]
    assert errs and "reranker" in (errs[-1].detail or "")
    # the engine never produced a report
    from disco.core import ReportEvent

    assert not any(isinstance(e, ReportEvent) for e in events)
