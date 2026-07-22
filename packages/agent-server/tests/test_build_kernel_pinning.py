"""BuildKernel per-run pinning.

The public Build entry points route through `_kernel_for` and pin the resolved
kernel for a run. With one active implementation this still matters: start/send,
steer, resume, and gate controls all go through the same protocol object, and
legacy persisted values degrade to Disco without splitting a run.
"""

from __future__ import annotations

import asyncio
import types
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from disco.agent_server.build_kernel import DiscoKernel
from disco.agent_server.runtime import ConversationRuntime
from disco.core import (
    ConversationStatus,
    Event,
    EventSource,
    SqliteEventStore,
    WorkspaceMutationEvent,
)

CID = "conv-pin-test"


@pytest.fixture
def store() -> SqliteEventStore:
    s = SqliteEventStore(":memory:")
    s.create_conversation(CID, owner_id="local")
    return s


def _runtime(store: SqliteEventStore, *, build_kernel: str = "disco") -> types.SimpleNamespace:
    """A fake carrying only the collaborators the pinning seam touches."""
    fake = types.SimpleNamespace()
    fake._store = store
    fake._tasks = {}
    fake._pinned_kernels = {}
    fake._run_generation = {}
    lock = asyncio.Lock()
    fake.workspace_lock = lambda _conversation_id: lock
    fake._workspace = MagicMock()

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

    fake._workspace.interprocess_mutation_fence = process_fence
    fake._workspace.append_run_ingress_locked = append_run_ingress
    fake._emit_toolscope_audit_summary = MagicMock()
    fake.kick = MagicMock()
    # CONTRACT-ACTIVATE: send_user_turn declares the contract from the brief
    # before kernel dispatch — a no-op collaborator here (pinning seam only).
    fake.activate_contract_for_brief = MagicMock()
    # CONTRACT-DURABILITY: send_user_turn folds any persisted declaration first —
    # same no-op collaborator treatment (the fold is proven in
    # test_build_contract_activation.py, not at this pinning seam).
    fake._fold_contract_from_history = AsyncMock()

    control = MagicMock()
    control.confirm = AsyncMock()
    control.approve_plan = AsyncMock()
    control.reject = AsyncMock()
    control.request_plan = AsyncMock()
    control.kill = AsyncMock()
    fake._control = control

    loop = MagicMock()
    loop.pick_alternative = AsyncMock()
    fake._loop = loop
    fake._loop_for = MagicMock(return_value=loop)

    cfg = MagicMock()
    cfg.build_kernel = build_kernel
    config_store = MagicMock()
    config_store.load.return_value = cfg
    fake._config_store = config_store

    fake._disco_kernel = DiscoKernel(fake)

    for name in (
        "_kernel_for",
        "_ensure_kernel_pinned",
        "_clear_pinned_kernel",
        "_unpin_if_current_generation",
        "_run_continuing_control",
        "start",
        "send_user_turn",
        "confirm",
        "reject",
        "approve_plan",
        "request_plan",
        "pick_alternative",
        "kill",
    ):
        setattr(fake, name, types.MethodType(getattr(ConversationRuntime, name), fake))
    return fake


async def test_send_user_turn_appends_and_kicks_and_returns_stored(
    store: SqliteEventStore,
) -> None:
    rt = _runtime(store)
    stored = await rt.send_user_turn(CID, "build a site", context="big ctx")

    events = [e for e in await store.get_events(CID) if hasattr(e, "message")]
    assert len(events) == 2
    ctx, user = events
    assert ctx.source == EventSource.ENVIRONMENT and ctx.message.content == "big ctx"
    assert user.source == EventSource.USER and user.message.content == "build a site"
    rt.kick.assert_called_once_with(CID, claimed_user_seq=user.seq)
    assert stored.id == user.id and stored.seq == user.seq
    assert rt._pinned_kernels[CID] is rt._disco_kernel


