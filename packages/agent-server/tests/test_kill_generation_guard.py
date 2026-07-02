"""Finding #4 — the LAST terminalizer path: the KILL switch (BoD §13.6).

`ConversationRuntime.kill` captures the run-generation, clears that generation's
pin, and threads the generation to `ControlOps.kill`, whose teardown AWAITS the
killed task AND the executor/sandbox teardown. In those await windows a fresh user
turn can start a NEWER run (generation N+1) that REUSES this conversation's cached
loop/executor/session and pin. The stale kill must then neither tear down the newer
run's executor nor append the terminal IDLE into its log.

These tests bind the SHIPPED runtime + the real `ControlOps` (the runtime constructs
one in `__init__`) so the real kill path is exercised, not a copy. The race is
simulated by bumping `_run_generation[cid]` inside one of kill's teardown awaits —
exactly the moment a newer run would take over.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from disco.agent_server import ConversationRuntime
from disco.core import (
    ActionEvent,
    AgentErrorEvent,
    ConversationStatus,
    EventSource,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    SqliteEventStore,
    StatusEvent,
    ToolCall,
    ToolResult,
)
from disco.tools import ProcessSandboxService

from harness.build_soak.events import normalize_events
from harness.build_soak.oracles.event_chain import EventChainOracle

CID = "conv-kill-gen"


def _runtime(store: SqliteEventStore) -> ConversationRuntime:
    router = MagicMock()
    svc = ProcessSandboxService()
    return ConversationRuntime(store, router=router, sandbox_service=svc)


async def _seed(store: SqliteEventStore) -> None:
    store.create_conversation(CID, owner_id="local")
    await store.append(
        CID,
        MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content="build it")),
    )
    await store.append(CID, StatusEvent(status=ConversationStatus.RUNNING))


def _killed_detail_present(events: list[object]) -> bool:
    return any(
        isinstance(e, StatusEvent) and e.detail == "killed" for e in events
    )


def _cancelled_errors(events: list[object], action_id: str) -> list[AgentErrorEvent]:
    return [
        e
        for e in events
        if isinstance(e, AgentErrorEvent)
        and e.action_id == action_id
        and e.error == "cancelled"
    ]


def _action(action_id: str = "act-inflight", call_id: str = "call-inflight") -> ActionEvent:
    return ActionEvent(
        id=action_id,
        thought="running a long tool call",
        tool_call=ToolCall(
            tool_name="shell",
            arguments={"command": "sleep 60"},
            call_id=call_id,
        ),
    )


class _RaceTask:
    """A stand-in for the killed run's task whose `await` (after cancel) bumps the
    run-generation — i.e. a NEWER run starts in the `await task` window."""

    def __init__(self, on_await) -> None:
        self._on_await = on_await

    def done(self) -> bool:
        return False

    def cancel(self) -> None:  # the kill cancels it; no-op here
        pass

    def __await__(self):
        self._on_await()
        return iter(())  # immediately-complete awaitable → returns None


@pytest.mark.asyncio
async def test_kill_newer_run_in_task_await_window_does_not_corrupt_it() -> None:
    """Guard A: a newer run starts while kill AWAITS the killed task. The killed run's
    task is already cancelled, so the conversation is yielded to the newer run — its
    executor is NOT torn down and NO terminal IDLE lands in its log."""
    store = SqliteEventStore(":memory:")
    await _seed(store)
    rt = _runtime(store)

    rt._run_generation[CID] = 1  # the killed run's generation

    # The newer run (gen 2) reuses the SAME cached executor/loop — it must survive.
    survivor_executor = MagicMock()
    survivor_executor.kill = AsyncMock()
    rt._executors[CID] = survivor_executor
    survivor_loop = object()
    rt._loops[CID] = survivor_loop

    def _newer_run_starts() -> None:
        rt._run_generation[CID] = 2  # gen 2 takes over in the await-task window

    rt._tasks[CID] = _RaceTask(_newer_run_starts)

    await rt.kill(CID)

    # The newer run's executor is NOT killed and its loop is NOT evicted.
    survivor_executor.kill.assert_not_awaited()
    assert rt._executors.get(CID) is survivor_executor
    assert rt._loops.get(CID) is survivor_loop
    # No stale terminal IDLE in the newer run's log.
    assert not _killed_detail_present(await store.get_events(CID))


@pytest.mark.asyncio
async def test_kill_newer_run_in_executor_teardown_window_does_not_terminalize_it() -> None:
    """Guard B: gen still matches at the first check (no race in the task-await window),
    but a newer run starts DURING the executor teardown await. The killed run's OWN
    executor is still torn down (correct), but the re-check immediately before the
    append (no await between) skips the stale IDLE so it never lands in the newer run's
    log."""
    store = SqliteEventStore(":memory:")
    await _seed(store)
    rt = _runtime(store)

    rt._run_generation[CID] = 1  # the killed run's generation; unchanged through guard A

    async def _kill_then_newer_run() -> None:
        # Tearing down the killed run's executor; a fresh user turn starts gen 2 here.
        rt._run_generation[CID] = 2

    killed_executor = MagicMock()
    killed_executor.kill = AsyncMock(side_effect=_kill_then_newer_run)
    rt._executors[CID] = killed_executor
    rt._loops[CID] = object()
    # No live task → guard A passes (generation still 1 when it is checked).
    rt._tasks.pop(CID, None)

    await rt.kill(CID)

    # The killed run's OWN executor was still torn down...
    killed_executor.kill.assert_awaited_once()
    # ...but the stale terminal IDLE is NOT appended into the newer run's log.
    assert not _killed_detail_present(await store.get_events(CID))


@pytest.mark.asyncio
async def test_kill_with_no_newer_run_terminalizes_and_tears_down() -> None:
    """The non-race path: a normal kill (no newer run — its generation is unchanged at
    teardown time) tears down the executor + sandbox, evicts the cached loop, releases
    the kernel pin, and appends the terminal IDLE 'killed' — exactly as before the
    guard was added."""
    store = SqliteEventStore(":memory:")
    await _seed(store)
    rt = _runtime(store)

    rt._run_generation[CID] = 1
    rt._pinned_kernels[CID] = object()  # the killed run's pin

    executor = MagicMock()
    executor.kill = AsyncMock()
    rt._executors[CID] = executor
    pending = MagicMock()
    pending.destroy = AsyncMock()
    rt._pending_sessions[CID] = pending
    rt._loops[CID] = object()

    await rt.kill(CID)

    executor.kill.assert_awaited_once()
    pending.destroy.assert_awaited_once()
    assert CID not in rt._executors
    assert CID not in rt._pending_sessions
    assert CID not in rt._loops
    assert CID not in rt._pinned_kernels  # terminal kill releases the pin
    assert _killed_detail_present(await store.get_events(CID))
    assert (await store.get_state(CID)).execution_status is ConversationStatus.IDLE


@pytest.mark.asyncio
async def test_kill_with_no_generation_tracked_terminalizes_legacy() -> None:
    """A kill with NO tracked run-generation (`generation is None` — a conversation that
    never started a run, or a legacy/direct caller) is NEVER treated as superseded and
    terminalizes unconditionally, preserving the pre-guard behavior."""
    store = SqliteEventStore(":memory:")
    await _seed(store)
    rt = _runtime(store)

    # No entry in _run_generation → captured generation is None.
    executor = MagicMock()
    executor.kill = AsyncMock()
    rt._executors[CID] = executor
    rt._loops[CID] = object()

    await rt.kill(CID)

    executor.kill.assert_awaited_once()
    assert CID not in rt._loops
    assert _killed_detail_present(await store.get_events(CID))


@pytest.mark.asyncio
async def test_kill_closes_dangling_action_with_cancelled_agent_error() -> None:
    store = SqliteEventStore(":memory:")
    await _seed(store)
    action = await store.append(CID, _action())
    rt = _runtime(store)
    rt._run_generation[CID] = 1

    await rt.kill(CID)

    events = await store.get_events(CID)
    cancelled = _cancelled_errors(events, action.id)
    assert len(cancelled) == 1
    assert cancelled[0].detail == "action cancelled by kill switch before completing"
    assert cancelled[0].tool_call_id == action.tool_call.call_id
    assert cancelled[0].seq is not None and action.seq is not None
    assert cancelled[0].seq > action.seq


@pytest.mark.asyncio
async def test_second_kill_does_not_duplicate_cancelled_agent_error() -> None:
    store = SqliteEventStore(":memory:")
    await _seed(store)
    action = await store.append(CID, _action())
    rt = _runtime(store)
    rt._run_generation[CID] = 1

    await rt.kill(CID)
    await rt.kill(CID)

    events = await store.get_events(CID)
    assert len(_cancelled_errors(events, action.id)) == 1


@pytest.mark.asyncio
async def test_kill_with_no_dangling_actions_adds_no_cancelled_agent_error() -> None:
    store = SqliteEventStore(":memory:")
    await _seed(store)
    action = await store.append(CID, _action())
    await store.append(
        CID,
        ObservationEvent(
            tool_result=ToolResult(
                call_id=action.tool_call.call_id,
                tool_name=action.tool_call.tool_name,
                success=True,
                content="done",
            ),
            action_id=action.id,
        ),
    )
    rt = _runtime(store)
    rt._run_generation[CID] = 1

    await rt.kill(CID)

    events = await store.get_events(CID)
    assert _cancelled_errors(events, action.id) == []


@pytest.mark.asyncio
async def test_event_chain_oracle_passes_killed_sequence_after_pair_recovery() -> None:
    store = SqliteEventStore(":memory:")
    await _seed(store)
    await store.append(CID, _action())
    rt = _runtime(store)
    rt._run_generation[CID] = 1

    await rt.kill(CID)

    events = [e.model_dump(mode="json") for e in await store.get_events(CID)]
    scenario = {
        "id": "kill-recovery",
        "assertions": {"event_chain": {"require_action_observation_pairs": True}},
    }
    results = EventChainOracle().check(normalize_events(events), scenario=scenario)
    assert all(r.passed for r in results), [r.to_dict() for r in results]
