"""BuildKernel per-run pinning.
The explicit control owner preserves one kernel identity for each run.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from disco.agent_server import run_supervisor as _run_supervisor
from disco.agent_server.build_kernel import BuildKernel
from disco.agent_server.run_registry import KernelPinRegistry
from disco.agent_server.runtime import ConversationRuntime
from disco.core import (
    ConversationState,
    ConversationStatus,
    Event,
    EventSource,
    SqliteEventStore,
    StatusEvent,
    WorkspaceMutationEvent,
)

CID = "conv-pin-test"


@pytest.fixture
def store() -> SqliteEventStore:
    result = SqliteEventStore(":memory:")
    result.create_conversation(CID, owner_id="local")
    return result


def _runtime(store: SqliteEventStore) -> ConversationRuntime:
    """Use the production composition graph while isolating the workspace fence."""

    runtime = ConversationRuntime(store)

    @asynccontextmanager
    async def process_fence(_conversation_id: str) -> AsyncIterator[None]:
        yield

    async def append_run_ingress(
        conversation_id: str,
        events: list[Event],
        source: str,
    ) -> list[Event]:
        return await store.append_many(
            conversation_id,
            [*events, WorkspaceMutationEvent(operation=f"agent.run-intent.{source}")],
        )

    runtime._workspace.interprocess_mutation_fence = process_fence
    runtime._workspace.append_run_ingress_locked = append_run_ingress
    runtime._workspace.claim_registered_run_locked = MagicMock()
    runtime._run_controller.kick = MagicMock()
    runtime._control.confirm = AsyncMock()
    runtime._control.approve_plan = AsyncMock()
    runtime._control.reject = AsyncMock()
    runtime._control.request_plan = AsyncMock()
    runtime._control.kill = AsyncMock()
    loop = MagicMock()
    loop.pick_alternative = AsyncMock()
    runtime._loop_factory.loop_for = MagicMock(return_value=loop)
    runtime._test_loop = loop
    return runtime


def _set_legacy_kernel_value(runtime: ConversationRuntime, value: str) -> None:
    config_store = MagicMock()
    config_store.load.return_value.build_kernel = value
    runtime._config_store = config_store


async def test_send_user_turn_appends_and_kicks_and_returns_stored(
    store: SqliteEventStore,
) -> None:
    runtime = _runtime(store)
    stored = await runtime.send_user_turn(CID, "build a site", context="big ctx")

    events = [event for event in await store.get_events(CID) if hasattr(event, "message")]
    assert len(events) == 2
    context, user = events
    assert context.source == EventSource.ENVIRONMENT
    assert context.message.content == "big ctx"
    assert user.source == EventSource.USER
    assert user.message.content == "build a site"
    runtime._run_controller.kick.assert_called_once_with(CID, claimed_user_seq=user.seq)
    assert stored.id == user.id and stored.seq == user.seq
    assert runtime._kernel_pins.current(CID) is runtime._disco_kernel


def test_start_routes_to_kick_and_pins(store: SqliteEventStore) -> None:
    runtime = _runtime(store)
    runtime.start(CID)
    runtime._run_controller.kick.assert_called_once_with(CID)
    assert runtime._kernel_pins.current(CID) is runtime._disco_kernel


async def test_pin_survives_later_controls(store: SqliteEventStore) -> None:
    runtime = _runtime(store)
    await runtime.send_user_turn(CID, "build it")
    pinned = runtime._kernel_pins.current(CID)

    await runtime.conversation_control.confirm(CID)
    runtime._control.confirm.assert_awaited_once_with(CID)
    await runtime.conversation_control.approve_plan(CID)
    runtime._control.approve_plan.assert_awaited_once_with(CID)
    assert runtime._kernel_pins.current(CID) is pinned is runtime._disco_kernel


async def test_midrun_steer_reuses_pin(store: SqliteEventStore) -> None:
    runtime = _runtime(store)
    await runtime.send_user_turn(CID, "first")
    pinned = runtime._kernel_pins.current(CID)

    await runtime.send_user_turn(CID, "steer me", steer=True)

    assert runtime._kernel_pins.current(CID) is pinned
    messages = [event for event in await store.get_events(CID) if hasattr(event, "message")]
    assert messages[-1].meta == {"steer": True}


async def test_pin_survives_midrun_config_change(store: SqliteEventStore) -> None:
    runtime = _runtime(store)
    await runtime.send_user_turn(CID, "first")
    pinned = runtime._kernel_pins.current(CID)

    _set_legacy_kernel_value(runtime, "pi_experimental")
    await runtime.conversation_control.confirm(CID)

    assert runtime._kernel_pins.current(CID) is pinned is runtime._disco_kernel


@pytest.mark.parametrize("legacy", ["pi_experimental", "pi", "garbage"])
def test_legacy_kernel_values_pin_disco(
    legacy: str,
    store: SqliteEventStore,
) -> None:
    runtime = _runtime(store)
    _set_legacy_kernel_value(runtime, legacy)

    runtime.start(CID)

    assert runtime._kernel_pins.current(CID) is runtime._disco_kernel


async def test_every_control_op_pins_when_no_pin_exists(store: SqliteEventStore) -> None:
    for op, check in (
        (
            lambda runtime: runtime.conversation_control.reject(CID, "no"),
            lambda runtime: runtime._control.reject,
        ),
        (
            lambda runtime: runtime.conversation_control.approve_plan(CID),
            lambda runtime: runtime._control.approve_plan,
        ),
        (
            lambda runtime: runtime.conversation_control.request_plan(CID, "again"),
            lambda runtime: runtime._control.request_plan,
        ),
        (
            lambda runtime: runtime.conversation_control.pick_alternative(CID, "opt-1"),
            lambda runtime: runtime._test_loop.pick_alternative,
        ),
    ):
        runtime = _runtime(store)
        assert runtime._kernel_pins.current(CID) is None
        await op(runtime)
        assert runtime._kernel_pins.current(CID) is runtime._disco_kernel
        check(runtime).assert_awaited_once()


async def test_control_op_raise_rolls_back_freshly_created_pin(
    store: SqliteEventStore,
) -> None:
    runtime = _runtime(store)
    runtime._control.confirm = AsyncMock(side_effect=RuntimeError("kernel down"))

    with pytest.raises(RuntimeError, match="kernel down"):
        await runtime.conversation_control.confirm(CID)

    assert runtime._kernel_pins.current(CID) is None


async def test_control_op_raise_preserves_a_preexisting_pin(
    store: SqliteEventStore,
) -> None:
    runtime = _runtime(store)
    runtime.start(CID)
    pinned = runtime._kernel_pins.current(CID)
    runtime._control.confirm = AsyncMock(side_effect=RuntimeError("boom"))

    with pytest.raises(RuntimeError, match="boom"):
        await runtime.conversation_control.confirm(CID)

    assert runtime._kernel_pins.current(CID) is pinned


def test_selector_change_does_not_replace_pin_until_clear() -> None:
    first = MagicMock(spec=BuildKernel)
    second = MagicMock(spec=BuildKernel)

    class MutableSelector:
        selected: BuildKernel = first

        def select_kernel(self, _conversation_id: str) -> BuildKernel:
            return self.selected

    selector = MutableSelector()
    pins = KernelPinRegistry(selector)

    assert pins.ensure(CID) is first
    selector.selected = second
    assert pins.ensure(CID) is first

    pins.clear(CID)
    assert pins.ensure(CID) is second


def test_clear_pin_lets_next_run_reresolve_to_disco(store: SqliteEventStore) -> None:
    runtime = _runtime(store)
    runtime.start(CID)
    assert runtime._kernel_pins.current(CID) is runtime._disco_kernel

    runtime._kernel_pins.clear(CID)
    _set_legacy_kernel_value(runtime, "pi_experimental")
    runtime.start(CID)

    assert runtime._kernel_pins.current(CID) is runtime._disco_kernel


async def test_kill_clears_pin(store: SqliteEventStore) -> None:
    runtime = _runtime(store)
    runtime.start(CID)
    await runtime.kill(CID)
    runtime._control.kill.assert_awaited_once_with(CID, None)
    assert runtime._kernel_pins.current(CID) is None


def _finalizer_runtime(store: SqliteEventStore) -> ConversationRuntime:
    runtime = _runtime(store)

    async def append_task_status_if_current(
        conversation_id: str,
        event: Event,
        **_kwargs: Any,
    ) -> Event:
        return await store.append(conversation_id, event)

    runtime._lifecycle_commands.append_task_status_if_current = (
        append_task_status_if_current
    )
    runtime._run_finalizer._observability.emit_reminder = AsyncMock()
    return runtime


async def test_stuck_watchdog_clears_pin(store: SqliteEventStore) -> None:
    await store.append(CID, StatusEvent(status=ConversationStatus.RUNNING))
    runtime = _finalizer_runtime(store)
    runtime._kernel_pins.ensure(CID)
    for _ in range(_run_supervisor._MAX_NONTERMINAL_REKICKS):
        runtime._run_recovery.increment_stall(CID)

    await runtime._run_finalizer.finalize_clean(CID)

    state = await store.get_state(CID)
    assert state.execution_status == ConversationStatus.STUCK
    assert runtime._kernel_pins.current(CID) is None
    runtime._run_finalizer._observability.emit_reminder.assert_awaited()


async def _registered_generation(runtime: ConversationRuntime) -> int:
    async def state() -> ConversationState:
        return await runtime._store.get_state(CID)

    task = asyncio.create_task(state())
    generation = runtime._run_registry.register_task(CID, task)
    await task
    assert runtime._run_registry.complete_task(CID, task)
    return generation


async def test_finalize_after_new_run_started_does_not_clear_new_pin(
    store: SqliteEventStore,
) -> None:
    await store.append(CID, StatusEvent(status=ConversationStatus.FINISHED))
    runtime = _finalizer_runtime(store)
    runtime._kernel_pins.ensure(CID)
    generation_1 = await _registered_generation(runtime)
    generation_2 = await _registered_generation(runtime)

    await runtime._run_finalizer.finalize_clean(CID, generation=generation_1)
    assert runtime._kernel_pins.current(CID) is runtime._disco_kernel

    await runtime._run_finalizer.finalize_clean(CID, generation=generation_2)
    assert runtime._kernel_pins.current(CID) is None


def test_unpin_statuses_exclude_paused_and_gates() -> None:
    assert ConversationStatus.PAUSED not in _run_supervisor._KERNEL_UNPIN_STATUSES
    assert (
        ConversationStatus.AWAITING_PLAN_APPROVAL
        not in _run_supervisor._KERNEL_UNPIN_STATUSES
    )
    assert (
        ConversationStatus.WAITING_FOR_CONFIRMATION
        not in _run_supervisor._KERNEL_UNPIN_STATUSES
    )
    assert ConversationStatus.FINISHED in _run_supervisor._KERNEL_UNPIN_STATUSES
    assert ConversationStatus.ERROR in _run_supervisor._KERNEL_UNPIN_STATUSES
    assert ConversationStatus.STUCK in _run_supervisor._KERNEL_UNPIN_STATUSES
