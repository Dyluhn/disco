from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from disco.agent_server.lifecycle import LifecycleManager
from disco.agent_server.runtime import ConversationRuntime
from disco.core import (
    ConversationStatus,
    EventSource,
    LLMMessage,
    MessageEvent,
    PlanEvent,
    StatusEvent,
)
from disco.core.store.sqlite import SqliteEventStore
from disco.retrieval.local_encoders import EncoderUnavailable
from disco.tools.sandbox import SandboxUnavailableError


class _NeverReturningRouter:
    async def complete(self, *_args, **_kwargs):
        await asyncio.sleep(3600)


async def test_w35_driver_preflight_is_bounded_and_names_driver() -> None:
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(store)
    rt._router_now = lambda *_args, **_kwargs: _NeverReturningRouter()  # type: ignore[method-assign]
    rt._DRIVER_PREFLIGHT_TIMEOUT_S = 0.03
    rt._DRIVER_PREFLIGHT_ATTEMPTS = 1

    started = time.perf_counter()
    reason = await rt._preflight_driver("conv_dead_driver")
    elapsed = time.perf_counter() - started

    assert elapsed < 0.5, f"driver preflight took {elapsed:.3f}s instead of failing fast"
    assert reason is not None
    assert "Driver" in reason
    assert "unreachable" in reason
    assert "timed out" in reason


async def test_w35_dead_driver_records_error_and_does_not_enter_loop() -> None:
    store = SqliteEventStore(":memory:")
    cid = "conv_dead_driver_no_loop"
    store.create_conversation(cid, surface="agent")
    rt = ConversationRuntime(store)

    async def dead_driver(*_args, **_kwargs) -> str:
        return "Driver 'broken-driver' unreachable: connection refused at http://dead/v1"

    class LoopThatMustNotRun:
        ran = False

        async def run(self):
            self.ran = True
            raise AssertionError("a doomed loop was started after preflight failed")

    loop = LoopThatMustNotRun()
    rt._preflight_driver = dead_driver  # type: ignore[method-assign]

    state = await rt._run_with_persistence(cid, loop)
    events = await store.get_events(cid)

    assert loop.ran is False
    assert state.execution_status is ConversationStatus.ERROR
    errors = [e for e in events if isinstance(e, StatusEvent)]
    assert errors and errors[-1].status is ConversationStatus.ERROR
    assert "broken-driver" in (errors[-1].detail or "")
    assert "unreachable" in (errors[-1].detail or "")


async def test_w35_deep_research_initial_kick_fails_before_running_on_dead_driver() -> None:
    store = SqliteEventStore(":memory:")
    cid = "conv_dr_dead_driver"
    store.create_conversation(cid, surface="deep_research")
    await store.append(
        cid,
        MessageEvent(
            source=EventSource.USER,
            message=LLMMessage(role="user", content="Research fusion energy milestones"),
        ),
    )
    rt = ConversationRuntime(store)

    async def dead_driver(*_args, **_kwargs) -> str:
        return "Driver 'dr-answerer' unreachable: no route to host"

    # Epic 13-B3 promoted deep research onto `rt.deep_research` and driver
    # preflight onto a DriverPreflight collaborator. `rt._preflight_driver` still
    # EXISTS and still forwards, so the call route survived — but a delegator
    # forwards calls, not monkeypatches, so assigning to it stopped intercepting
    # anything (finding F8). Seam re-pointed at the collaborator that now owns
    # the behavior; the assertions below are untouched.
    rt.deep_research._preflight.check = dead_driver

    await rt.deep_research._maybe_run_deep_research(cid)
    events = await store.get_events(cid)

    assert not any(isinstance(e, PlanEvent) for e in events)
    statuses = [e for e in events if isinstance(e, StatusEvent)]
    assert statuses[-1].status is ConversationStatus.ERROR
    assert "dr-answerer" in (statuses[-1].detail or "")
    assert "unreachable" in (statuses[-1].detail or "")
    assert all(s.status is not ConversationStatus.RUNNING for s in statuses)


async def test_w33_deep_research_stream_stops_on_named_encoder_preflight_error() -> None:
    class DeadReranker:
        async def probe(self) -> None:
            raise EncoderUnavailable(
                "Deep Research needs the reranker, but it isn't reachable at http://dead:8091"
            )

    class HealthyNli:
        async def probe(self) -> None:
            return None

    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(
        store,
        research_providers={"reranker": DeadReranker(), "nli": HealthyNli()},
    )
    rt._preflight_driver = lambda *_args, **_kwargs: asyncio.sleep(0, result=None)  # type: ignore[method-assign]

    stream = rt.deep_research.research_stream("What changed?", conversation_id="dr1")
    frames = [frame async for frame in stream]

    assert frames == [
        {
            "type": "error",
            "message": (
                "Deep Research needs the reranker, but it isn't reachable at http://dead:8091"
            ),
        }
    ]


