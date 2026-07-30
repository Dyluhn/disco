"""Finding #4 — the LAST terminalizer path: the KILL switch (BoD §13.6).

`ConversationRuntime.kill` retains process generation only for cache cleanup.
Exact task identity protects same-process replacement, while durable
run-intent/view identity is the final transition authority across restarts.

These tests bind the shipped runtime and real command service. Races publish an
actual replacement task or durable peer identity; a generation-only mismatch is
diagnostic and must not veto a current durable target.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
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
    WorkspaceMutationEvent,
    derive_final_workspace_fence,
)
from disco.tools import ProcessSandboxService

from harness.build_soak.events import normalize_events
from harness.build_soak.oracles.event_chain import EventChainOracle

CID = "conv-kill-gen"


@pytest.fixture(autouse=True)
def _deploy_lock_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCO_DEPLOY_LOCK_DIR", str(tmp_path / "locks"))


def _runtime(store: SqliteEventStore) -> ConversationRuntime:
    router = MagicMock()
    svc = ProcessSandboxService()
    return ConversationRuntime(store, router=router, sandbox_service=svc)


def _advance_generation(rt: ConversationRuntime) -> int:
    task = MagicMock()
    task.done.return_value = True
    generation = rt._run_registry.register_task(CID, task)
    assert rt._run_registry.complete_task(CID, task)
    return generation


async def _close_dangling_actions(
    rt: ConversationRuntime,
) -> list[AgentErrorEvent]:
    authority = await rt._lifecycle_commands.resolve_current_authority(CID)
    async with rt._workspace.lock(CID):
        async with rt._workspace.interprocess_mutation_fence(CID):
            return await rt._run_kills._close_dangling_actions_locked(CID, authority)


async def _seed(store: SqliteEventStore) -> None:
    store.create_conversation(CID, owner_id="local")
    await store.append(
        CID,
        MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content="build it")),
    )
    await store.append(CID, StatusEvent(status=ConversationStatus.RUNNING))


def _killed_detail_present(events: list[object]) -> bool:
    return any(isinstance(e, StatusEvent) and e.detail == "killed" for e in events)


def _cancelled_errors(events: list[object], action_id: str) -> list[AgentErrorEvent]:
    return [
        e
        for e in events
        if isinstance(e, AgentErrorEvent) and e.action_id == action_id and e.error == "cancelled"
    ]


def _action(
    action_id: str = "act-inflight",
    call_id: str = "call-inflight",
    *,
    agent_view_id: str | None = None,
) -> ActionEvent:
    return ActionEvent(
        id=action_id,
        thought="running a long tool call",
        tool_call=ToolCall(
            tool_name="shell",
            arguments={"command": "sleep 60"},
            call_id=call_id,
        ),
        agent_view_id=agent_view_id,
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

    # The newer run (gen 2) reuses the SAME cached executor/loop — it must survive.
    survivor_executor = MagicMock()
    survivor_executor.kill = AsyncMock()
    rt._run_resources.set_executor(CID, survivor_executor)
    survivor_loop = object()
    rt._loop_registry.bind(CID, survivor_loop)
    survivor_task = MagicMock()
    survivor_task.done.return_value = False

    def _newer_run_starts() -> None:
        assert rt._run_registry.register_task(CID, survivor_task) == 2

    assert rt._run_registry.register_task(CID, _RaceTask(_newer_run_starts)) == 1

    await rt.kill(CID)

    # The newer run's executor is NOT killed and its loop is NOT evicted.
    survivor_executor.kill.assert_not_awaited()
    assert rt._run_resources.executor(CID) is survivor_executor
    assert rt._loop_registry.loop(CID) is survivor_loop
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

    assert _advance_generation(rt) == 1

    survivor_executor = MagicMock()
    survivor_executor.kill = AsyncMock()
    survivor_loop = object()
    survivor_task = MagicMock()
    survivor_task.done.return_value = False

    async def _kill_then_newer_run() -> None:
        # A real replacement owns both local task identity and durable authority.
        intent = await store.append(
            CID,
            WorkspaceMutationEvent(
                operation="agent.run-intent.user-turn",
                run_protocol_version=1,
            ),
        )
        await store.append(
            CID,
            WorkspaceMutationEvent(
                operation="agent.view-admitted",
                run_intent_id=intent.id,
                agent_view_id="view-new",
                run_protocol_version=1,
            ),
        )
        assert rt._run_registry.register_task(CID, survivor_task) == 2
        rt._run_resources.set_executor(CID, survivor_executor)
        rt._loop_registry.bind(CID, survivor_loop)

    killed_executor = MagicMock()
    killed_executor.kill = AsyncMock(side_effect=_kill_then_newer_run)
    rt._run_resources.set_executor(CID, killed_executor)
    rt._loop_registry.bind(CID, object())

    await rt.kill(CID)

    # The killed run's OWN executor was still torn down...
    killed_executor.kill.assert_awaited_once()
    survivor_executor.kill.assert_not_awaited()
    assert rt._run_resources.executor(CID) is survivor_executor
    assert rt._loop_registry.loop(CID) is survivor_loop
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

    assert _advance_generation(rt) == 1
    rt._kernel_pins.ensure(CID)

    executor = MagicMock()
    executor.kill = AsyncMock()
    rt._run_resources.set_executor(CID, executor)
    pending = MagicMock()
    pending.destroy = AsyncMock()
    rt._run_resources.set_pending_session(CID, pending)
    rt._loop_registry.bind(CID, object())

    await rt.kill(CID)

    executor.kill.assert_awaited_once()
    pending.destroy.assert_awaited_once()
    assert rt._run_resources.executor(CID) is None
    assert rt._run_resources.pending_session(CID) is None
    assert rt._loop_registry.loop(CID) is None
    assert rt._kernel_pins.current(CID) is None
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
    rt._run_resources.set_executor(CID, executor)
    rt._loop_registry.bind(CID, object())

    await rt.kill(CID)

    executor.kill.assert_awaited_once()
    assert rt._loop_registry.loop(CID) is None
    assert _killed_detail_present(await store.get_events(CID))


async def test_kill_terminal_status_waits_for_host_workspace_fence() -> None:
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    await store.append(CID, StatusEvent(status=ConversationStatus.FINISHED))
    rt = _runtime(store)
    rt._settings._set_surface(CID, "build")
    lock = rt._workspace.lock(CID)
    await lock.acquire()

    killing = asyncio.create_task(rt._control.kill(CID))
    await asyncio.sleep(0)
    assert not killing.done()
    assert not _killed_detail_present(await store.get_events(CID))

    lock.release()
    await killing
    assert _killed_detail_present(await store.get_events(CID))


async def test_kill_accepts_current_durable_target_despite_stale_local_generation() -> None:
    store = SqliteEventStore(":memory:")
    await _seed(store)
    rt = _runtime(store)
    rt._settings._set_surface(CID, "build")
    assert _advance_generation(rt) == 1
    lock = rt._workspace.lock(CID)
    await lock.acquire()

    killing = asyncio.create_task(rt._control.kill(CID, generation=1))
    await asyncio.sleep(0)
    assert not killing.done()
    # Generation alone is process-local diagnostic state. With no replacement
    # task or durable intent/view, the queued command still targets this run.
    assert _advance_generation(rt) == 2
    lock.release()

    await killing
    assert _killed_detail_present(await store.get_events(CID))


async def test_kill_rechecks_generation_after_waiting_for_workspace_fence() -> None:
    store = SqliteEventStore(":memory:")
    await _seed(store)
    rt = _runtime(store)
    rt._settings._set_surface(CID, "build")
    assert _advance_generation(rt) == 1
    lock = rt._workspace.lock(CID)
    await lock.acquire()

    killing = asyncio.create_task(rt._control.kill(CID, generation=1))
    await asyncio.sleep(0)
    assert not killing.done()

    intent = await store.append(
        CID,
        WorkspaceMutationEvent(
            operation="agent.run-intent.user-turn",
            run_protocol_version=1,
        ),
    )
    await store.append(
        CID,
        WorkspaceMutationEvent(
            operation="agent.view-admitted",
            run_intent_id=intent.id,
            agent_view_id="view-replacement",
            run_protocol_version=1,
        ),
    )
    replacement = MagicMock()
    replacement.done.return_value = False
    assert rt._run_registry.register_task(CID, replacement) == 2
    lock.release()

    await killing
    assert not _killed_detail_present(await store.get_events(CID))


@pytest.mark.asyncio
async def test_kill_closes_dangling_action_with_cancelled_agent_error() -> None:
    store = SqliteEventStore(":memory:")
    await _seed(store)
    action = await store.append(CID, _action())
    rt = _runtime(store)
    assert _advance_generation(rt) == 1

    await rt.kill(CID)

    events = await store.get_events(CID)
    cancelled = _cancelled_errors(events, action.id)
    assert len(cancelled) == 1
    assert cancelled[0].detail is not None
    assert "outcome is UNKNOWN" in cancelled[0].detail
    assert cancelled[0].tool_call_id == action.tool_call.call_id
    assert cancelled[0].seq is not None and action.seq is not None
    assert cancelled[0].seq > action.seq


@pytest.mark.asyncio
async def test_second_kill_does_not_duplicate_cancelled_agent_error() -> None:
    store = SqliteEventStore(":memory:")
    await _seed(store)
    action = await store.append(CID, _action())
    rt = _runtime(store)
    assert _advance_generation(rt) == 1

    await rt.kill(CID)
    await rt.kill(CID)

    events = await store.get_events(CID)
    assert len(_cancelled_errors(events, action.id)) == 1


async def test_kill_closure_waits_for_real_outcome_under_shared_fence() -> None:
    """The paired-result check and closure append share the effect fence.

    A real result that owns the fence first must be observed by the queued kill;
    kill must not append a second, contradictory terminal result.
    """
    store = SqliteEventStore(":memory:")
    await _seed(store)
    action = await store.append(CID, _action(agent_view_id="view-a"))
    rt = _runtime(store)
    rt._settings._set_surface(CID, "build")
    assert _advance_generation(rt) == 1

    lock = rt._workspace.lock(CID)
    await lock.acquire()
    closing = asyncio.create_task(_close_dangling_actions(rt))
    await asyncio.sleep(0)
    assert not closing.done()

    await store.append(
        CID,
        ObservationEvent(
            action_id=action.id,
            agent_view_id=action.agent_view_id,
            tool_result=ToolResult(
                call_id=action.tool_call.call_id,
                tool_name=action.tool_call.tool_name,
                success=True,
                content="real outcome",
            ),
        ),
    )
    lock.release()

    assert await asyncio.wait_for(closing, timeout=1) == []
    events = await store.get_events(CID)
    assert _cancelled_errors(events, action.id) == []


async def test_kill_closure_preserves_strict_action_view_attribution() -> None:
    """A kill result remains paired in the strict view that emitted its action."""
    store = SqliteEventStore(":memory:")
    await _seed(store)
    intent = await store.append(
        CID,
        WorkspaceMutationEvent(
            operation="agent.run-intent.user-turn",
            run_protocol_version=1,
        ),
    )
    await store.append(
        CID,
        WorkspaceMutationEvent(
            operation="agent.view-admitted",
            run_intent_id=intent.id,
            agent_view_id="view-strict",
            run_protocol_version=1,
        ),
    )
    action = await store.append(CID, _action(agent_view_id="view-strict"))
    rt = _runtime(store)
    rt._settings._set_surface(CID, "build")
    assert _advance_generation(rt) == 1

    closures = await _close_dangling_actions(rt)
    assert len(closures) == 1
    assert closures[0].action_id == action.id
    assert closures[0].tool_call_id == action.tool_call.call_id
    assert closures[0].agent_view_id == action.agent_view_id

    terminal = await store.append(
        CID,
        StatusEvent(
            status=ConversationStatus.FINISHED,
            agent_view_id="view-strict",
        ),
    )
    terminal_seq, _ = derive_final_workspace_fence(await store.get_events(CID))
    assert terminal_seq == terminal.seq


async def test_strict_kill_status_is_bound_to_captured_view() -> None:
    store = SqliteEventStore(":memory:")
    await _seed(store)
    intent = await store.append(
        CID,
        WorkspaceMutationEvent(
            operation="agent.run-intent.user-turn",
            run_protocol_version=1,
        ),
    )
    await store.append(
        CID,
        WorkspaceMutationEvent(
            operation="agent.view-admitted",
            run_intent_id=intent.id,
            agent_view_id="view-killed",
            run_protocol_version=1,
        ),
    )
    rt = _runtime(store)
    rt._settings._set_surface(CID, "build")

    await rt._control.kill(CID)

    killed = [
        event
        for event in await store.get_events(CID)
        if isinstance(event, StatusEvent) and event.detail == "killed"
    ]
    assert len(killed) == 1
    assert killed[0].agent_view_id == "view-killed"
    assert killed[0].run_intent_id is None


async def test_cross_process_new_intent_blocks_stale_kill_teardown_and_idle(
    monkeypatch,
) -> None:
    """A peer's durable takeover is visible even when local generation is not."""
    store = SqliteEventStore(":memory:")
    await _seed(store)
    old_intent = await store.append(
        CID,
        WorkspaceMutationEvent(
            operation="agent.run-intent.user-turn",
            run_protocol_version=1,
        ),
    )
    await store.append(
        CID,
        WorkspaceMutationEvent(
            operation="agent.view-admitted",
            run_intent_id=old_intent.id,
            agent_view_id="view-old",
            run_protocol_version=1,
        ),
    )
    rt = _runtime(store)
    rt._settings._set_surface(CID, "build")

    capture_done = asyncio.Event()
    release_kill = asyncio.Event()
    original_capture = rt._lifecycle_commands.resolve_current_authority

    async def _capture_then_pause(conversation_id: str):
        authority = await original_capture(conversation_id)
        capture_done.set()
        await release_kill.wait()
        return authority

    monkeypatch.setattr(
        rt._lifecycle_commands,
        "resolve_current_authority",
        _capture_then_pause,
    )
    killing = asyncio.create_task(rt._control.kill(CID))
    await asyncio.wait_for(capture_done.wait(), timeout=1)

    # Simulate another Agent server: durable protocol advances, but this
    # process's _run_generation is unchanged.
    new_intent = await store.append(
        CID,
        WorkspaceMutationEvent(
            operation="agent.run-intent.user-turn",
            run_protocol_version=1,
        ),
    )
    await store.append(
        CID,
        WorkspaceMutationEvent(
            operation="agent.view-admitted",
            run_intent_id=new_intent.id,
            agent_view_id="view-peer",
            run_protocol_version=1,
        ),
    )
    peer_executor = MagicMock()
    peer_executor.kill = AsyncMock()
    rt._run_resources.set_executor(CID, peer_executor)
    release_kill.set()

    await asyncio.wait_for(killing, timeout=1)
    peer_executor.kill.assert_not_awaited()
    assert rt._run_resources.executor(CID) is peer_executor
    assert not _killed_detail_present(await store.get_events(CID))


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
    assert _advance_generation(rt) == 1

    await rt.kill(CID)

    events = await store.get_events(CID)
    assert _cancelled_errors(events, action.id) == []


@pytest.mark.asyncio
async def test_event_chain_oracle_passes_killed_sequence_after_pair_recovery() -> None:
    store = SqliteEventStore(":memory:")
    await _seed(store)
    await store.append(CID, _action())
    rt = _runtime(store)
    assert _advance_generation(rt) == 1

    await rt.kill(CID)

    events = [e.model_dump(mode="json") for e in await store.get_events(CID)]
    scenario = {
        "id": "kill-recovery",
        "assertions": {"event_chain": {"require_action_observation_pairs": True}},
    }
    results = EventChainOracle().check(normalize_events(events), scenario=scenario)
    assert all(r.passed for r in results), [r.to_dict() for r in results]
