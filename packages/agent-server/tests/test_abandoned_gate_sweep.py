"""P-C — reap conversations abandoned at an AWAITING_* gate.

The idle sweep only frees a gated conversation's SANDBOX; it never resolves the
gate, so a plan/decision/question the user walked away from lingers as an open
gate forever. ``sweep_abandoned_gates_once`` reaps the genuinely-abandoned ones
(no UI connected, gate state, last event older than a long TTL) → STUCK, while
leaving active/recent gates and non-gate conversations untouched.
"""

import asyncio
import contextlib
import os
from unittest.mock import MagicMock

import pytest
from disco.agent_server import ConversationRuntime
from disco.core import (
    ConversationStatus,
    EventSource,
    LLMMessage,
    MessageEvent,
    SqliteEventStore,
    StatusEvent,
)
from disco.tools import ProcessSandboxService


def _runtime(store: SqliteEventStore) -> ConversationRuntime:
    router = MagicMock()
    svc = ProcessSandboxService()
    return ConversationRuntime(store, router=router, sandbox_service=svc)


async def _gated_conv(store: SqliteEventStore, cid: str, status: ConversationStatus) -> str:
    await store.append(
        cid,
        MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content="hi")),
    )
    await store.append(cid, StatusEvent(status=status))
    return cid


_GATE_PARAMS = [
    ConversationStatus.AWAITING_PLAN_APPROVAL,
    ConversationStatus.AWAITING_USER_DECISION,
    ConversationStatus.AWAITING_USER_QUESTION,
    ConversationStatus.WAITING_FOR_CONFIRMATION,
]


def _advance_generation(rt: ConversationRuntime, cid: str, target: int) -> None:
    """Advance a run generation through the registry's public operations."""

    while (rt._run_registry.generation(cid) or 0) < target:
        task = MagicMock()
        task.done.return_value = True
        rt._run_registry.register_task(cid, task)
        assert rt._run_registry.complete_task(cid, task)


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", _GATE_PARAMS)
async def test_abandoned_gate_past_ttl_is_reaped(gate, monkeypatch):
    """A gate with no UI connected, older than the TTL (0 here), reaps to STUCK
    with a user-facing note appended."""
    store = SqliteEventStore(":memory:")
    rt = _runtime(store)
    cid = await _gated_conv(store, f"conv_{gate.value}", gate)

    monkeypatch.setenv("DISCO_ABANDONED_GATE_TTL_S", "0")
    reaped = await rt.sweep_abandoned_gates_once()

    assert reaped == 1
    state = await store.get_state(cid)
    assert state.execution_status is ConversationStatus.STUCK
    # A user-visible note explains the closeout.
    events = await store.get_events(cid)
    assert any(
        isinstance(e, MessageEvent)
        and e.source is EventSource.ENVIRONMENT
        and "left" in (e.message.content or "")
        for e in events
    )


@pytest.mark.asyncio
async def test_recent_gate_not_reaped():
    """A fresh gate (last event ~now) under the generous default TTL is NOT
    reaped — only genuinely-abandoned ones are."""
    store = SqliteEventStore(":memory:")
    rt = _runtime(store)
    cid = await _gated_conv(store, "conv_recent", ConversationStatus.AWAITING_PLAN_APPROVAL)

    # No env override → default 86400s TTL; a just-created conv is way under it.
    os.environ.pop("DISCO_ABANDONED_GATE_TTL_S", None)
    os.environ.pop("PMX_ABANDONED_GATE_TTL_S", None)
    reaped = await rt.sweep_abandoned_gates_once()

    assert reaped == 0
    state = await store.get_state(cid)
    assert state.execution_status is ConversationStatus.AWAITING_PLAN_APPROVAL


@pytest.mark.asyncio
async def test_connected_gate_not_reaped_even_past_ttl(monkeypatch):
    """A gate with a live UI connection is being watched — never reaped, even
    past the TTL (someone is actively looking at it)."""
    store = SqliteEventStore(":memory:")
    rt = _runtime(store)
    cid = await _gated_conv(store, "conv_connected", ConversationStatus.AWAITING_USER_DECISION)
    rt.connections.on_connect(cid)  # UI attached

    monkeypatch.setenv("DISCO_ABANDONED_GATE_TTL_S", "0")
    reaped = await rt.sweep_abandoned_gates_once()

    assert reaped == 0
    state = await store.get_state(cid)
    assert state.execution_status is ConversationStatus.AWAITING_USER_DECISION