class _SandboxService:
    name = "gvisor"

    def __init__(self, endpoint: str, outcome: str) -> None:
        self._cfg = SimpleNamespace(docker_socket=endpoint)
        self.outcome = outcome

    async def healthcheck(self) -> None:
        if self.outcome == "ok":
            return None
        if self.outcome == "hang":
            await asyncio.sleep(3600)
        raise SandboxUnavailableError("connection refused")


class _CurrentTransitionWorkspace:
    """Minimal fake for the workspace coordinator's current-run transition seam."""

    def __init__(self, store: SqliteEventStore) -> None:
        self._store = store

    async def append_current_run_transition(
        self,
        conversation_id: str,
        events: list[MessageEvent],
        status: StatusEvent,
        *,
        expected_statuses: frozenset[ConversationStatus],
    ):
        state = await self._store.get_state(conversation_id)
        if state.execution_status not in expected_statuses:
            return None
        return await self._store.append_many(conversation_id, [*events, status])


def _point_sandbox_seam(rt, endpoint: str, outcome: str) -> None:
    """Re-point this file's sandbox seams at the collaborator that now owns them.

    Epic 13-B3 moved sandbox preflight off ConversationRuntime onto
    `rt.sandbox` (SandboxRuntimeService): `_preflight_sandbox` became
    `preflight_failure()`, `_sandbox_service_now` moved with it, and the endpoint
    label is now read from the config store rather than off the service stub.
    Setting the old attributes on `rt` created new, unread attributes and the
    real preflight ran instead — the delegator/injector split recorded as F8.
    Seam re-pointed; every assertion in the tests below is untouched.
    """
    rt.sandbox._sandbox_service_now = lambda: _SandboxService(endpoint, outcome)
    cfg = rt.sandbox._config_store.load()
    cfg.sandbox.docker_socket = endpoint
    rt.sandbox._config_store = SimpleNamespace(load=lambda: cfg)


async def test_w48_gvisor_preflight_names_endpoint_and_is_bounded(monkeypatch) -> None:
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(store)
    endpoint = "ssh://sandbox@100.81.82.115"
    _point_sandbox_seam(rt, endpoint, "hang")
    # The bound is now a module constant on the owning collaborator, not an
    # attribute on the runtime.
    monkeypatch.setattr(
        "disco.agent_server.sandbox_runtime_service._RUN_PREFLIGHT_TIMEOUT_S", 0.03
    )

    started = time.perf_counter()
    reason = await rt.sandbox.preflight_failure()
    elapsed = time.perf_counter() - started

    assert elapsed < 0.5, f"gVisor preflight took {elapsed:.3f}s instead of failing fast"
    assert reason is not None
    assert endpoint in reason
    assert "gvisor sandbox host" in reason
    assert "unreachable" in reason


async def test_w48_gvisor_preflight_reports_typed_endpoint_error_and_ok() -> None:
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(store)
    endpoint = "ssh://sandbox@100.81.82.115"

    _point_sandbox_seam(rt, endpoint, "error")
    reason = await rt.sandbox.preflight_failure()
    assert reason is not None
    assert endpoint in reason
    assert "connection refused" in reason
    assert "unreachable" in reason

    _point_sandbox_seam(rt, endpoint, "ok")
    assert await rt.sandbox.preflight_failure() is None


async def test_pc_abandoned_gate_sweeper_reaps_only_stale_unwatched_gates(monkeypatch) -> None:
    monkeypatch.setenv("DISCO_ABANDONED_GATE_TTL_S", "60")
    store = SqliteEventStore(":memory:")
    old = datetime.now(UTC) - timedelta(hours=2)
    recent = datetime.now(UTC)

    async def add_status(cid: str, status: ConversationStatus, timestamp: datetime) -> None:
        store.create_conversation(cid, surface="agent")
        await store.append(
            cid,
            StatusEvent(status=status, timestamp=timestamp),
        )

    await add_status("abandoned", ConversationStatus.AWAITING_PLAN_APPROVAL, old)
    await add_status("recent_gate", ConversationStatus.AWAITING_USER_DECISION, recent)
    await add_status("ui_connected", ConversationStatus.AWAITING_USER_QUESTION, old)
    await add_status("active_run_gate", ConversationStatus.WAITING_FOR_CONFIRMATION, old)
    await add_status("running", ConversationStatus.RUNNING, old)

    rt = SimpleNamespace(
        _store=store,
        _workspace=_CurrentTransitionWorkspace(store),
        _connections={"ui_connected": 1},
        _run_generation={},
        _unpin_if_current_generation=lambda *_args: None,
        running_conversation_ids=lambda: {"active_run_gate"},
    )
    swept = await LifecycleManager(rt).sweep_abandoned_gates_once()

    assert swept == 1
    assert (await store.get_state("abandoned")).execution_status is ConversationStatus.STUCK
    assert (await store.get_state("recent_gate")).execution_status is (
        ConversationStatus.AWAITING_USER_DECISION
    )
    assert (await store.get_state("ui_connected")).execution_status is (
        ConversationStatus.AWAITING_USER_QUESTION
    )
    assert (await store.get_state("active_run_gate")).execution_status is (
        ConversationStatus.WAITING_FOR_CONFIRMATION
    )
    assert (await store.get_state("running")).execution_status is ConversationStatus.RUNNING


