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
import pytest
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


async def test_preflight_driver_passes_and_caches_success(monkeypatch):
    """A healthy driver passes; a SUCCESS is cached so back-to-back kicks don't
    each pay a live round-trip."""
    from disco.core.inspect import registry

    monkeypatch.setenv("DISCO_INSPECT", "1")
    registry().clear()
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
    trace = registry().snapshot("c1")
    assert trace is not None
    assert [decision["reason"] for decision in trace["routing_decisions"]] == [
        "cached driver preflight success"
    ]


async def test_concurrent_cold_preflights_singleflight_per_model(monkeypatch):
    """A parallel Build wave must not stampede one cold provider endpoint."""
    from disco.core.inspect import registry

    monkeypatch.setenv("DISCO_INSPECT", "1")
    registry().clear()
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(store)

    class _SlowOkRouter:
        def __init__(self):
            self.calls = 0

        async def complete(self, req, *, context=None):
            self.calls += 1
            await asyncio.sleep(0.05)

    router = _SlowOkRouter()
    rt._router_now = lambda **kw: router

    results = await asyncio.gather(*(rt._preflight_driver(f"c{i}") for i in range(6)))

    assert results == [None] * 6
    assert router.calls == 1
    assert {key[0] for key in rt._driver_proven} == {f"c{i}" for i in range(6)}
    # The creator's real DefaultLLMRouter records its own successful route in
    # production (this fake router has no sink). Every waiter receives one exact
    # synthetic record for the shared readiness verdict it consumed.
    assert registry().snapshot("c0") is None
    for index in range(1, 6):
        trace = registry().snapshot(f"c{index}")
        assert trace is not None
        assert trace["spans"] == []
        assert trace["tool_scopes"] == []
        assert [decision["reason"] for decision in trace["routing_decisions"]] == [
            "shared driver preflight success"
        ]


async def test_concurrent_hard_preflight_failure_is_shared_but_not_cached_long_term(monkeypatch):
    from disco.core.inspect import registry

    monkeypatch.setenv("DISCO_INSPECT", "1")
    registry().clear()
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(store)

    class _DeadRouter:
        def __init__(self):
            self.calls = 0

        async def complete(self, req, *, context=None):
            self.calls += 1
            await asyncio.sleep(0.05)
            raise LLMAuthError("invalid api key", provider="x")

    router = _DeadRouter()
    rt._router_now = lambda **kw: router
    rt._DRIVER_PREFLIGHT_SHARED_RESULT_TTL_S = 0.01

    results = await asyncio.gather(*(rt._preflight_driver(f"c{i}") for i in range(6)))

    assert all(result is not None and "rejected the API key" in result for result in results)
    assert router.calls == 1
    for index in range(6):
        trace = registry().snapshot(f"c{index}")
        assert trace is not None
        assert trace["tool_scopes"] == []
        assert trace["spans"] == []
        assert trace["routing_decisions"]
        assert all(
            decision["reason"].startswith("terminal failure:")
            for decision in trace["routing_decisions"]
        )

    # The probe itself took longer than the sharing TTL, but the TTL begins at
    # completion: a trailing caller still consumes the just-finished verdict.
    model_key = next(iter(rt._driver_preflight_inflight))
    completed_task, completed_at = rt._driver_preflight_inflight[model_key]
    assert completed_at is not None
    assert await rt._preflight_driver("trailing") is not None
    assert router.calls == 1
    assert rt._driver_preflight_inflight[model_key] == (completed_task, completed_at)

    # The shared failure is a burst result, not a sticky outage cache.
    rt._driver_preflight_inflight[model_key] = (
        completed_task,
        completed_at - rt._DRIVER_PREFLIGHT_SHARED_RESULT_TTL_S - 1,
    )
    assert await rt._preflight_driver("later") is not None
    assert router.calls == 2


async def test_shared_preflight_failure_expires_without_a_later_caller():
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(store)
    rt._DRIVER_PREFLIGHT_SHARED_RESULT_TTL_S = 0.01

    class _DeadRouter:
        async def complete(self, req, *, context=None):
            raise LLMAuthError("invalid api key", provider="x")

    rt._router_now = lambda **kw: _DeadRouter()
    assert await rt._preflight_driver("c1") is not None
    assert rt._driver_preflight_inflight

    await asyncio.sleep(0.03)
    assert rt._driver_preflight_inflight == {}


async def test_aclose_cancels_and_drains_orphaned_shared_preflight():
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(store)
    started = asyncio.Event()

    class _SlowRouter:
        async def complete(self, req, *, context=None):
            started.set()
            await asyncio.sleep(60)

    rt._router_now = lambda **kw: _SlowRouter()
    waiter = asyncio.create_task(rt._preflight_driver("c1"))
    await started.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    shared_task = next(iter(rt._driver_preflight_inflight.values()))[0]
    assert not shared_task.done()
    await rt.aclose()
    assert shared_task.done() and shared_task.cancelled()
    assert rt._driver_preflight_inflight == {}


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


