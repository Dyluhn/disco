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
    assert (
        state.execution_status == ConversationStatus.ERROR
    ), "crashed run must end ERROR, not hang RUNNING"
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
    assert (
        state.execution_status == ConversationStatus.FINISHED
    ), "must not clobber an already-terminal status"