async def test_pc_abandoned_gate_sweeper_logs_candidate_errors(monkeypatch, caplog) -> None:
    monkeypatch.setenv("DISCO_ABANDONED_GATE_TTL_S", "60")
    store = SqliteEventStore(":memory:")
    store.create_conversation("healthy_gate", surface="agent")
    store.create_conversation("broken_gate", surface="agent")
    await store.append(
        "broken_gate",
        StatusEvent(
            status=ConversationStatus.AWAITING_PLAN_APPROVAL,
            timestamp=datetime.now(UTC) - timedelta(hours=2),
        ),
    )
    await store.append(
        "healthy_gate",
        StatusEvent(
            status=ConversationStatus.AWAITING_PLAN_APPROVAL,
            timestamp=datetime.now(UTC) - timedelta(hours=2),
        ),
    )

    class ExplodingGeneration:
        def get(self, cid: str):
            if cid == "broken_gate":
                raise RuntimeError("generation lookup failed")
            return None

    rt = SimpleNamespace(
        _store=store,
        _workspace=_CurrentTransitionWorkspace(store),
        _connections={},
        _run_generation=ExplodingGeneration(),
        _unpin_if_current_generation=lambda *_args: None,
        running_conversation_ids=lambda: set(),
    )
    ids = await store.list_conversations(owner_id="local", limit=2)
    assert ids == ["broken_gate", "healthy_gate"]
    caplog.set_level(logging.ERROR, logger="disco.agent_server.lifecycle")
    assert await LifecycleManager(rt).sweep_abandoned_gates_once() == 1
    assert (await store.get_state("healthy_gate")).execution_status is ConversationStatus.STUCK
    assert "failed to evaluate abandoned gate conversation broken_gate" in caplog.text
    assert "generation lookup failed" in caplog.text


async def test_pc_abandoned_gate_background_sweep_logs_and_continues(monkeypatch, caplog) -> None:
    class BrokenRuntime:
        abandoned_calls = 0

        async def sweep_idle_once(self) -> None:
            return None

        async def sweep_stranded_runs_once(self) -> None:
            return None

        async def sweep_abandoned_gates_once(self) -> None:
            self.abandoned_calls += 1
            raise RuntimeError("conversation listing failed")

    sleep_delays: list[float] = []

    async def bounded_sleep(delay: float) -> None:
        sleep_delays.append(delay)
        if len(sleep_delays) == 2:
            raise asyncio.CancelledError

    async def no_tts_unload(*, ttl_s: int) -> None:
        del ttl_s

    monkeypatch.setenv("DISCO_IDLE_SWEEP_INTERVAL_S", "60")
    monkeypatch.setattr(asyncio, "sleep", bounded_sleep)
    monkeypatch.setattr(
        "disco.agent_server.tts_local.maybe_unload_if_idle",
        no_tts_unload,
    )
    caplog.set_level(logging.ERROR, logger="disco.agent_server.lifecycle")
    rt = BrokenRuntime()
    await LifecycleManager(rt)._idle_sweep_loop()

    assert sleep_delays == [60.0, 60.0]
    assert rt.abandoned_calls == 1
    assert "abandoned gate sweep failed" in caplog.text
    assert "conversation listing failed" in caplog.text


async def test_pc_idle_sweep_invalid_intervals_use_bounded_default(monkeypatch, caplog) -> None:
    class RuntimeThatMustNotSweep:
        pass

    delays: list[float] = []

    async def cancel_after_first_sleep(delay: float) -> None:
        delays.append(delay)
        raise asyncio.CancelledError

    caplog.set_level(logging.ERROR, logger="disco.agent_server.lifecycle")
    invalid = ("0", "-1", "5e-324", "0.001", "4.999", "nan", "inf", "not-a-number")
    for raw in invalid:
        delays.clear()
        monkeypatch.setenv("DISCO_IDLE_SWEEP_INTERVAL_S", raw)
        monkeypatch.setattr(asyncio, "sleep", cancel_after_first_sleep)
        await LifecycleManager(RuntimeThatMustNotSweep())._idle_sweep_loop()
        assert delays == [60.0]

    assert caplog.text.count("DISCO_IDLE_SWEEP_INTERVAL_S must be finite") == len(invalid)
