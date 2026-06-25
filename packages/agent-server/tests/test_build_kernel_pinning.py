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
    fake._run_generation = {}
    fake.kick = MagicMock()

    control = MagicMock()
    control.confirm = AsyncMock()
    control.approve_plan = AsyncMock()
    control.reject = AsyncMock()
    control.request_plan = AsyncMock()
    control.kill = AsyncMock()
    fake._control = control
    # pick_alternative routes through DiscoKernel → runtime._loop_for(cid).pick_alternative
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
    fake._pi_kernel = PiKernel(fake)

    for name in (
        "_kernel_for",
        "_ensure_kernel_pinned",
        "_clear_pinned_kernel",
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


# ---- finding #1 (deeper): control ops on an UNPINNED run must pin, not bypass --


async def test_confirm_pins_when_no_pin_exists(
    monkeypatch: pytest.MonkeyPatch, store: SqliteEventStore
) -> None:
    """A control op on a conversation with NO in-memory pin (after a restart, a legacy
    pre-A1 gated conversation, or a direct unpinned kick) must RESOLVE + PIN the kernel
    — not re-resolve transiently. Else a later op could land on a different kernel."""
    monkeypatch.delenv(EXP_ENV, raising=False)
    rt = _runtime(store, build_kernel="disco")
    assert CID not in rt._pinned_kernels  # no pin (e.g. fresh process after a restart)

    await rt.confirm(CID)
    assert rt._pinned_kernels[CID] is rt._disco_kernel  # pinned by the control op
    rt._control.confirm.assert_awaited_once_with(CID)


async def test_every_control_op_pins_when_no_pin_exists(
    monkeypatch: pytest.MonkeyPatch, store: SqliteEventStore
) -> None:
    """EVERY run-continuing control op (confirm/reject/approve_plan/request_plan/
    pick_alternative) pins an unpinned conversation through the SAME seam."""
    monkeypatch.delenv(EXP_ENV, raising=False)
    for op, check in (
        (lambda rt: rt.reject(CID, "no"), lambda rt: rt._control.reject),
        (lambda rt: rt.approve_plan(CID), lambda rt: rt._control.approve_plan),
        (lambda rt: rt.request_plan(CID, "again"), lambda rt: rt._control.request_plan),
        (lambda rt: rt.pick_alternative(CID, "opt-1"), lambda rt: rt._loop.pick_alternative),
    ):
        rt = _runtime(store, build_kernel="disco")
        assert CID not in rt._pinned_kernels
        await op(rt)
        assert rt._pinned_kernels[CID] is rt._disco_kernel
        check(rt).assert_awaited_once()


async def test_pick_alternative_routes_through_pinned_kernel(
    monkeypatch: pytest.MonkeyPatch, store: SqliteEventStore
) -> None:
    """`pick_alternative` used to bypass the kernel entirely (`_loop_for` direct). It now
    routes through the pinned DiscoKernel, which performs the SAME loop call — byte-
    identical for disco, but pinned so the run can't be split."""
    monkeypatch.delenv(EXP_ENV, raising=False)
    rt = _runtime(store, build_kernel="disco")
    await rt.send_user_turn(CID, "build it")  # pins disco
    await rt.pick_alternative(CID, "opt-A")
    rt._loop.pick_alternative.assert_awaited_once_with("opt-A")
    assert rt._pinned_kernels[CID] is rt._disco_kernel


async def test_control_op_on_pi_gated_on_rolls_back_freshly_created_pin(
    monkeypatch: pytest.MonkeyPatch, store: SqliteEventStore
) -> None:
    """A control op that FRESHLY pins pi (gate on) and then raises in the stub must roll
    the pin back (finding #2 semantics), so the conversation is re-resolvable — not stuck
    pinned to a kernel that can't run."""
    monkeypatch.setenv(EXP_ENV, "1")
    rt = _runtime(store, build_kernel="pi_experimental")
    assert CID not in rt._pinned_kernels
    with pytest.raises(NotImplementedError):
        await rt.confirm(CID)
    assert CID not in rt._pinned_kernels  # rolled back — no leaked pin
    rt._control.confirm.assert_not_awaited()


async def test_control_op_raise_preserves_a_preexisting_pin(
    monkeypatch: pytest.MonkeyPatch, store: SqliteEventStore
) -> None:
    """A raise during a control op on an ALREADY-pinned (live) run must NOT clear the pin
    — only a freshly-created pin is rolled back."""
    monkeypatch.delenv(EXP_ENV, raising=False)
    rt = _runtime(store, build_kernel="disco")
    raising = MagicMock()
    raising.confirm = AsyncMock(side_effect=RuntimeError("boom"))
    rt._pinned_kernels[CID] = raising  # a live run already pinned
    with pytest.raises(RuntimeError):
        await rt.confirm(CID)
    assert rt._pinned_kernels[CID] is raising  # preserved across the failure


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
    fake._run_generation = {}
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
    for name in (
        "_finalize_clean_return",
        "_clear_pinned_kernel",
        "_unpin_if_current_generation",
    ):
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


async def test_finalize_after_new_run_started_does_not_clear_the_new_runs_pin(
    store: SqliteEventStore,
) -> None:
    """Finding #3 (the pin set/clear RACE): `_on_run_task_done` schedules
    `_finalize_clean_return` ASYNC. Before it runs, a fresh user turn can reuse the pin
    and `kick` a NEW run (bumping the run-generation). The STALE finalizer (carrying the
    OLD generation) must NOT clear the pin out from under the newer run.

    Simulated: the conversation is at a terminal FINISHED (run A), pinned, but a newer
    run B has already started (run_generation == 2). The finalizer for generation 1 runs
    and must LEAVE the pin (it belongs to run B now)."""
    from disco.core import ConversationStatus, StatusEvent

    await store.append(CID, StatusEvent(status=ConversationStatus.FINISHED))
    rt = _finalize_fake(store)
    pin = object()
    rt._pinned_kernels[CID] = pin
    rt._run_generation[CID] = 2  # a NEWER run (gen 2) already reused the pin

    # The stale finalizer for the OLD run (generation 1) — must not clear gen-2's pin.
    await rt._finalize_clean_return(CID, generation=1)
    assert rt._pinned_kernels[CID] is pin  # SURVIVES — newer run keeps its pin

    # The finalizer for the CURRENT generation DOES clear it (the run really ended).
    await rt._finalize_clean_return(CID, generation=2)
    assert CID not in rt._pinned_kernels


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


# ---- finding #3: the CRASH terminalizer path (symmetric with the clean path) -------


def _crash_fake(store: SqliteEventStore):
    """Minimal fake carrying the collaborators `_terminalize_crashed` touches, with the
    REAL method + the pin helpers bound so the SHIPPED crash-terminalization runs."""
    fake = types.SimpleNamespace()
    fake._store = store
    fake._pinned_kernels = {}
    fake._run_generation = {}
    fake._emit_persistence_reminder = AsyncMock()
    fake._CONCLUDED_STATUSES = ConversationRuntime._CONCLUDED_STATUSES
    for name in (
        "_terminalize_crashed",
        "_clear_pinned_kernel",
        "_unpin_if_current_generation",
    ):
        setattr(fake, name, types.MethodType(getattr(ConversationRuntime, name), fake))
    return fake


async def test_crash_terminalize_after_new_run_started_does_not_corrupt_it(
    store: SqliteEventStore,
) -> None:
    """Finding #3 (crash path made SYMMETRIC with the clean path): `_on_run_task_done`
    pops the run's task then schedules `_terminalize_crashed` ASYNC. Before it runs, a
    fresh user turn can start a NEWER run (bumping the run-generation) and REUSE the pin.
    The STALE crash terminalizer (carrying the OLD generation) must NOT append ERROR into
    the newer run's event log NOR clear the newer run's pin.

    Simulated: generation 1 crashed, but generation 2 has already started (run_generation
    == 2, status RUNNING, pinned). The crash terminalizer for generation 1 runs and must
    be a complete no-op."""
    from disco.core import StatusEvent

    # Generation 2 is the live run: RUNNING + pinned.
    await store.append(CID, StatusEvent(status=ConversationStatus.RUNNING))
    rt = _crash_fake(store)
    pin = object()
    rt._pinned_kernels[CID] = pin
    rt._run_generation[CID] = 2  # a NEWER run (gen 2) already reused the pin

    # The stale crash terminalizer for the OLD run (generation 1).
    await rt._terminalize_crashed(CID, RuntimeError("gen-1 boom"), 1)

    state = await store.get_state(CID)
    assert state.execution_status == ConversationStatus.RUNNING  # NO stale ERROR appended
    assert rt._pinned_kernels[CID] is pin  # gen-2's pin SURVIVES
    rt._emit_persistence_reminder.assert_not_awaited()  # no stale reminder either


async def test_crash_terminalize_with_no_newer_generation_terminalizes_and_unpins(
    store: SqliteEventStore,
) -> None:
    """A normal crash (NO newer generation) still terminalizes to ERROR and releases the
    pin, exactly as before the generation guard was added — both for a matching generation
    and for the legacy `generation=None` caller (the stranded-run sweep)."""
    from disco.core import StatusEvent

    # Matching generation: the crash IS the current run.
    await store.append(CID, StatusEvent(status=ConversationStatus.RUNNING))
    rt = _crash_fake(store)
    rt._pinned_kernels[CID] = object()
    rt._run_generation[CID] = 1

    await rt._terminalize_crashed(CID, RuntimeError("boom"), 1)

    state = await store.get_state(CID)
    assert state.execution_status == ConversationStatus.ERROR
    err = [
        e
        for e in await store.get_events(CID)
        if getattr(e, "status", None) == ConversationStatus.ERROR
    ]
    assert err and "RuntimeError" in (err[-1].detail or "")  # exception named in detail
    assert CID not in rt._pinned_kernels  # pin released on terminalization
    rt._emit_persistence_reminder.assert_awaited()

    # Legacy/stranded caller (generation=None) → unconditional terminalize, as before.
    cid2 = "conv-crash-legacy"
    store.create_conversation(cid2, owner_id="local")
    await store.append(cid2, StatusEvent(status=ConversationStatus.RUNNING))
    rt._pinned_kernels[cid2] = object()
    rt._run_generation[cid2] = 7  # irrelevant — None caller skips the generation check

    await rt._terminalize_crashed(cid2, RuntimeError("legacy boom"), None)

    state2 = await store.get_state(cid2)
    assert state2.execution_status == ConversationStatus.ERROR
    assert cid2 not in rt._pinned_kernels