def test_start_routes_to_kick_and_pins(store: SqliteEventStore) -> None:
    rt = _runtime(store)
    rt.start(CID)
    rt.kick.assert_called_once_with(CID)
    assert rt._pinned_kernels[CID] is rt._disco_kernel


@pytest.mark.parametrize("legacy", ["pi_experimental", "pi", "garbage"])
def test_legacy_kernel_values_pin_disco(legacy: str, store: SqliteEventStore) -> None:
    rt = _runtime(store, build_kernel=legacy)
    rt.start(CID)
    assert rt._pinned_kernels[CID] is rt._disco_kernel


async def test_pin_survives_midrun_config_change(store: SqliteEventStore) -> None:
    rt = _runtime(store, build_kernel="disco")

    await rt.send_user_turn(CID, "build it")
    assert rt._pinned_kernels[CID] is rt._disco_kernel

    rt._config_store.load.return_value.build_kernel = "pi_experimental"

    await rt.confirm(CID)
    rt._control.confirm.assert_awaited_once_with(CID)
    await rt.approve_plan(CID)
    rt._control.approve_plan.assert_awaited_once_with(CID)
    assert rt._pinned_kernels[CID] is rt._disco_kernel


async def test_midrun_steer_reuses_pin(store: SqliteEventStore) -> None:
    rt = _runtime(store)
    await rt.send_user_turn(CID, "first")
    rt._config_store.load.return_value.build_kernel = "pi_experimental"

    await rt.send_user_turn(CID, "steer me", steer=True)

    assert rt._pinned_kernels[CID] is rt._disco_kernel
    msgs = [e for e in await store.get_events(CID) if hasattr(e, "message")]
    assert msgs[-1].meta == {"steer": True}


async def test_every_control_op_pins_when_no_pin_exists(store: SqliteEventStore) -> None:
    for op, check in (
        (lambda rt: rt.reject(CID, "no"), lambda rt: rt._control.reject),
        (lambda rt: rt.approve_plan(CID), lambda rt: rt._control.approve_plan),
        (lambda rt: rt.request_plan(CID, "again"), lambda rt: rt._control.request_plan),
        (lambda rt: rt.pick_alternative(CID, "opt-1"), lambda rt: rt._loop.pick_alternative),
    ):
        rt = _runtime(store)
        assert CID not in rt._pinned_kernels
        await op(rt)
        assert rt._pinned_kernels[CID] is rt._disco_kernel
        check(rt).assert_awaited_once()


async def test_control_op_raise_rolls_back_freshly_created_pin(
    store: SqliteEventStore,
) -> None:
    rt = _runtime(store)
    raising = MagicMock()
    raising.confirm = AsyncMock(side_effect=RuntimeError("kernel down"))
    rt._disco_kernel = raising

    with pytest.raises(RuntimeError):
        await rt.confirm(CID)

    assert CID not in rt._pinned_kernels
    rt._control.confirm.assert_not_awaited()


async def test_control_op_raise_preserves_a_preexisting_pin(
    store: SqliteEventStore,
) -> None:
    rt = _runtime(store)
    raising = MagicMock()
    raising.confirm = AsyncMock(side_effect=RuntimeError("boom"))
    rt._pinned_kernels[CID] = raising

    with pytest.raises(RuntimeError):
        await rt.confirm(CID)

    assert rt._pinned_kernels[CID] is raising


def test_clear_pin_lets_next_run_reresolve_to_disco(store: SqliteEventStore) -> None:
    rt = _runtime(store)
    rt.start(CID)
    assert rt._pinned_kernels[CID] is rt._disco_kernel

    rt._clear_pinned_kernel(CID)
    assert CID not in rt._pinned_kernels

    rt._config_store.load.return_value.build_kernel = "pi_experimental"
    assert rt._ensure_kernel_pinned(CID) is rt._disco_kernel


async def test_kill_clears_pin(store: SqliteEventStore) -> None:
    rt = _runtime(store)
    rt.start(CID)
    await rt.kill(CID)
    rt._control.kill.assert_awaited_once_with(CID, None)
    assert CID not in rt._pinned_kernels


