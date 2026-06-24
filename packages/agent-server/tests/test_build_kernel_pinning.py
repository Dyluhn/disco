"""BuildKernel per-run pinning — Disco Pi Build Kernel Campaign, codex finding #1.

The seam used to be half-wired: `start`/`send_message`/`steer` kicked the loop
DIRECTLY while the plan/action-gate ops routed through `_kernel_for`. With the
experimental gate on and `pi_experimental` selected, a Disco loop could start via
`kick()` while a later `confirm`/`approve_plan` resolved to `PiKernel` and raised
mid-run — a half-disco/half-pi run.

The fix: the start/send/steer entry points route THROUGH the selected kernel and
PIN it on the conversation before the first append/kick; every later control op
reuses the pinned kernel (`_kernel_for` returns the pin), so a run can never be
split across kernels even if Settings / the gate change mid-run. The pin is
released on a terminal ending (or kill) so the NEXT run re-resolves the selection.

These tests bind the SHIPPED runtime methods onto a lightweight fake (the same
technique as `test_build_kernel_interface`) so the real routing/pinning code is
exercised, not a copy.
"""

from __future__ import annotations

import types
from unittest.mock import AsyncMock, MagicMock

import pytest
from disco.agent_server.build_kernel import DiscoKernel, PiKernel
from disco.agent_server.runtime import ConversationRuntime
from disco.core import ConversationStatus, EventSource, SqliteEventStore

CID = "conv-pin-test"
EXP_ENV = "DISCO_PI_KERNEL_EXPERIMENTAL"


@pytest.fixture
def store() -> SqliteEventStore:
    s = SqliteEventStore(":memory:")
    s.create_conversation(CID, owner_id="local")
    return s


def _runtime(store: SqliteEventStore, *, build_kernel: str = "disco") -> types.SimpleNamespace:
    """A fake carrying ONLY the collaborators the pinning seam touches, with the
    REAL runtime methods bound so the shipped routing/pinning is under test."""
    fake = types.SimpleNamespace()
    fake._store = store
    fake._tasks = {}
    fake._pinned_kernels = {}
    fake.kick = MagicMock()

    control = MagicMock()
    control.confirm = AsyncMock()
    control.approve_plan = AsyncMock()
    control.kill = AsyncMock()
    fake._control = control

    cfg = MagicMock()
    cfg.build_kernel = build_kernel
    config_store = MagicMock()
    config_store.load.return_value = cfg
    fake._config_store = config_store

    fake._disco_kernel = DiscoKernel(fake)
    fake._pi_kernel = PiKernel(fake)

    for name in (
        "_kernel_for",
        "_ensure_kernel_pinned",
        "_clear_pinned_kernel",
        "start",
        "send_user_turn",
        "confirm",
        "approve_plan",
        "kill",
    ):
        setattr(fake, name, types.MethodType(getattr(ConversationRuntime, name), fake))
    return fake


# ---- disco path is byte-identical -------------------------------------------


async def test_send_user_turn_disco_appends_and_kicks_and_returns_stored(
    monkeypatch: pytest.MonkeyPatch, store: SqliteEventStore
) -> None:
    monkeypatch.delenv(EXP_ENV, raising=False)
    rt = _runtime(store, build_kernel="disco")
    stored = await rt.send_user_turn(CID, "build a site", context="big ctx")

    events = [e for e in await store.get_events(CID) if hasattr(e, "message")]
    assert len(events) == 2
    ctx, user = events
    assert ctx.source == EventSource.ENVIRONMENT and ctx.message.content == "big ctx"
    assert user.source == EventSource.USER and user.message.content == "build a site"
    rt.kick.assert_called_once_with(CID)
    # The REST send/followup routes report id/seq off the returned user message.
    assert stored.id == user.id and stored.seq == user.seq


def test_start_disco_routes_to_kick(
    monkeypatch: pytest.MonkeyPatch, store: SqliteEventStore
) -> None:
    monkeypatch.delenv(EXP_ENV, raising=False)
    rt = _runtime(store, build_kernel="disco")
    rt.start(CID)
    rt.kick.assert_called_once_with(CID)
    assert rt._pinned_kernels[CID] is rt._disco_kernel  # pinned at start


# ---- a run pins ONE kernel for its lifetime ---------------------------------


