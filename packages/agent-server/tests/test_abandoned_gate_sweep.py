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
    rt._connections[cid] = 1  # UI attached

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
    rt._tasks[active] = task
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