async def test_preflight_driver_retries_then_proceeds_on_transient_timeout():
    """Resilience: a single pre-flight timeout must NOT hard-fail the run. A driver
    that times out on the first probe but answers on the next is reachable → the
    pre-flight retries and returns None (the run PROCEEDS, no terminal reason)."""
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(store)  # no injected router → real preflight path
    rt._DRIVER_PREFLIGHT_BACKOFF_S = 0.0  # keep the test fast

    class _FlakyRouter:
        def __init__(self):
            self.calls = 0

        async def complete(self, req, *, context=None):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError  # the deadline fired once (slow remote model)
            return  # answered on the retry

    flaky = _FlakyRouter()
    rt._router_now = lambda **kw: flaky
    assert await rt._preflight_driver("c1") is None  # proceeds, not terminal
    assert flaky.calls == 2  # one miss, then a successful re-probe


async def test_preflight_driver_soft_degrades_for_already_working_conversation():
    """An established run that ALREADY proved the driver reachable in THIS
    conversation must not be hard-failed by a later transient blip: a probe that
    now always times out soft-degrades to a warning and PROCEEDS (the real call
    will surface a genuine error if the driver is truly down)."""
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(store)
    rt._DRIVER_PREFLIGHT_BACKOFF_S = 0.0

    class _SwitchRouter:
        def __init__(self):
            self.fail = False
            self.timeout_calls = 0

        async def complete(self, req, *, context=None):
            if self.fail:
                self.timeout_calls += 1
                raise TimeoutError
            return  # healthy

    router = _SwitchRouter()
    rt._router_now = lambda **kw: router

    # First kick: the driver is healthy → THIS (conversation, role, model) is proven.
    assert await rt._preflight_driver("c1") is None
    assert any(k[0] == "c1" and k[1] == ModelRole.AGENT_DRIVER for k in rt._driver_proven)

    # Simulate a later kick after the success cache's TTL has lapsed (30 turns in):
    rt._driver_preflight_ok.clear()  # force a real re-probe
    router.fail = True  # the remote model is now transiently slow on every probe

    # Soft-degrade: a proven conversation PROCEEDS despite the transient timeout.
    assert await rt._preflight_driver("c1") is None
    assert router.timeout_calls >= 1  # the real re-probe WAS attempted


async def test_preflight_driver_never_soft_degrades_hard_auth_failure_after_success():
    """A prior success may mask only a transient blip, never bad credentials."""

    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(store)

    class _SwitchRouter:
        def __init__(self):
            self.reject = False

        async def complete(self, req, *, context=None):
            if self.reject:
                raise LLMAuthError("invalid api key", provider="x")

    router = _SwitchRouter()
    rt._router_now = lambda **kw: router
    assert await rt._preflight_driver("c1") is None

    rt._driver_preflight_ok.clear()
    router.reject = True
    reason = await rt._preflight_driver("c1")

    assert reason is not None
    assert "rejected the API key" in reason


async def test_preflight_driver_still_terminal_when_genuinely_unreachable():
    """The safety stays: a driver that NEVER answers (wrong endpoint/key) on a
    conversation that never proved it still ends in a NAMED terminal reason —
    after the bounded retries, not on the first miss, and not forever."""
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(store)
    rt._DRIVER_PREFLIGHT_BACKOFF_S = 0.0

    class _DeadRouter:
        def __init__(self):
            self.calls = 0

        async def complete(self, req, *, context=None):
            self.calls += 1
            raise TimeoutError  # black hole: never answers

    dead = _DeadRouter()
    rt._router_now = lambda **kw: dead
    reason = await rt._preflight_driver("c2")  # never proven
    assert reason is not None
    assert "timed out" in reason and "unreachable" in reason
    assert dead.calls == rt._DRIVER_PREFLIGHT_ATTEMPTS  # retried, still bounded
    assert not any(k[0] == "c2" for k in rt._driver_proven)


async def test_preflight_soft_degrade_is_per_driver_not_per_conversation():
    """codex regression guard: proven-state is keyed by (conversation, role, model),
    NOT by conversation alone. A conversation that proved driver A (build's
    AGENT_DRIVER) must NOT soft-degrade a DIFFERENT, genuinely-unreachable driver B
    (a DR RAG_ANSWERER on another model) used in the SAME conversation — that call
    still terminates with the named 'unreachable' reason."""

    class _RoleCfg:
        # Distinct model keys per role so the model-keyed success cache (which IS
        # legitimately shared across roles on the same endpoint) does not apply.
        models = {"model-a": object(), "model-b": object()}

        def model_for(self, role, *, override=None):
            if override is not None:
                return override
            return {
                ModelRole.AGENT_DRIVER: "model-a",
                ModelRole.RAG_ANSWERER: "model-b",
            }.get(role, "model-a")

    class _RoleStore:
        def load(self):
            return _RoleCfg()

    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(store)
    rt._DRIVER_PREFLIGHT_BACKOFF_S = 0.0
    rt._config_store = _RoleStore()  # type: ignore[assignment]

    # Driver A (AGENT_DRIVER / model-a) is healthy → proves THAT driver only.
    class _OkRouter:
        async def complete(self, req, *, context=None):
            return

    rt._router_now = lambda **kw: _OkRouter()
    assert await rt._preflight_driver("c1", role=ModelRole.AGENT_DRIVER) is None
    assert ("c1", ModelRole.AGENT_DRIVER, "model-a") in rt._driver_proven

    # Driver B (RAG_ANSWERER / model-b) is genuinely unreachable in the SAME
    # conversation. The conversation is "proven" — but for a DIFFERENT driver — so
    # this STILL terminates (no soft-degrade masking a dead DR driver).
    class _DeadRouter:
        def __init__(self):
            self.calls = 0

        async def complete(self, req, *, context=None):
            self.calls += 1
            raise TimeoutError

    dead = _DeadRouter()
    rt._router_now = lambda **kw: dead
    reason = await rt._preflight_driver("c1", role=ModelRole.RAG_ANSWERER)
    assert reason is not None and "timed out" in reason and "unreachable" in reason
    assert dead.calls == rt._DRIVER_PREFLIGHT_ATTEMPTS  # actually probed, not masked
    assert ("c1", ModelRole.RAG_ANSWERER, "model-b") not in rt._driver_proven


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


