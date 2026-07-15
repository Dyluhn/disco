"""W2 — request/task SUPERVISION contract (the silent-hang AMPLIFIER fix).

W1 fixed the *trigger* (a missing-file sandbox read now raises a FileNotFoundError the
bookkeeping handlers catch). W2 is the *backstop*: even when SOME future unhandled
exception escapes ``loop.run()``, the conversation must terminate in a durable ``ERROR``
status — never sit at ``RUNNING`` forever (the silent hang Dylan hit). And cancellation
must NOT be reported as an error. Terminalization must be idempotent.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid

import pytest
from disco.agent_server import ConversationRuntime
from disco.core import SqliteEventStore
from disco.core.events import (
    ConversationStatus,
    EventSource,
    LLMMessage,
    MessageEvent,
    StatusEvent,
)
from disco.core.llm import DefaultLLMRouter, ModelEntry, ProposedToolCall, RouterConfig
from disco.tools import ProcessSandboxService

from .scripted_model import ScriptedProvider

pytestmark = pytest.mark.boundary_contract

_ENTRY = ModelEntry(model_id="scripted-m", provider="scripted", context_window=8192)
_CFG = RouterConfig(models={"scripted-m": _ENTRY}, default_model="scripted-m")
# A single trivial step so ScriptedProvider constructs — the loop is monkeypatched away in
# every test here (we inject crashing/cancelling/finishing loops), so it's never executed.
_STEP = [("noop", [ProposedToolCall(tool_name="finish", arguments={})])]


def _runtime(store: SqliteEventStore) -> ConversationRuntime:
    return ConversationRuntime(
        store,
        router=DefaultLLMRouter(_CFG, {"scripted": ScriptedProvider(_STEP)}),
        sandbox_service=ProcessSandboxService(),
    )


async def _kicked_conversation(store, runtime) -> str:
    cid = f"sup-{uuid.uuid4().hex[:8]}"
    store.create_conversation(cid, owner_id="local", surface="build")
    runtime.set_surface(cid, "build")
    runtime.set_artifact_mode(cid, True)
    await store.append(
        cid,
        MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content="go")),
    )
    return cid


class _BoomLoop:
    """A loop whose run() raises an exception that escapes the harness (simulates the
    bookkeeping/sandbox exceptions that used to crash the task silently)."""

    async def run(self):
        raise RuntimeError("boom: an unhandled exception escaped the loop")


class _CancelLoop:
    async def run(self):
        await asyncio.sleep(3600)  # block until cancelled


async def test_uncaught_loop_exception_terminalizes_as_error(monkeypatch):
    store = SqliteEventStore(":memory:")
    runtime = _runtime(store)
    cid = await _kicked_conversation(store, runtime)
    monkeypatch.setattr(runtime, "_loop_for", lambda _c: _BoomLoop())

    runtime.kick(cid)
    task = runtime._tasks.get(cid)
    assert task is not None
    with contextlib.suppress(Exception):
        await asyncio.wait_for(asyncio.shield(task), timeout=10)
    await asyncio.sleep(0.2)  # let the supervisor's terminalize task run

    state = await store.get_state(cid)
    assert state.execution_status == ConversationStatus.ERROR, (
        "crashed run must end ERROR, not hang RUNNING"
    )
    events = await store.get_events(cid)
    assert any(
        isinstance(e, StatusEvent) and e.status == ConversationStatus.ERROR for e in events
    ), "a terminal ERROR StatusEvent must be visible to the UI"


async def test_cancellation_is_not_reported_as_error(monkeypatch):
    store = SqliteEventStore(":memory:")
    runtime = _runtime(store)
    cid = await _kicked_conversation(store, runtime)
    monkeypatch.setattr(runtime, "_loop_for", lambda _c: _CancelLoop())

    runtime.kick(cid)
    task = runtime._tasks.get(cid)
    assert task is not None
    await asyncio.sleep(0.1)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task
    await asyncio.sleep(0.2)

    events = await store.get_events(cid)
    assert not any(
        isinstance(e, StatusEvent) and e.status == ConversationStatus.ERROR for e in events
    ), "a cancelled run must NOT be reported as ERROR"


async def test_terminalize_is_idempotent(monkeypatch):
    """If the run already concluded (e.g. FINISHED) before the callback, don't overwrite."""
    store = SqliteEventStore(":memory:")
    runtime = _runtime(store)
    cid = await _kicked_conversation(store, runtime)

    class _FinishThenRaise:
        async def run(self):
            await store.append(cid, StatusEvent(status=ConversationStatus.FINISHED))
            raise RuntimeError("late crash after finishing")

    monkeypatch.setattr(runtime, "_loop_for", lambda _c: _FinishThenRaise())
    runtime.kick(cid)
    task = runtime._tasks.get(cid)
    with contextlib.suppress(Exception):
        await asyncio.wait_for(asyncio.shield(task), timeout=10)
    await asyncio.sleep(0.2)

    state = await store.get_state(cid)
    assert state.execution_status == ConversationStatus.FINISHED, (
        "must not clobber an already-terminal status"
    )


