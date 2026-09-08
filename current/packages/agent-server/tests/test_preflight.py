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
    SqliteEventStore,
    StatusEvent,
)
from disco.core.llm import (
    CallContext,
    CapabilityProfile,
    CompletionRequest,
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

    rt.drivers.router = lambda **kw: _DeadRouter()
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

    rt.drivers.router = lambda **kw: _UnreachableRouter()
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
    rt.drivers.router = lambda **kw: ok
    assert await rt._preflight_driver("c1") is None
    assert await rt._preflight_driver("c1") is None
    assert ok.calls == 1  # second call served from the short-TTL success cache
    trace = registry().snapshot("c1")
    assert trace is not None
    assert [decision["reason"] for decision in trace["routing_decisions"]] == [
        "cached driver preflight success"
    ]


async def test_real_completion_refreshes_shared_driver_readiness(monkeypatch):
    import disco.agent_server.driver_runtime as driver_runtime_module

    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(store)

    class _ConfigStore:
        def load(self):
            return _cfg()

    provider = _FakeProvider()
    monkeypatch.setattr(
        driver_runtime_module,
        "build_providers",
        lambda *_args, **_kwargs: {"fake": provider},
    )
    rt.drivers._config_store = _ConfigStore()  # type: ignore[assignment]
    router = rt.drivers.router(conversation_id="working")
    request = CompletionRequest(
        profile=CapabilityProfile(role=ModelRole.AGENT_DRIVER),
        messages=[LLMMessage(role="user", content="real work")],
    )

    await router.complete(request, context=CallContext(conversation_id="working"))

    assert "m" in rt._driver_preflight._ok
    assert ("working", ModelRole.AGENT_DRIVER, "m") in rt._driver_preflight._proven

    class _DeadRouter:
        calls = 0

        async def complete(self, req, *, context=None):
            self.calls += 1
            raise TimeoutError

    dead = _DeadRouter()
    rt.drivers.router = lambda **_kwargs: dead  # type: ignore[method-assign]
    assert await rt._preflight_driver("next") is None
    assert dead.calls == 0


async def test_real_completion_supersedes_concurrent_transient_probe(monkeypatch):
    import disco.agent_server.driver_runtime as driver_runtime_module

    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(store)

    class _ConfigStore:
        def load(self):
            return _cfg()

    monkeypatch.setattr(
        driver_runtime_module,
        "build_providers",
        lambda *_args, **_kwargs: {"fake": _FakeProvider()},
    )
    rt.drivers._config_store = _ConfigStore()  # type: ignore[assignment]
    live_router = rt.drivers.router(conversation_id="working")
    request = CompletionRequest(
        profile=CapabilityProfile(role=ModelRole.AGENT_DRIVER),
        messages=[LLMMessage(role="user", content="real work")],
    )

    class _BlockedTransientRouter:
        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def complete(self, req, *, context=None):
            self.started.set()
            await self.release.wait()
            raise TimeoutError

    blocked = _BlockedTransientRouter()
    rt.drivers.router = lambda **_kwargs: blocked  # type: ignore[method-assign]
    rt._driver_preflight._ATTEMPTS = 1
    pending = asyncio.create_task(rt._preflight_driver("waiting"))
    await blocked.started.wait()

    await live_router.complete(request, context=CallContext(conversation_id="working"))
    blocked.release.set()

    assert await pending is None
    assert ("waiting", ModelRole.AGENT_DRIVER, "m") in rt._driver_preflight._proven


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
    rt.drivers.router = lambda **kw: router

    results = await asyncio.gather(*(rt._preflight_driver(f"c{i}") for i in range(6)))

    assert results == [None] * 6
    assert router.calls == 1
    assert {key[0] for key in rt._driver_preflight._proven} == {f"c{i}" for i in range(6)}
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
    rt.drivers.router = lambda **kw: router
    rt._driver_preflight._SHARED_RESULT_TTL_S = 0.01

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
    model_key = next(iter(rt._driver_preflight._inflight))
    completed_task, completed_at = rt._driver_preflight._inflight[model_key]
    assert completed_at is not None
    assert await rt._preflight_driver("trailing") is not None
    assert router.calls == 1
    assert rt._driver_preflight._inflight[model_key] == (completed_task, completed_at)

    # The shared failure is a burst result, not a sticky outage cache.
    rt._driver_preflight._inflight[model_key] = (
        completed_task,
        completed_at - rt._driver_preflight._SHARED_RESULT_TTL_S - 1,
    )
    assert await rt._preflight_driver("later") is not None
    assert router.calls == 2


async def test_shared_preflight_failure_expires_without_a_later_caller():
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(store)
    rt._driver_preflight._SHARED_RESULT_TTL_S = 0.01

    class _DeadRouter:
        async def complete(self, req, *, context=None):
            raise LLMAuthError("invalid api key", provider="x")

    rt.drivers.router = lambda **kw: _DeadRouter()
    assert await rt._preflight_driver("c1") is not None
    assert rt._driver_preflight._inflight

    await asyncio.sleep(0.03)
    assert rt._driver_preflight._inflight == {}


async def test_aclose_cancels_and_drains_orphaned_shared_preflight():
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(store)
    started = asyncio.Event()

    class _SlowRouter:
        async def complete(self, req, *, context=None):
            started.set()
            await asyncio.sleep(60)

    rt.drivers.router = lambda **kw: _SlowRouter()
    waiter = asyncio.create_task(rt._preflight_driver("c1"))
    await started.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    shared_task = next(iter(rt._driver_preflight._inflight.values()))[0]
    assert not shared_task.done()
    await rt.aclose()
    assert shared_task.done() and shared_task.cancelled()
    assert rt._driver_preflight._inflight == {}


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

    rt.drivers.router = lambda **kw: _BlackHoleRouter()
    rt._driver_preflight._TIMEOUT_S = 0.1  # tiny bound for the test

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
    rt._driver_preflight._BACKOFF_S = 0.0  # keep the test fast

    class _FlakyRouter:
        def __init__(self):
            self.calls = 0

        async def complete(self, req, *, context=None):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError  # the deadline fired once (slow remote model)
            return  # answered on the retry

    flaky = _FlakyRouter()
    rt.drivers.router = lambda **kw: flaky
    assert await rt._preflight_driver("c1") is None  # proceeds, not terminal
    assert flaky.calls == 2  # one miss, then a successful re-probe


async def test_preflight_driver_soft_degrades_for_already_working_conversation():
    """An established run that ALREADY proved the driver reachable in THIS
    conversation must not be hard-failed by a later transient blip: a probe that
    now always times out soft-degrades to a warning and PROCEEDS (the real call
    will surface a genuine error if the driver is truly down)."""
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(store)
    rt._driver_preflight._BACKOFF_S = 0.0

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
    rt.drivers.router = lambda **kw: router

    # First kick: the driver is healthy → THIS (conversation, role, model) is proven.
    assert await rt._preflight_driver("c1") is None
    assert any(
        k[0] == "c1" and k[1] == ModelRole.AGENT_DRIVER for k in rt._driver_preflight._proven
    )

    # Simulate a later kick after the success cache's TTL has lapsed (30 turns in):
    rt._driver_preflight._ok.clear()  # force a real re-probe
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
    rt.drivers.router = lambda **kw: router
    assert await rt._preflight_driver("c1") is None

    rt._driver_preflight._ok.clear()
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
    rt._driver_preflight._BACKOFF_S = 0.0

    class _DeadRouter:
        def __init__(self):
            self.calls = 0

        async def complete(self, req, *, context=None):
            self.calls += 1
            raise TimeoutError  # black hole: never answers

    dead = _DeadRouter()
    rt.drivers.router = lambda **kw: dead
    reason = await rt._preflight_driver("c2")  # never proven
    assert reason is not None
    assert "timed out" in reason and "unreachable" in reason
    assert dead.calls == rt._driver_preflight._ATTEMPTS  # retried, still bounded
    assert not any(k[0] == "c2" for k in rt._driver_preflight._proven)


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
    rt._driver_preflight._BACKOFF_S = 0.0
    rt.drivers._config_store = _RoleStore()  # type: ignore[assignment]

    # Driver A (AGENT_DRIVER / model-a) is healthy → proves THAT driver only.
    class _OkRouter:
        async def complete(self, req, *, context=None):
            return

    rt.drivers.router = lambda **kw: _OkRouter()
    assert await rt._preflight_driver("c1", role=ModelRole.AGENT_DRIVER) is None
    assert ("c1", ModelRole.AGENT_DRIVER, "model-a") in rt._driver_preflight._proven

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
    rt.drivers.router = lambda **kw: dead
    reason = await rt._preflight_driver("c1", role=ModelRole.RAG_ANSWERER)
    assert reason is not None and "timed out" in reason and "unreachable" in reason
    assert dead.calls == rt._driver_preflight._ATTEMPTS  # actually probed, not masked
    assert (
        "c1",
        ModelRole.RAG_ANSWERER,
        "model-b",
    ) not in rt._driver_preflight._proven


def _dr_runtime(store: SqliteEventStore) -> ConversationRuntime:
    """A deep_research conversation with hermetic (present, healthy) encoders,
    so only the driver pre-flight decides whether the run starts."""
    rt = ConversationRuntime(
        store,
        research_providers={
            "search": object(),
            "extraction": object(),
            "reranker": _FakeNLI(),
            "embedder": None,
            "nli": _FakeNLI(),
        },
    )
    rt.settings._set_surface("c1", "deep_research")
    return rt


async def test_deep_research_kick_blocks_on_dead_driver():
    """P1-2: the gateless DR KICKOFF pre-flights the driver BEFORE any research
    starts. A dead/unauthed model emits ErrorEvent + StatusEvent(ERROR) and does
    NOT leave the conversation stuck RUNNING (v2: there is no plan step, so the
    kickoff pre-flight is the only thing standing between a dead driver and a
    long doomed run)."""
    store = SqliteEventStore(":memory:")
    rt = _dr_runtime(store)

    async def _dead(cid, **kw):
        return "Driver 'm' rejected the API key: invalid api key"

    rt._driver_preflight.check = _dead  # type: ignore[method-assign]

    await store.append(
        "c1",
        MessageEvent(
            source=EventSource.USER,
            message=LLMMessage(role="user", content="research X"),
        ),
    )
    await rt.deep_research._maybe_run_deep_research("c1")

    events = await store.get_events("c1")
    statuses = [e for e in events if isinstance(e, StatusEvent)]
    assert statuses and statuses[-1].status == ConversationStatus.ERROR
    assert "rejected the API key" in (statuses[-1].detail or "")
    assert any(isinstance(e, ErrorEvent) and e.code == "deep_research_preflight" for e in events)
    # NEVER set RUNNING → not stuck. And no plan machinery is involved at all.
    assert not any(s.status == ConversationStatus.RUNNING for s in statuses)
    assert not any(isinstance(e, PlanEvent) for e in events)


async def test_deep_research_run_failure_goes_error_not_stuck(monkeypatch):
    """PKG-34 semantics, retained in v2: when the pre-flight passes and the run
    itself then fails (a provider blowing up mid-research), the single failure
    handler takes the conversation to ERROR with the named reason — it never
    spins forever at RUNNING. (The old decompose-failure path this replaced is
    gone: v2 never decomposes.)"""
    from disco.agent_server._deep_research_service_parts import execute as execute_parts

    store = SqliteEventStore(":memory:")
    rt = _dr_runtime(store)

    async def _ok(cid, **kw):
        return None  # pre-flight passes

    rt._driver_preflight.check = _ok  # type: ignore[method-assign]
    rt.drivers.router = lambda **kw: object()

    async def _boom(*args, **kwargs):
        raise LLMAuthError("research key rejected", provider="x")

    monkeypatch.setattr(execute_parts, "run_engine", _boom)

    await store.append(
        "c1",
        MessageEvent(
            source=EventSource.USER,
            message=LLMMessage(role="user", content="research X"),
        ),
    )
    await rt.deep_research._maybe_run_deep_research("c1")

    events = await store.get_events("c1")
    statuses = [e for e in events if isinstance(e, StatusEvent)]
    assert statuses and statuses[-1].status == ConversationStatus.ERROR
    assert "research key rejected" in (statuses[-1].detail or "")
    assert any(isinstance(e, ErrorEvent) and e.code == "deep_research_failed" for e in events)


async def _failing_run(store, monkeypatch, *, error_event_failures: int):
    """Drive a doomed DR run whose terminal ErrorEvent append fails the first
    `error_event_failures` times. Returns the conversation's events."""
    from disco.agent_server._deep_research_service_parts import execute as execute_parts

    monkeypatch.setattr(execute_parts, "_TERMINAL_RETRY_DELAY_S", 0.0)
    rt = _dr_runtime(store)

    async def _ok(cid, **kw):
        return None

    rt._driver_preflight.check = _ok  # type: ignore[method-assign]
    rt.drivers.router = lambda **kw: object()

    async def _boom(*args, **kwargs):
        raise LLMAuthError("research key rejected", provider="x")

    monkeypatch.setattr(execute_parts, "run_engine", _boom)

    real_append = store.append
    remaining = {"n": error_event_failures}

    async def _flaky_append(conversation_id, event):
        if isinstance(event, ErrorEvent) and remaining["n"] > 0:
            remaining["n"] -= 1
            raise RuntimeError("event store write failed")
        return await real_append(conversation_id, event)

    monkeypatch.setattr(store, "append", _flaky_append)

    await real_append(
        "c1",
        MessageEvent(
            source=EventSource.USER,
            message=LLMMessage(role="user", content="research X"),
        ),
    )
    await rt.deep_research._maybe_run_deep_research("c1")
    monkeypatch.setattr(store, "append", real_append)
    return await store.get_events("c1")


async def test_failed_terminal_write_is_retried_before_the_run_is_abandoned(monkeypatch):
    """A conversation pinned RUNNING with no terminal event is the worst funnel
    outcome. One transient failure writing the terminal ErrorEvent is retried,
    and the run still ends with both the ErrorEvent and ERROR."""
    store = SqliteEventStore(":memory:")
    events = await _failing_run(store, monkeypatch, error_event_failures=1)

    assert any(isinstance(e, ErrorEvent) and e.code == "deep_research_failed" for e in events)
    statuses = [e for e in events if isinstance(e, StatusEvent)]
    assert statuses and statuses[-1].status == ConversationStatus.ERROR
    assert "research key rejected" in (statuses[-1].detail or "")


async def test_unwritable_error_event_still_moves_the_run_out_of_running(monkeypatch):
    """When the ErrorEvent can never be written, the status-only fallback still
    takes the conversation to ERROR — it is never left stranded at RUNNING."""
    store = SqliteEventStore(":memory:")
    events = await _failing_run(store, monkeypatch, error_event_failures=99)

    assert not any(isinstance(e, ErrorEvent) for e in events)  # never persisted
    statuses = [e for e in events if isinstance(e, StatusEvent)]
    assert statuses and statuses[-1].status == ConversationStatus.ERROR


async def test_totally_unwritable_failure_is_logged_critical_not_swallowed(
    monkeypatch, caplog
):
    """Both the retry and the status-only fallback failing is the one case that
    truly strands the conversation: it must be logged CRITICAL with the id."""
    from disco.agent_server._deep_research_service_parts import execute as execute_parts

    monkeypatch.setattr(execute_parts, "_TERMINAL_RETRY_DELAY_S", 0.0)
    store = SqliteEventStore(":memory:")
    rt = _dr_runtime(store)

    async def _ok(cid, **kw):
        return None

    rt._driver_preflight.check = _ok  # type: ignore[method-assign]
    rt.drivers.router = lambda **kw: object()

    async def _boom(*args, **kwargs):
        raise LLMAuthError("research key rejected", provider="x")

    monkeypatch.setattr(execute_parts, "run_engine", _boom)

    real_append = store.append
    await real_append(
        "c1",
        MessageEvent(
            source=EventSource.USER,
            message=LLMMessage(role="user", content="research X"),
        ),
    )

    async def _dead_append(conversation_id, event):
        raise RuntimeError("event store write failed")

    monkeypatch.setattr(store, "append", _dead_append)
    with caplog.at_level("CRITICAL"):
        await rt.deep_research._maybe_run_deep_research("c1")
    monkeypatch.setattr(store, "append", real_append)

    assert any(
        record.levelname == "CRITICAL" and "c1" in record.getMessage()
        for record in caplog.records
    )


async def test_deep_research_kickoff_probes_rag_answerer_role():
    """P1-3 in v2: the gateless kickoff probes the role the run GENERATES with —
    RAG_ANSWERER (the research agent and the writer both use it) — not
    AGENT_DRIVER, and no longer QUERY_REWRITER (nothing decomposes any more)."""
    store = SqliteEventStore(":memory:")
    rt = _dr_runtime(store)

    probed: list[ModelRole] = []

    async def _by_role(cid, *, override=None, role=ModelRole.AGENT_DRIVER):
        probed.append(role)
        # Exactly the bounded-timeout reason _preflight_driver returns for a
        # black hole (see test_preflight_driver_is_wall_bounded_on_black_hole).
        return "Driver 'm' unreachable: no response within 8s (pre-flight timed out)"

    rt._driver_preflight.check = _by_role  # type: ignore[method-assign]

    await store.append(
        "c1",
        MessageEvent(
            source=EventSource.USER,
            message=LLMMessage(role="user", content="research X"),
        ),
    )
    await rt.deep_research._maybe_run_deep_research("c1")

    events = await store.get_events("c1")
    statuses = [e for e in events if isinstance(e, StatusEvent)]
    assert probed == [ModelRole.RAG_ANSWERER]
    assert statuses and statuses[-1].status == ConversationStatus.ERROR
    assert "timed out" in (statuses[-1].detail or "")
    # NEVER set RUNNING → genuinely bounded, not stuck.
    assert not any(s.status == ConversationStatus.RUNNING for s in statuses)


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

    rt._driver_preflight.check = _capture  # type: ignore[method-assign]
    frames = [f async for f in rt.deep_research.research_stream("q")]

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
    rt.settings._set_surface("c1", "build")

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
    reason = await rt.deep_research._preflight_encoders(deps, required=("reranker", "nli"))
    assert reason is not None
    assert "reranker" in reason and "dead:8091" in reason


async def test_preflight_encoders_names_missing_nli():
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(store, router=DefaultLLMRouter(_cfg(), {"fake": _FakeProvider()}))
    # bundled/fake reranker (no probe) present, but NLI is None (declined encoder).
    deps = {"reranker": _FakeNLI(), "nli": None}
    reason = await rt.deep_research._preflight_encoders(deps, required=("reranker", "nli"))
    assert reason is not None and "nli" in reason


async def test_preflight_encoders_passes_when_all_present():
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(store, router=DefaultLLMRouter(_cfg(), {"fake": _FakeProvider()}))
    # Bundled encoders have no probe() → treated as present (raise on RAM fail).
    deps = {"reranker": _FakeNLI(), "nli": _FakeNLI()}
    assert await rt.deep_research._preflight_encoders(deps, required=("reranker", "nli")) is None


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
    frames = [f async for f in rt.deep_research.research_stream("q")]
    assert len(frames) == 1
    assert frames[0]["type"] == "error"
    assert "reranker" in frames[0]["message"]


async def test_execute_deep_research_blocks_on_dead_encoder():
    """The DR report engine does NOT start on a dead required encoder: an
    ErrorEvent + StatusEvent(ERROR) name the reason instead of a wrong report.
    A degraded remote reranker returns input order silently, so this check is
    the only thing between it and a quietly-wrong report."""
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
    await rt.deep_research._execute_deep_research("c1")

    events = await store.get_events("c1")
    assert any(isinstance(e, ErrorEvent) and e.code == "deep_research_preflight" for e in events)
    errs = [
        e for e in events if isinstance(e, StatusEvent) and e.status == ConversationStatus.ERROR
    ]
    assert errs and "reranker" in (errs[-1].detail or "")
    # the engine never produced a report
    from disco.core import ReportEvent

    assert not any(isinstance(e, ReportEvent) for e in events)