async def test_deep_research_kick_bounds_query_rewriter_role():
    """W-35-fu: decompose_query runs via QUERY_REWRITER, NOT RAG_ANSWERER. A
    black-holed QUERY_REWRITER (endpoint accepts the socket, never answers) that
    the RAG_ANSWERER probe didn't cover must be caught by the QUERY_REWRITER
    pre-flight → bounded → StatusEvent(ERROR), NOT a kick stuck RUNNING forever.
    Both roles are probed before RUNNING; the rewriter probe is what fails here.
    """
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(store)  # no injected router → preflight path active
    rt.set_surface("c1", "deep_research")

    probed: list[ModelRole] = []

    async def _by_role(cid, *, override=None, role=ModelRole.AGENT_DRIVER):
        probed.append(role)
        if role is ModelRole.QUERY_REWRITER:
            # Exactly the bounded-timeout reason _preflight_driver returns for a
            # black hole (see test_preflight_driver_is_wall_bounded_on_black_hole).
            return "Driver 'rw' unreachable: no response within 8s (pre-flight timed out)"
        return None  # RAG_ANSWERER healthy

    rt._preflight_driver = _by_role  # type: ignore[assignment]

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
    # The rewriter role WAS probed, and the kick errored on it.
    assert ModelRole.QUERY_REWRITER in probed
    assert statuses and statuses[-1].status == ConversationStatus.ERROR
    assert "timed out" in (statuses[-1].detail or "")
    # NEVER set RUNNING and never decomposed/planned → not stuck (genuinely bounded).
    assert not any(s.status == ConversationStatus.RUNNING for s in statuses)
    assert not any(isinstance(e, PlanEvent) for e in events)


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
    rt = ConversationRuntime(store, router=DefaultLLMRouter(_cfg(), {"fake": _FakeProvider()}))
    assert await rt._preflight_driver("c1") is None


async def test_run_with_persistence_emits_error_and_skips_loop_on_dead_driver():
    """The integration chokepoint: a failed pre-flight emits StatusEvent(ERROR)
    with the named reason AND the loop never runs."""
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(store, router=DefaultLLMRouter(_cfg(), {"fake": _FakeProvider()}))
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
        e for e in events if isinstance(e, StatusEvent) and e.status == ConversationStatus.ERROR
    ]
    assert errs and "unreachable" in (errs[-1].detail or "")


# ---- W-33: encoder pre-flight (Deep Research) -------------------------------


async def test_preflight_encoders_names_unreachable_reranker():
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(store, router=DefaultLLMRouter(_cfg(), {"fake": _FakeProvider()}))
    deps = {"reranker": _dead_reranker(), "nli": _FakeNLI()}
    reason = await rt._dr._preflight_encoders(deps, required=("reranker", "nli"))
    assert reason is not None
    assert "reranker" in reason and "dead:8091" in reason


async def test_preflight_encoders_names_missing_nli():
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(store, router=DefaultLLMRouter(_cfg(), {"fake": _FakeProvider()}))
    # bundled/fake reranker (no probe) present, but NLI is None (declined encoder).
    deps = {"reranker": _FakeNLI(), "nli": None}
    reason = await rt._dr._preflight_encoders(deps, required=("reranker", "nli"))
    assert reason is not None and "nli" in reason


async def test_preflight_encoders_passes_when_all_present():
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(store, router=DefaultLLMRouter(_cfg(), {"fake": _FakeProvider()}))
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
    assert any(isinstance(e, ErrorEvent) and e.code == "deep_research_preflight" for e in events)
    errs = [
        e for e in events if isinstance(e, StatusEvent) and e.status == ConversationStatus.ERROR
    ]
    assert errs and "reranker" in (errs[-1].detail or "")
    # the engine never produced a report
    from disco.core import ReportEvent

    assert not any(isinstance(e, ReportEvent) for e in events)