def _finalize_fake(store: SqliteEventStore) -> types.SimpleNamespace:
    fake = types.SimpleNamespace()
    fake._store = store
    fake._pinned_kernels = {}
    fake._run_generation = {}
    fake._last_status = {}
    fake._nonterminal_rekicks = {}
    fake._last_rekick_progress_seq = {}  # STUCK-fix watermark, popped at finalize

    async def _no_auto_resume(cid, status, generation):
        return False

    fake._maybe_auto_resume_actionless_pause = _no_auto_resume
    fake._post_terminal_rekick_seq = {}
    fake._run_claimed_user_seq = {}
    fake.kick = MagicMock()
    fake._emit_persistence_reminder = AsyncMock()
    fake._emit_toolscope_audit_summary = MagicMock()

    # [F2 watchdog fix] STUCK terminalization appends the terminal status
    # through the real run-authority-aware helper `_append_task_status_if_current`
    # (bound below); with no view/run-intent authority it delegates to
    # `_workspace.append_status`. Provide a faithful workspace whose append_status
    # DURABLY writes to the store so the STUCK projection is real.
    # (Removed the prior `_maybe_shadow_fold_finished_manifest` stub: dead
    # scaffolding for a renamed-away method never called on the finalize path.)
    async def _append_status(conversation_id: str, event: Event) -> None:
        await store.append(conversation_id, event)

    fake._workspace = types.SimpleNamespace(append_status=_append_status)
    for attr in (
        "_CONCLUDED_STATUSES",
        "_RUN_PARKED_STATUSES",
        "_KERNEL_UNPIN_STATUSES",
        "_MAX_NONTERMINAL_REKICKS",
    ):
        setattr(fake, attr, getattr(ConversationRuntime, attr))
    for name in (
        "_finalize_clean_return",
        "_append_task_status_if_current",
        "_maybe_rekick_for_stranded_followup",
        "_clear_pinned_kernel",
        "_unpin_if_current_generation",
    ):
        setattr(fake, name, types.MethodType(getattr(ConversationRuntime, name), fake))
    return fake


async def test_stuck_watchdog_clears_pin(store: SqliteEventStore) -> None:
    from disco.core import StatusEvent

    await store.append(CID, StatusEvent(status=ConversationStatus.RUNNING))
    rt = _finalize_fake(store)
    rt._pinned_kernels[CID] = object()
    rt._nonterminal_rekicks[CID] = ConversationRuntime._MAX_NONTERMINAL_REKICKS

    await rt._finalize_clean_return(CID)

    state = await store.get_state(CID)
    assert state.execution_status == ConversationStatus.STUCK
    assert CID not in rt._pinned_kernels
    rt._emit_persistence_reminder.assert_awaited()


async def test_finalize_after_new_run_started_does_not_clear_new_pin(
    store: SqliteEventStore,
) -> None:
    from disco.core import StatusEvent

    await store.append(CID, StatusEvent(status=ConversationStatus.FINISHED))
    rt = _finalize_fake(store)
    pin = object()
    rt._pinned_kernels[CID] = pin
    rt._run_generation[CID] = 2

    await rt._finalize_clean_return(CID, generation=1)
    assert rt._pinned_kernels[CID] is pin

    await rt._finalize_clean_return(CID, generation=2)
    assert CID not in rt._pinned_kernels


def test_unpin_statuses_exclude_paused_and_gates() -> None:
    s = ConversationRuntime._KERNEL_UNPIN_STATUSES
    assert ConversationStatus.PAUSED not in s
    assert ConversationStatus.AWAITING_PLAN_APPROVAL not in s
    assert ConversationStatus.WAITING_FOR_CONFIRMATION not in s
    assert ConversationStatus.FINISHED in s
    assert ConversationStatus.ERROR in s
    assert ConversationStatus.STUCK in s