@pytest.mark.asyncio
async def test_active_run_gate_not_reaped_even_past_ttl(monkeypatch):
    """A gate WITH a live in-memory run task — e.g. just resumed, a decision in
    flight, a background step executing — is NEVER reaped even past the TTL: its
    last persisted event can be stale while the run is mid-flight, so reaping on
    status+age alone would corrupt the live run. A genuinely dormant gate (no
    live task) sitting alongside it IS still reaped."""
    store = SqliteEventStore(":memory:")
    rt = _runtime(store)
    active = await _gated_conv(store, "conv_active_run", ConversationStatus.AWAITING_USER_DECISION)
    dormant = await _gated_conv(store, "conv_dormant", ConversationStatus.AWAITING_USER_DECISION)

    # Register a LIVE (not-done) run task for the active conversation — the same
    # ground truth running_conversation_ids() reads.
    async def _never() -> None:
        await asyncio.Event().wait()

    task = asyncio.ensure_future(_never())
    rt._run_registry.register_task(active, task)  # type: ignore[arg-type]
    try:
        assert active in rt.running_conversation_ids()

        monkeypatch.setenv("DISCO_ABANDONED_GATE_TTL_S", "0")
        reaped = await rt.sweep_abandoned_gates_once()

        # Only the dormant one is reaped; the live run is untouched.
        assert reaped == 1
        assert (await store.get_state(active)).execution_status is (
            ConversationStatus.AWAITING_USER_DECISION
        )
        assert (await store.get_state(dormant)).execution_status is ConversationStatus.STUCK
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_newer_run_reusing_pin_blocks_stale_reap(monkeypatch):
    """Finding #3 (third path): the reaper reads gate state / liveness, then AWAITS
    event reads before appending terminal STUCK + clearing the pin. If a NEWER run
    starts in that window (a fresh message resumes the gate → `kick` bumps the
    run-generation and REUSES the pin), the stale reaper must NOT append STUCK into
    the newer run's log nor clear the newer run's pin. We simulate the newer run by
    bumping `_run_generation[cid]` during the reaper's `get_events` await (after it
    captured the generation, before its terminal append)."""
    store = SqliteEventStore(":memory:")
    rt = _runtime(store)
    cid = await _gated_conv(store, "conv_gen_race", ConversationStatus.AWAITING_PLAN_APPROVAL)

    # The gate-creating run pinned a kernel and recorded its run-generation.
    sentinel_kernel = object()
    rt._kernel_pin_store.set(cid, sentinel_kernel)
    _advance_generation(rt, cid, 5)

    # Inject the race: a newer run kicks (generation 5 → 6, pin reused) precisely in
    # the async window the reaper captured-generation-then-awaits.
    real_get_events = store.get_events
    fired = {"done": False}

    async def _get_events_then_newer_run(conv_id, *a, **k):
        result = await real_get_events(conv_id, *a, **k)
        if conv_id == cid and not fired["done"]:
            fired["done"] = True
            _advance_generation(rt, cid, 6)  # a newer run now owns the conversation
        return result

    monkeypatch.setattr(store, "get_events", _get_events_then_newer_run)
    monkeypatch.setenv("DISCO_ABANDONED_GATE_TTL_S", "0")

    reaped = await rt.sweep_abandoned_gates_once()

    # Stale reap is skipped: no STUCK in the NEWER run's log.
    assert reaped == 0
    assert (await store.get_state(cid)).execution_status is (
        ConversationStatus.AWAITING_PLAN_APPROVAL
    )
    # The NEWER run's pin is left intact (that run owns it now).
    assert rt._kernel_pin_store.current(cid) is sentinel_kernel
    assert rt._run_registry.generation(cid) == 6


@pytest.mark.asyncio
async def test_genuinely_abandoned_gate_terminalizes_and_unpins(monkeypatch):
    """The non-race path: a genuinely-abandoned gate (no newer run — its
    run-generation is unchanged by reap time) still terminalizes to STUCK AND
    releases its kernel pin via `_unpin_if_current_generation` (the generation
    matches, so the pin clears), so an abandoned gated run never leaks its pin."""
    store = SqliteEventStore(":memory:")
    rt = _runtime(store)
    cid = await _gated_conv(
        store, "conv_abandoned_unpin", ConversationStatus.AWAITING_USER_DECISION
    )
    sentinel_kernel = object()
    rt._kernel_pin_store.set(cid, sentinel_kernel)
    _advance_generation(rt, cid, 3)  # the gate-creating run's generation, never bumped

    monkeypatch.setenv("DISCO_ABANDONED_GATE_TTL_S", "0")
    reaped = await rt.sweep_abandoned_gates_once()

    assert reaped == 1
    assert (await store.get_state(cid)).execution_status is ConversationStatus.STUCK
    # Terminal reap releases the pin too (same generation → cleared).
    assert rt._kernel_pin_store.current(cid) is None


@pytest.mark.asyncio
async def test_non_gate_conversations_never_reaped(monkeypatch):
    """RUNNING / FINISHED / IDLE conversations are not gates — even past the TTL
    the gate sweep leaves them alone (RUNNING is handled by the stranded sweep)."""
    store = SqliteEventStore(":memory:")
    rt = _runtime(store)
    running = await _gated_conv(store, "conv_running", ConversationStatus.RUNNING)
    finished = await _gated_conv(store, "conv_finished", ConversationStatus.FINISHED)

    monkeypatch.setenv("DISCO_ABANDONED_GATE_TTL_S", "0")
    reaped = await rt.sweep_abandoned_gates_once()

    assert reaped == 0
    assert (await store.get_state(running)).execution_status is ConversationStatus.RUNNING
    assert (await store.get_state(finished)).execution_status is ConversationStatus.FINISHED