async def test_pin_survives_midrun_gate_flip_no_half_run(
    monkeypatch: pytest.MonkeyPatch, store: SqliteEventStore
) -> None:
    """The core finding-#1 guarantee: a run started under `disco` keeps routing its
    control ops to the PINNED disco kernel even if Settings flip to pi_experimental
    AND the gate turns on mid-run — never a half-disco/half-pi run."""
    monkeypatch.delenv(EXP_ENV, raising=False)
    rt = _runtime(store, build_kernel="disco")

    await rt.send_user_turn(CID, "build it")  # pins disco
    assert rt._pinned_kernels[CID] is rt._disco_kernel

    # Mid-run: the user switches Settings to pi AND an operator flips the gate on.
    rt._config_store.load.return_value.build_kernel = "pi_experimental"
    monkeypatch.setenv(EXP_ENV, "1")

    await rt.confirm(CID)  # must land on the PINNED disco control op, NOT pi
    rt._control.confirm.assert_awaited_once_with(CID)
    await rt.approve_plan(CID)
    rt._control.approve_plan.assert_awaited_once_with(CID)


async def test_midrun_steer_reuses_pin(
    monkeypatch: pytest.MonkeyPatch, store: SqliteEventStore
) -> None:
    monkeypatch.delenv(EXP_ENV, raising=False)
    rt = _runtime(store, build_kernel="disco")
    await rt.send_user_turn(CID, "first")
    rt._config_store.load.return_value.build_kernel = "pi_experimental"
    monkeypatch.setenv(EXP_ENV, "1")
    # A mid-run steer keeps the disco pin (no re-resolve) → still appends + kicks.
    await rt.send_user_turn(CID, "steer me", steer=True)
    assert rt._pinned_kernels[CID] is rt._disco_kernel
    msgs = [e for e in await store.get_events(CID) if hasattr(e, "message")]
    assert msgs[-1].meta == {"steer": True}


# ---- selecting pi cannot produce a half-run; it fails cleanly ----------------


async def test_pi_selected_and_gated_on_fails_clean_no_append(
    monkeypatch: pytest.MonkeyPatch, store: SqliteEventStore
) -> None:
    """With pi selected AND the gate on, the entry point pins pi and the stub raises
    BEFORE any append — no partial run is created (zero events appended, no kick)."""
    monkeypatch.setenv(EXP_ENV, "1")
    rt = _runtime(store, build_kernel="pi_experimental")
    with pytest.raises(NotImplementedError):
        await rt.send_user_turn(CID, "build it")
    assert [e for e in await store.get_events(CID) if hasattr(e, "message")] == []
    rt.kick.assert_not_called()


# ---- the pin is released on a terminal ending / kill ------------------------


def test_clear_pin_lets_next_run_reresolve(
    monkeypatch: pytest.MonkeyPatch, store: SqliteEventStore
) -> None:
    monkeypatch.delenv(EXP_ENV, raising=False)
    rt = _runtime(store, build_kernel="disco")
    rt.start(CID)
    assert rt._pinned_kernels[CID] is rt._disco_kernel

    rt._clear_pinned_kernel(CID)  # what a terminal ending / kill does
    assert CID not in rt._pinned_kernels

    # Next run re-resolves the CURRENT selection (now pi, gated on).
    rt._config_store.load.return_value.build_kernel = "pi_experimental"
    monkeypatch.setenv(EXP_ENV, "1")
    assert rt._ensure_kernel_pinned(CID) is rt._pi_kernel


async def test_kill_clears_pin(monkeypatch: pytest.MonkeyPatch, store: SqliteEventStore) -> None:
    monkeypatch.delenv(EXP_ENV, raising=False)
    rt = _runtime(store, build_kernel="disco")
    rt.start(CID)
    await rt.kill(CID)
    rt._control.kill.assert_awaited_once_with(CID)
    assert CID not in rt._pinned_kernels


# ---- finding #2: a kernel that RAISES on start/send must not LEAK a pin ------


async def test_pi_send_raises_leaves_no_pin_so_next_reresolves(
    monkeypatch: pytest.MonkeyPatch, store: SqliteEventStore
) -> None:
    """The pin commits only once the kernel call SUCCEEDS (finding #2): a pi start/send
    that raises before any append must leave NO pin — otherwise the failed selection
    stays pinned forever and a later gate/config flip is ineffective. After the raise
    the conversation is unpinned, so the next attempt re-resolves the CURRENT
    selection (here: gate flipped off → disco)."""
    monkeypatch.setenv(EXP_ENV, "1")
    rt = _runtime(store, build_kernel="pi_experimental")
    with pytest.raises(NotImplementedError):
        await rt.send_user_turn(CID, "build it")
    assert CID not in rt._pinned_kernels  # rolled back — no leaked pin
    # Next attempt re-resolves: operator flips the gate OFF → disco wins.
    monkeypatch.delenv(EXP_ENV, raising=False)
    rt._config_store.load.return_value.build_kernel = "disco"
    assert rt._ensure_kernel_pinned(CID) is rt._disco_kernel


