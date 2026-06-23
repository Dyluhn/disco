from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from disco.agent_server.lifecycle import LifecycleManager
from disco.agent_server.runtime import ConversationRuntime
from disco.core import (
    ConversationStatus,
    EventSource,
    LLMMessage,
    MessageEvent,
    PlanEvent,
    PlanStep,
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

    rt._preflight_driver = dead_driver  # type: ignore[method-assign]

    await rt._maybe_run_deep_research(cid)
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

    frames = [frame async for frame in rt.research_stream("What changed?", conversation_id="dr1")]

    assert frames == [
        {
            "type": "error",
            "message": (
                "Deep Research needs the reranker, but it isn't reachable at "
                "http://dead:8091"
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


async def test_w48_gvisor_preflight_names_endpoint_and_is_bounded() -> None:
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(store)
    endpoint = "ssh://sandbox@100.81.82.115"
    rt._sandbox_service_now = lambda: _SandboxService(endpoint, "hang")  # type: ignore[method-assign]
    rt._SANDBOX_PREFLIGHT_TIMEOUT_S = 0.03

    started = time.perf_counter()
    reason = await rt._preflight_sandbox("conv_gvisor")
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

    rt._sandbox_service_now = lambda: _SandboxService(endpoint, "error")  # type: ignore[method-assign]
    reason = await rt._preflight_sandbox("conv_gvisor")
    assert reason is not None
    assert endpoint in reason
    assert "connection refused" in reason
    assert "unreachable" in reason

    rt._sandbox_service_now = lambda: _SandboxService(endpoint, "ok")  # type: ignore[method-assign]
    assert await rt._preflight_sandbox("conv_gvisor") is None


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
        _connections={"ui_connected": 1},
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