# --- W11: clean RETURN at RUNNING (the silent MiniMax-build stall) -------------------
#
# B-C root cause: loop.run() can RETURN without raising while the conversation status
# is still RUNNING (a dropped/unparseable model response ends the turn with no terminal
# StatusEvent). W2 only caught EXCEPTIONS, so this sat at RUNNING forever — no gate, no
# terminal status, no recovery (only an operator steer un-wedged it). W11 reconciles a
# clean non-terminal return: re-kick once, else mark STUCK so the wedge is VISIBLE.


async def _drain_until(store, cid, predicate, *, ticks=40, dt=0.05):
    for _ in range(ticks):
        await asyncio.sleep(dt)
        if predicate(await store.get_state(cid)):
            return await store.get_state(cid)
    return await store.get_state(cid)


async def test_clean_return_at_running_recovers_then_stucks(monkeypatch):
    """A run that keeps returning at RUNNING is re-kicked ONCE, then terminalized STUCK."""
    store = SqliteEventStore(":memory:")
    runtime = _runtime(store)
    cid = await _kicked_conversation(store, runtime)

    calls = {"n": 0}

    class _ReturnAtRunning:
        async def run(self):
            calls["n"] += 1
            await store.append(cid, StatusEvent(status=ConversationStatus.RUNNING))
            return await store.get_state(cid)

    monkeypatch.setattr(runtime, "_loop_for", lambda _c: _ReturnAtRunning())

    runtime.kick(cid)
    state = await _drain_until(store, cid, lambda s: s.execution_status == ConversationStatus.STUCK)

    assert state.execution_status == ConversationStatus.STUCK, (
        "a run that ends at RUNNING without concluding must become visibly STUCK"
    )
    assert calls["n"] >= 2, "must re-kick once (transient-drop recovery) before giving up"
    events = await store.get_events(cid)
    assert any(
        isinstance(e, StatusEvent)
        and e.status == ConversationStatus.STUCK
        and e.detail == "loop ended without reaching a terminal state"
        for e in events
    ), "the STUCK status must carry an honest no-terminal-state reason"


async def test_clean_return_at_running_transient_recovers_without_stuck(monkeypatch):
    """The FIRST return stalls at RUNNING; the re-kick FINISHES — no STUCK is emitted."""
    store = SqliteEventStore(":memory:")
    runtime = _runtime(store)
    cid = await _kicked_conversation(store, runtime)

    seq = {"n": 0}

    class _TransientStall:
        async def run(self):
            seq["n"] += 1
            status = ConversationStatus.RUNNING if seq["n"] == 1 else ConversationStatus.FINISHED
            await store.append(cid, StatusEvent(status=status))
            return await store.get_state(cid)

    monkeypatch.setattr(runtime, "_loop_for", lambda _c: _TransientStall())

    runtime.kick(cid)
    state = await _drain_until(
        store, cid, lambda s: s.execution_status == ConversationStatus.FINISHED
    )

    assert state.execution_status == ConversationStatus.FINISHED, "the re-kick must recover"
    events = await store.get_events(cid)
    assert not any(
        isinstance(e, StatusEvent) and e.status == ConversationStatus.STUCK for e in events
    ), "a transient stall that recovers on re-kick must NOT be marked STUCK"


async def test_clean_return_at_paused_is_left_alone(monkeypatch):
    """A legitimate parked return (PAUSED/awaiting) must NOT be re-kicked or clobbered."""
    store = SqliteEventStore(":memory:")
    runtime = _runtime(store)
    cid = await _kicked_conversation(store, runtime)

    calls = {"n": 0}

    class _Park:
        async def run(self):
            calls["n"] += 1
            await store.append(cid, StatusEvent(status=ConversationStatus.PAUSED))
            return await store.get_state(cid)

    monkeypatch.setattr(runtime, "_loop_for", lambda _c: _Park())

    runtime.kick(cid)
    await asyncio.sleep(0.4)

    state = await store.get_state(cid)
    assert state.execution_status == ConversationStatus.PAUSED, "a parked run is honored"
    assert calls["n"] == 1, "a legitimately parked return must not be re-kicked"


async def test_stranded_running_swept_to_recovery(monkeypatch):
    """The idle-sweep watchdog detects a RUNNING conversation with no live task and
    routes it through recovery (re-kick → STUCK), so a lost done-callback self-heals."""
    store = SqliteEventStore(":memory:")
    runtime = _runtime(store)
    cid = await _kicked_conversation(store, runtime)

    class _ReturnAtRunning:
        async def run(self):
            await store.append(cid, StatusEvent(status=ConversationStatus.RUNNING))
            return await store.get_state(cid)

    monkeypatch.setattr(runtime, "_loop_for", lambda _c: _ReturnAtRunning())
    # Simulate a stranded conversation: a loop this process owns, status RUNNING in the
    # store, but NO live task in self._tasks (the done-callback never fired).
    runtime._loops[cid] = _ReturnAtRunning()  # type: ignore[assignment]
    await store.append(cid, StatusEvent(status=ConversationStatus.RUNNING))

    acted = await runtime.sweep_stranded_runs_once()
    assert acted == 1, "the stranded RUNNING conversation must be detected"

    state = await _drain_until(store, cid, lambda s: s.execution_status == ConversationStatus.STUCK)
    assert state.execution_status == ConversationStatus.STUCK, (
        "a stranded RUNNING run must self-heal to a visible terminal status"
    )