def test_pi_start_raises_leaves_no_pin(
    monkeypatch: pytest.MonkeyPatch, store: SqliteEventStore
) -> None:
    """Same rollback guarantee on the sync `start` entry point."""
    monkeypatch.setenv(EXP_ENV, "1")
    rt = _runtime(store, build_kernel="pi_experimental")
    with pytest.raises(NotImplementedError):
        rt.start(CID)
    assert CID not in rt._pinned_kernels


async def test_raise_does_not_roll_back_a_preexisting_pin(
    monkeypatch: pytest.MonkeyPatch, store: SqliteEventStore
) -> None:
    """A raise during a STEER of an already-running (pinned) run must NOT clear the
    pin — that live run is still on its kernel; only a FRESHLY-created pin is rolled
    back. Simulated with a pre-pinned kernel whose send raises."""
    monkeypatch.delenv(EXP_ENV, raising=False)
    rt = _runtime(store, build_kernel="disco")

    raising = MagicMock()
    raising.send_user_turn = AsyncMock(side_effect=RuntimeError("boom"))
    rt._pinned_kernels[CID] = raising  # a live run already pinned to this kernel

    with pytest.raises(RuntimeError):
        await rt.send_user_turn(CID, "steer me", steer=True)
    assert rt._pinned_kernels[CID] is raising  # pin preserved across the failure


# ---- finding #3: the STUCK watchdog path must release the pin ----------------


def _finalize_fake(store: SqliteEventStore):
    """Minimal fake carrying the collaborators `_finalize_clean_return` touches, with
    the REAL method + `_clear_pinned_kernel` bound so the shipped watchdog code runs."""
    fake = types.SimpleNamespace()
    fake._store = store
    fake._pinned_kernels = {}
    fake._last_status = {}
    fake._nonterminal_rekicks = {}
    fake.kick = MagicMock()
    fake._emit_persistence_reminder = AsyncMock()
    for attr in (
        "_CONCLUDED_STATUSES",
        "_RUN_PARKED_STATUSES",
        "_KERNEL_UNPIN_STATUSES",
        "_MAX_NONTERMINAL_REKICKS",
    ):
        setattr(fake, attr, getattr(ConversationRuntime, attr))
    for name in ("_finalize_clean_return", "_clear_pinned_kernel"):
        setattr(fake, name, types.MethodType(getattr(ConversationRuntime, name), fake))
    return fake


async def test_stuck_watchdog_clears_pin(store: SqliteEventStore) -> None:
    """Finding #3: when the clean-return watchdog marks a wedged RUNNING run STUCK, it
    appends a FRESH terminal status — so it must clear the kernel pin itself (the
    healthy-return branch unpins via `_KERNEL_UNPIN_STATUSES`, but this branch doesn't
    pass through it). Else a wedged run leaks its pin forever."""
    from disco.core import StatusEvent

    await store.append(CID, StatusEvent(status=ConversationStatus.RUNNING))
    rt = _finalize_fake(store)
    rt._pinned_kernels[CID] = object()  # a run pinned to some kernel
    # Already re-kicked once → the next non-terminal finalize takes the STUCK branch.
    rt._nonterminal_rekicks[CID] = ConversationRuntime._MAX_NONTERMINAL_REKICKS

    await rt._finalize_clean_return(CID)

    state = await store.get_state(CID)
    assert state.execution_status == ConversationStatus.STUCK
    assert CID not in rt._pinned_kernels  # pin released on the STUCK terminalization
    rt._emit_persistence_reminder.assert_awaited()


def test_unpin_statuses_exclude_paused_and_gates() -> None:
    """A PAUSED run or a gate-park must KEEP its pin (resume/approve continues under
    the same kernel); only terminal/idle statuses release it."""
    s = ConversationRuntime._KERNEL_UNPIN_STATUSES
    assert ConversationStatus.PAUSED not in s
    assert ConversationStatus.AWAITING_PLAN_APPROVAL not in s
    assert ConversationStatus.WAITING_FOR_CONFIRMATION not in s
    assert ConversationStatus.FINISHED in s
    assert ConversationStatus.ERROR in s
    assert ConversationStatus.STUCK in s
