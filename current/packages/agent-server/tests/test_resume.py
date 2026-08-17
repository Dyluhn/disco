"""BP-12 — First-class Build resume: unit tests.

Tests the legality matrix, exactly-once environment message, and double-resume
race condition. Uses the same hermetic scripted-model harness as test_build_surface.

Legality tests inject events directly (no live loop) so they stay fast. Tests that
kick the loop cancel the background task after asserting return values.
"""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from disco.agent_server import ConversationRuntime, create_app
from disco.core import (
    ActionEvent,
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
)
from disco.core.events import PlanEvent, PlanStep
from disco.core.llm import (
    CompletionResponse,
    DefaultLLMRouter,
    ModelEntry,
    ProposedToolCall,
    RouterConfig,
    StreamChunk,
    TokenUsage,
)
from disco.tools import ProcessSandboxService

CID = "conv_test_resume_cid"


# ---- shared scripted model ---------------------------------------------------


class _ScriptedProvider:
    name = "fake"

    def __init__(self, steps) -> None:
        self._steps = list(steps)
        self.calls = 0

    async def complete(self, req, *, model):
        i = min(self.calls, len(self._steps) - 1)
        self.calls += 1
        text, tcs = self._steps[i]
        return CompletionResponse(
            text=text,
            tool_calls=list(tcs),
            usage=TokenUsage(input_tokens=1, output_tokens=1),
            finish_reason="stop",
            model_used=model,
            request_id=req.request_id,
            routing=None,
        )

    async def stream_complete(self, req, *, model):
        yield StreamChunk(done=True, final=await self.complete(req, model=model))

    def supports(self, requirement, *, model):
        return True


def _plan_event() -> PlanEvent:
    return PlanEvent(summary="scripted plan", steps=[], revision=1)


def _plan(steps: list[str]) -> ProposedToolCall:
    return ProposedToolCall(
        tool_name="submit_plan",
        arguments={"summary": "scripted", "steps": [{"title": s} for s in steps]},
    )


def _finish(summary: str = "done") -> ProposedToolCall:
    return ProposedToolCall(tool_name="finish", arguments={"summary": summary})


def _runtime(store: SqliteEventStore, steps=None) -> ConversationRuntime:
    if steps is None:
        steps = [("done", [_finish()])]
    cfg = RouterConfig(
        models={"m": ModelEntry(model_id="m", provider="fake", context_window=8192)},
        default_model="m",
    )
    router = DefaultLLMRouter(cfg, {"fake": _ScriptedProvider(steps)})
    return ConversationRuntime(store, router=router, sandbox_service=ProcessSandboxService())


def _user(content: str) -> MessageEvent:
    return MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content=content))


async def _cancel_task(rt: ConversationRuntime) -> None:
    """Cancel the live loop task so tests don't hang after kicking."""
    task = rt.run_registry.task(CID)
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


# ---- legality matrix ---------------------------------------------------------
# These tests inject events directly (no live loop) and cancel the kicked task.


async def test_resume_from_paused_is_legal():
    """PAUSED → resume_conversation → ok=True."""
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store)
    rt.settings._set_surface(CID, "build")
    await store.append(CID, _user("build it"))
    await store.append(CID, StatusEvent(status=ConversationStatus.PAUSED))

    result = await rt._resume.resume_conversation(CID)
    await _cancel_task(rt)

    assert result["ok"] is True
    assert result["status"] == "RUNNING"


async def test_resume_running_flip_waits_for_workspace_mutation_fence(monkeypatch):
    """A host mutation owns the head until it has resealed it.

    Resume may reconstruct context while that work is in flight, but it must not
    publish RUNNING (or start the loop) before the permanent workspace fence is
    released.
    """
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store)
    rt.settings._set_surface(CID, "build")
    await store.append(CID, _user("build it"))
    await store.append(CID, StatusEvent(status=ConversationStatus.PAUSED))
    monkeypatch.setattr(rt._resume._run_start, "start", MagicMock())

    underlying = rt.workspace.lock(CID)
    entered = asyncio.Event()

    class _ObservedFence:
        def locked(self) -> bool:
            return underlying.locked()

        async def __aenter__(self):
            entered.set()
            await underlying.acquire()
            return underlying

        async def __aexit__(self, *_exc) -> None:
            underlying.release()

    monkeypatch.setattr(rt._resume._workspace, "lock", lambda _cid: _ObservedFence())
    await underlying.acquire()
    resume = asyncio.create_task(rt._resume.resume_conversation(CID))
    await asyncio.wait_for(entered.wait(), timeout=1)

    while_held = await store.get_events(CID)
    assert not any(
        isinstance(event, StatusEvent)
        and event.status is ConversationStatus.RUNNING
        and event.detail == "resumed"
        for event in while_held
    )
    assert not resume.done()
    rt._resume._run_start.start.assert_not_called()

    underlying.release()
    assert await resume == {"ok": True, "status": "RUNNING"}
    after_release = await store.get_events(CID)
    assert any(
        isinstance(event, StatusEvent)
        and event.status is ConversationStatus.RUNNING
        and event.detail == "resumed"
        for event in after_release
    )
    rt._resume._run_start.start.assert_called_once_with(CID)


async def test_resume_pins_the_kernel_via_start(monkeypatch):
    """Finding #2: a resume must route through the PINNED `start` path, not a raw
    `kick`. Otherwise a resumed run starts UNPINNED. Here: a PAUSED build resume pins
    the disco kernel and triggers it via `kick` (the disco kernel's `start`)."""
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store)
    rt.settings._set_surface(CID, "build")
    await store.append(CID, _user("build it"))
    await store.append(CID, StatusEvent(status=ConversationStatus.PAUSED))
    # Stub kick so the resume does not spawn a real run (whose finalize could clear the
    # pin mid-assert) — we only assert the pin/trigger wiring.
    monkeypatch.setattr(rt.run_controller, "kick", MagicMock())

    assert rt._kernel_pin_store.current(CID) is None  # no pin before resume
    result = await rt._resume.resume_conversation(CID)

    assert result["ok"] is True
    assert rt._kernel_pin_store.current(CID) is rt._disco_kernel  # resume pinned via start
    rt.run_controller.kick.assert_called_once_with(CID)  # disco kernel start → kick = the trigger


async def test_resume_routes_to_the_pinned_selected_kernel_not_silently_disco():
    """Finding #2: a PAUSED gate-park keeps its kernel pin, so a resume must drive the
    trigger THROUGH that pinned (selected) kernel — proven here with a sentinel standing
    in for the selected experimental kernel. A raw `kick` would have ignored it and run
    the disco loop instead."""
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store)
    rt.settings._set_surface(CID, "build")
    await store.append(CID, _user("build it"))
    await store.append(CID, StatusEvent(status=ConversationStatus.PAUSED))
    sentinel = MagicMock()  # the SELECTED kernel pinned to this paused run
    rt._kernel_pin_store._pins[CID] = sentinel

    result = await rt._resume.resume_conversation(CID)
    await _cancel_task(rt)

    assert result["ok"] is True
    sentinel.start.assert_called_once_with(CID)  # routed through the pinned kernel
    assert rt._kernel_pin_store.current(CID) is sentinel  # pin preserved across resume


async def test_resume_from_idle_with_plan_is_legal():
    """IDLE + unfinished plan → resume_conversation → ok=True."""
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store)
    rt.settings._set_surface(CID, "build")
    await store.append(CID, _user("build it"))
    await store.append(CID, _plan_event())
    await store.append(CID, StatusEvent(status=ConversationStatus.IDLE))

    result = await rt._resume.resume_conversation(CID)
    await _cancel_task(rt)

    assert result["ok"] is True
    assert result["status"] == "RUNNING"


async def test_resume_from_idle_without_plan_is_illegal():
    """IDLE with no plan → illegal (no work to resume)."""
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store)
    rt.settings._set_surface(CID, "build")
    # No plan event — conversation was never started.

    result = await rt._resume.resume_conversation(CID)

    assert result["ok"] is False
    assert "illegal_state" in result["reason"]


async def test_resume_from_running_is_409():
    """RUNNING → resume_conversation → already_running."""
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store)
    rt.settings._set_surface(CID, "build")
    await store.append(CID, _user("build it"))
    await store.append(CID, StatusEvent(status=ConversationStatus.RUNNING))

    result = await rt._resume.resume_conversation(CID)

    assert result["ok"] is False
    assert result["reason"] == "already_running"


async def test_resume_from_finished_is_409():
    """FINISHED → resume_conversation → conversation_finished."""
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store)
    rt.settings._set_surface(CID, "build")
    await store.append(CID, _user("build it"))
    await store.append(CID, StatusEvent(status=ConversationStatus.FINISHED))

    result = await rt._resume.resume_conversation(CID)

    assert result["ok"] is False
    assert result["reason"] == "conversation_finished"


async def _seed_first_actionless_pause(store: SqliteEventStore, cid: str) -> None:
    store.create_conversation(cid, owner_id="local", surface="build")
    await store.append(cid, _user("build it"))
    await store.append(cid, PlanEvent(summary="p", steps=[{"title": "one"}], revision=1))
    await store.append(
        cid,
        StatusEvent(status=ConversationStatus.RUNNING, detail="plan_approved"),
    )
    tool_call = ToolCall(tool_name="shell", arguments={"command": "echo built"})
    action = await store.append(
        cid,
        ActionEvent(
            thought="do work",
            tool_call=tool_call,
        ),
    )
    await store.append(
        cid,
        ObservationEvent(
            action_id=action.id,
            tool_result=ToolResult(
                call_id=tool_call.call_id,
                tool_name="shell",
                success=True,
                content="ok",
            ),
        ),
    )
    await store.append(
        cid,
        StatusEvent(status=ConversationStatus.PAUSED, detail="actionless"),
    )


async def test_actionless_auto_resume_once_only_for_autonomous_build(monkeypatch):
    store = SqliteEventStore(":memory:")
    rt = _runtime(store)
    auto_cid = f"{CID}-auto"
    await _seed_first_actionless_pause(store, auto_cid)
    rt.settings._set_surface(auto_cid, "build")
    rt.settings.set_autonomous(auto_cid, True)
    resume = AsyncMock(return_value={"ok": True, "status": "RUNNING"})
    monkeypatch.setattr(rt._resume, "resume_conversation", resume)

    await rt._run_finalizer.finalize_clean(auto_cid)
    await rt._run_finalizer.finalize_clean(auto_cid)

    resume.assert_awaited_once_with(auto_cid)
    auto_events = await store.get_events(auto_cid)
    auto_nudges = [
        e
        for e in auto_events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and e.message is not None
        and "AUTO-RESUME-ONCE(actionless)" in (e.message.content or "")
    ]
    assert len(auto_nudges) == 1

    manual_cid = f"{CID}-manual"
    await _seed_first_actionless_pause(store, manual_cid)
    rt.settings._set_surface(manual_cid, "build")

    await rt._run_finalizer.finalize_clean(manual_cid)

    resume.assert_awaited_once_with(auto_cid)
    manual_events = await store.get_events(manual_cid)
    assert not any(
        isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and e.message is not None
        and "AUTO-RESUME-ONCE(actionless)" in (e.message.content or "")
        for e in manual_events
    )


async def test_second_actionless_pause_reenters_resume_endpoint_for_synthetic_finish(
    monkeypatch,
):
    store = SqliteEventStore(":memory:")
    rt = _runtime(store)
    cid = f"{CID}-second-actionless"
    await _seed_first_actionless_pause(store, cid)
    await store.append(
        cid,
        MessageEvent(
            source=EventSource.ENVIRONMENT,
            message=LLMMessage(
                role="user",
                content="AUTO-RESUME-ONCE(actionless): already nudged",
            ),
        ),
    )
    await store.append(
        cid,
        StatusEvent(status=ConversationStatus.RUNNING, detail="resumed"),
    )
    await store.append(
        cid,
        StatusEvent(status=ConversationStatus.PAUSED, detail="actionless"),
    )
    rt.settings._set_surface(cid, "build")
    rt.settings.set_autonomous(cid, True)
    resume = AsyncMock(return_value={"ok": True, "status": "RUNNING"})
    monkeypatch.setattr(rt._resume, "resume_conversation", resume)

    await rt._run_finalizer.finalize_clean(cid)

    resume.assert_awaited_once_with(cid)
    events = await store.get_events(cid)
    assert (
        sum(
            1
            for e in events
            if isinstance(e, MessageEvent)
            and e.source == EventSource.ENVIRONMENT
            and e.message is not None
            and "AUTO-RESUME-ONCE(actionless)" in (e.message.content or "")
        )
        == 1
    )


async def test_resume_from_error_is_legal_and_preserves_history():
    """RECOVERY: ERROR → resume_conversation → ok=True/RUNNING, with the FULL
    persisted history intact (no events wiped — the 'Try again' button must not lose
    progress). A RUNNING StatusEvent is appended; every original event id survives."""
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store)
    rt.settings._set_surface(CID, "build")
    await store.append(CID, _user("build it"))
    await store.append(CID, _plan_event())
    await store.append(CID, StatusEvent(status=ConversationStatus.ERROR))

    ids_before = {e.id for e in await store.get_events(CID)}

    result = await rt._resume.resume_conversation(CID)
    # kick() registers synchronously, then resolves the frozen driver context and
    # composes inside that task. Yield once so this test observes the composed loop
    # before cancelling the otherwise-live run.
    await asyncio.sleep(0)
    await _cancel_task(rt)

    assert result["ok"] is True
    assert result["status"] == "RUNNING"

    events_after = await store.get_events(CID)
    # No progress lost: every pre-resume event still present.
    assert ids_before <= {e.id for e in events_after}
    # The loop was actually re-kicked (kick() composes + caches a loop for CID).
    assert rt._loop_registry.loop(CID) is not None
    # The conversation is back in RUNNING (a RUNNING StatusEvent was appended).
    assert any(
        isinstance(e, StatusEvent) and e.status == ConversationStatus.RUNNING for e in events_after
    )


async def test_resume_from_stuck_is_legal_and_preserves_history():
    """RECOVERY: STUCK → resume_conversation → ok=True/RUNNING, history preserved."""
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store)
    rt.settings._set_surface(CID, "build")
    await store.append(CID, _user("build it"))
    await store.append(CID, _plan_event())
    await store.append(CID, StatusEvent(status=ConversationStatus.STUCK))

    ids_before = {e.id for e in await store.get_events(CID)}

    result = await rt._resume.resume_conversation(CID)
    await _cancel_task(rt)

    assert result["ok"] is True
    assert result["status"] == "RUNNING"
    assert ids_before <= {e.id for e in await store.get_events(CID)}


async def test_resume_from_error_rehydrates_build_workspace(monkeypatch):
    """The resumed Build kick must rehydrate the persisted workspace so the files
    come back — the rehydrate-on-kick path fires for an ERROR resume just as for a
    PAUSED one (surface-gated, not status-gated)."""
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    # A plan-then-park script so the loop reaches the rehydrate hook then parks
    # cleanly at AWAITING_PLAN_APPROVAL (no live sandbox work needed).
    rt = _runtime(store, steps=[("plan", [_plan(["do it"])])])
    rt.settings._set_surface(CID, "build")
    await store.append(CID, _user("build it"))
    await store.append(CID, _plan_event())
    await store.append(CID, StatusEvent(status=ConversationStatus.ERROR))

    calls: list[str] = []
    orig = rt.lifecycle._maybe_rehydrate

    async def _spy(cid: str) -> None:
        calls.append(cid)
        return await orig(cid)

    monkeypatch.setattr(rt.lifecycle, "_maybe_rehydrate", _spy)

    result = await rt._resume.resume_conversation(CID)
    assert result["ok"] is True

    task = rt.run_registry.task(CID)
    if task is not None:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    await _cancel_task(rt)

    assert calls == [CID]  # workspace rehydrate fired on the resumed build kick


async def test_resume_from_error_is_illegal_for_deep_research():
    """A Deep Research conversation in ERROR is NOT resumable via this path — its
    driver only re-runs from a PAUSED checkpoint, so resume must report illegal
    rather than falsely flip to RUNNING and no-op (the DR surface retries fresh)."""
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store)
    rt.settings._set_surface(CID, "deep_research")
    await store.append(CID, _user("research it"))
    await store.append(CID, StatusEvent(status=ConversationStatus.ERROR))

    result = await rt._resume.resume_conversation(CID)

    assert result["ok"] is False
    assert "illegal_state" in result["reason"]


# ---- environment message exactly once ----------------------------------------


async def test_resume_appends_environment_message_exactly_once():
    """resume_conversation appends exactly one 'Resumed by user.' env message."""
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store)
    rt.settings._set_surface(CID, "build")
    await store.append(CID, _user("build it"))
    await store.append(CID, StatusEvent(status=ConversationStatus.PAUSED))

    events_before = await store.get_events(CID)
    resume_msgs_before = [
        e
        for e in events_before
        if isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and "Resumed by user." in e.message.content
    ]
    assert len(resume_msgs_before) == 0

    await rt._resume.resume_conversation(CID)
    await _cancel_task(rt)

    events_after = await store.get_events(CID)
    resume_msgs_after = [
        e
        for e in events_after
        if isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and "Resumed by user." in e.message.content
    ]
    assert len(resume_msgs_after) == 1


async def test_resume_append_failure_preserves_volatile_pause_flags(monkeypatch):
    """A failed durable transition must not mutate the still-paused local loop."""

    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store)
    rt.settings._set_surface(CID, "build")
    await store.append(CID, _user("build it"))
    await store.append(CID, StatusEvent(status=ConversationStatus.PAUSED))
    monkeypatch.setattr(rt._resume._run_start, "start", MagicMock())

    cancel_flag = asyncio.Event()
    cancel_flag.set()
    pause_flag = asyncio.Event()
    pause_flag.set()
    loop = MagicMock()
    loop._pause_requested = pause_flag
    rt._cancellations._flags[CID] = cancel_flag
    rt._loop_registry._loops[CID] = loop

    async def fail_append(*args, **kwargs):
        raise RuntimeError("append failed")

    monkeypatch.setattr(
        rt._lifecycle_commands,
        "append_transition_batch_locked",
        fail_append,
    )

    with pytest.raises(RuntimeError, match="append failed"):
        await rt._resume.resume_conversation(CID)

    assert rt._cancellations._flags[CID] is cancel_flag
    assert cancel_flag.is_set()
    assert pause_flag.is_set()
    rt._resume._run_start.start.assert_not_called()


# ---- double-resume race -------------------------------------------------------


async def test_double_resume_second_call_is_409():
    """Second resume_conversation call while RUNNING (after first resume) → already_running."""
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store)
    rt.settings._set_surface(CID, "build")
    await store.append(CID, _user("build it"))
    await store.append(CID, StatusEvent(status=ConversationStatus.PAUSED))

    # First resume: ok=True, flips status to RUNNING in the event log.
    result1 = await rt._resume.resume_conversation(CID)
    assert result1["ok"] is True

    # Second resume: status is now RUNNING → 409.
    result2 = await rt._resume.resume_conversation(CID)
    assert result2["ok"] is False
    assert result2["reason"] == "already_running"

    await _cancel_task(rt)


# ---- WALK-18: resume drains a lingering cancelled task before re-kicking ------


async def test_resume_drains_lingering_task_then_rekicks(monkeypatch):
    """WALK-18: after a cooperative Stop the loop task may still be finishing its
    in-flight model step. kick() is idempotent over a non-done task, so without a
    drain the re-kick silently NO-OPs ('Resume does nothing'). resume must drain
    the lingering task FIRST, then spawn a fresh run."""
    import disco.agent_server.resume_service as resume_mod

    # Keep the hard-cancel fallback fast for the test's wedged-step simulation.
    monkeypatch.setattr(resume_mod, "_RESUME_DRAIN_TIMEOUT_S", 0.05)

    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store)
    rt.settings._set_surface(CID, "build")
    await store.append(CID, _user("build it"))
    await store.append(CID, _plan_event())
    await store.append(CID, StatusEvent(status=ConversationStatus.PAUSED))

    # A cooperatively-cancelled loop task still winding down (NOT done) at the
    # moment the user clicks Resume — here wedged so kick() would no-op over it.
    blocker = asyncio.Event()

    async def _wedged():
        await blocker.wait()

    lingering = asyncio.create_task(_wedged())
    rt.run_registry._tasks[CID] = lingering

    result = await rt._resume.resume_conversation(CID)
    assert result["ok"] is True
    # The lingering task was drained (hard-cancelled after the patched timeout)…
    assert lingering.done()
    # …and a FRESH task was spawned — resume actually re-kicked the loop.
    new_task = rt.run_registry.task(CID)
    assert new_task is not None and new_task is not lingering

    await _cancel_task(rt)


async def test_resume_timeout_never_cancels_or_pops_newer_task(monkeypatch):
    """A stale resume may time out and hard-cancel only the task it observed.

    This is the destructive counterexample for the old conversation-id drain:
    while resume awaits task A, task B takes over the registry.  The timeout must
    cancel A by object identity, reject the stale resume, and leave B registered
    and live.
    """
    import disco.agent_server.resume_service as resume_mod

    monkeypatch.setattr(resume_mod, "_RESUME_DRAIN_TIMEOUT_S", 0.05)

    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store)
    rt.settings._set_surface(CID, "build")
    await store.append(CID, _user("build it"))
    await store.append(CID, StatusEvent(status=ConversationStatus.PAUSED))
    monkeypatch.setattr(rt._resume._run_start, "start", MagicMock())

    old_release = asyncio.Event()

    async def _old_tail() -> None:
        await old_release.wait()

    old_task = asyncio.create_task(_old_tail())
    rt.run_registry._generations[CID] = 1
    rt.run_registry._tasks[CID] = old_task

    resuming = asyncio.create_task(rt._resume.resume_conversation(CID))

    async def _wait_until_detached() -> None:
        while rt.run_registry.task(CID) is old_task:
            await asyncio.sleep(0)

    await asyncio.wait_for(_wait_until_detached(), timeout=1)

    newer_release = asyncio.Event()

    async def _newer_run() -> None:
        await newer_release.wait()

    newer_task = asyncio.create_task(_newer_run())
    async with rt.workspace.lock(CID):
        async with rt.workspace.interprocess_mutation_fence(CID):
            rt.run_registry._generations[CID] = 2
            rt.run_registry._tasks[CID] = newer_task

    result = await asyncio.wait_for(resuming, timeout=1)
    assert result == {"ok": False, "reason": "resume_superseded"}
    assert old_task.done() and old_task.cancelled()
    assert not newer_task.done()
    assert rt.run_registry.task(CID) is newer_task
    rt._resume._run_start.start.assert_not_called()
    events = await store.get_events(CID)
    assert not any(
        isinstance(event, StatusEvent)
        and event.status is ConversationStatus.RUNNING
        and event.detail == "resumed"
        for event in events
    )

    newer_release.set()
    await newer_task
    rt.run_registry._tasks.pop(CID, None)


async def test_resume_reconstructs_from_fresh_post_drain_history(monkeypatch):
    """Events landing between the two fenced phases must inform reconstruction.

    The old implementation reconstructed from the pre-drain list.  Here a fresh
    approved plan lands while the drain yields; the resume environment message
    must name its first undone step.
    """
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store)
    rt.settings._set_surface(CID, "build")
    await store.append(CID, _user("build it"))
    await store.append(CID, StatusEvent(status=ConversationStatus.PAUSED))
    monkeypatch.setattr(rt._resume._run_start, "start", MagicMock())

    drain_entered = asyncio.Event()
    release_drain = asyncio.Event()
    original_drain = rt._resume._drain_finishing_task

    async def _yielding_drain(task) -> bool:
        drain_entered.set()
        await release_drain.wait()
        return await original_drain(task)

    monkeypatch.setattr(rt._resume, "_drain_finishing_task", _yielding_drain)
    resuming = asyncio.create_task(rt._resume.resume_conversation(CID))
    await asyncio.wait_for(drain_entered.wait(), timeout=1)

    async with rt.workspace.lock(CID):
        async with rt.workspace.interprocess_mutation_fence(CID):
            await store.append(
                CID,
                PlanEvent(
                    summary="fresh plan",
                    steps=[PlanStep(title="Use the fresh post-drain step")],
                    revision=2,
                ),
            )
    release_drain.set()

    assert await asyncio.wait_for(resuming, timeout=1) == {
        "ok": True,
        "status": "RUNNING",
    }
    events = await store.get_events(CID)
    resume_messages = [
        event
        for event in events
        if isinstance(event, MessageEvent)
        and event.source is EventSource.ENVIRONMENT
        and "Resumed by user." in event.message.content
    ]
    assert len(resume_messages) == 1
    assert "Use the fresh post-drain step" in resume_messages[0].message.content
    rt._resume._run_start.start.assert_called_once_with(CID)


async def test_resume_does_not_start_beside_cancellation_resistant_old_task(monkeypatch):
    import disco.agent_server.resume_service as resume_mod

    monkeypatch.setattr(resume_mod, "_RESUME_DRAIN_TIMEOUT_S", 0.01)
    monkeypatch.setattr(resume_mod, "_RESUME_CANCEL_GRACE_S", 0.01)
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store)
    rt.settings._set_surface(CID, "build")
    await store.append(CID, _user("build it"))
    await store.append(CID, StatusEvent(status=ConversationStatus.PAUSED))
    monkeypatch.setattr(rt._resume._run_start, "start", MagicMock())

    release = asyncio.Event()

    async def _resists_one_cancel() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()

    old_task = asyncio.create_task(_resists_one_cancel())
    rt.run_registry._generations[CID] = 1
    rt.run_registry._tasks[CID] = old_task

    result = await asyncio.wait_for(rt._resume.resume_conversation(CID), timeout=1)
    assert result == {"ok": False, "reason": "resume_drain_timeout"}
    assert rt.run_registry.task(CID) is old_task
    assert not old_task.done()
    rt._resume._run_start.start.assert_not_called()

    release.set()
    await old_task
    rt.run_registry._tasks.pop(CID, None)


# ---- HTTP route (via TestClient) ---------------------------------------------


async def _async_client(store: SqliteEventStore, rt: ConversationRuntime):
    """An async httpx client backed by the ASGI app — route runs in THIS event loop
    so background tasks are in scope and can be cancelled after each test."""
    app = create_app(store, runtime=rt)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_http_resume_paused_returns_ok():
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store)
    rt.settings._set_surface(CID, "build")
    await store.append(CID, _user("build it"))
    await store.append(CID, _plan_event())
    await store.append(CID, StatusEvent(status=ConversationStatus.PAUSED))

    async with await _async_client(store, rt) as client:
        resp = await client.post(f"/conversations/{CID}/resume")

    await _cancel_task(rt)

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["status"] == "RUNNING"


async def test_http_resume_running_returns_409():
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store)
    rt.settings._set_surface(CID, "build")
    await store.append(CID, StatusEvent(status=ConversationStatus.RUNNING))

    async with await _async_client(store, rt) as client:
        resp = await client.post(f"/conversations/{CID}/resume")

    assert resp.status_code == 409
    body = resp.json()
    detail = body.get("detail", body)
    assert detail["ok"] is False
    assert detail["reason"] == "already_running"


async def test_http_resume_finished_returns_409():
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store)
    rt.settings._set_surface(CID, "build")
    await store.append(CID, StatusEvent(status=ConversationStatus.FINISHED))

    async with await _async_client(store, rt) as client:
        resp = await client.post(f"/conversations/{CID}/resume")

    assert resp.status_code == 409
    body = resp.json()
    detail = body.get("detail", body)
    assert detail["ok"] is False
    assert detail["reason"] == "conversation_finished"


async def _append_productive_work(store: SqliteEventStore, cid: str) -> None:
    tool_call = ToolCall(tool_name="file_append", arguments={"path": "index.html", "content": "x"})
    action = await store.append(cid, ActionEvent(thought="progress", tool_call=tool_call))
    await store.append(
        cid,
        ObservationEvent(
            action_id=action.id,
            tool_result=ToolResult(
                call_id=tool_call.call_id,
                tool_name="file_append",
                success=True,
                content="ok",
            ),
        ),
    )


async def test_auto_resume_fires_again_after_productive_work_between_pauses(monkeypatch):
    """Live-caught (20-build soak): pause -> nudge -> SUCCESSFUL work -> pause
    again died PAUSED — the attempted-guard scanned the whole segment while the
    pause counter reset on productive work. Progress opens a NEW window: the
    nudge must fire again (bounded by the segment cap)."""
    store = SqliteEventStore(":memory:")
    rt = _runtime(store)
    cid = f"{CID}-rewindow"
    await _seed_first_actionless_pause(store, cid)
    rt.settings._set_surface(cid, "build")
    rt.settings.set_autonomous(cid, True)
    resume = AsyncMock(return_value={"ok": True, "status": "RUNNING"})
    monkeypatch.setattr(rt._resume, "resume_conversation", resume)

    await rt._run_finalizer.finalize_clean(cid)  # pause #1 -> nudge #1
    assert resume.await_count == 1

    # the model resumes, does REAL work, then pauses again (the live trail)
    await store.append(cid, StatusEvent(status=ConversationStatus.RUNNING, detail="resumed"))
    await _append_productive_work(store, cid)
    await store.append(cid, StatusEvent(status=ConversationStatus.PAUSED, detail="actionless"))

    await rt._run_finalizer.finalize_clean(cid)  # NEW window -> nudge #2 must fire
    assert resume.await_count == 2

    events = await store.get_events(cid)
    nudges = [
        e
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and e.message is not None
        and "AUTO-RESUME-ONCE(actionless)" in (e.message.content or "")
    ]
    assert len(nudges) == 2

    # cap: after 3 total nudges, a 4th window gets nothing
    await store.append(cid, StatusEvent(status=ConversationStatus.RUNNING, detail="resumed"))
    await _append_productive_work(store, cid)
    await store.append(cid, StatusEvent(status=ConversationStatus.PAUSED, detail="actionless"))
    await rt._run_finalizer.finalize_clean(cid)  # nudge #3
    assert resume.await_count == 3
    await store.append(cid, StatusEvent(status=ConversationStatus.RUNNING, detail="resumed"))
    await _append_productive_work(store, cid)
    await store.append(cid, StatusEvent(status=ConversationStatus.PAUSED, detail="actionless"))
    await rt._run_finalizer.finalize_clean(cid)  # capped — no 4th
    assert resume.await_count == 3


# --- [9] resume of legacy Build mints v1 run intent atomically -----------------


async def test_resume_legacy_build_mints_v1_run_intent():
    """Resuming a legacy Build conversation (pre-v1, using agent.run-admitted)
    must mint a v1 run-intent atomically with the RUNNING flip, so the resumed
    loop runs under strict v1 protocol."""
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store)
    rt.settings._set_surface(CID, "build")
    # Legacy Build events — no run_protocol_version
    await store.append(CID, _user("build it"))
    await store.append(CID, StatusEvent(status=ConversationStatus.PAUSED))

    result = await rt._resume.resume_conversation(CID)
    await _cancel_task(rt)

    assert result["ok"] is True
    events = await store.get_events(CID)
    intents = [
        e
        for e in events
        if isinstance(e, WorkspaceMutationEvent) and e.operation == "agent.run-intent.resume"
    ]
    assert len(intents) == 1
    assert intents[0].run_protocol_version == 1
    # The RUNNING flip follows the intent
    resume_idx = next(i for i, e in enumerate(events) if e is intents[0])
    running_events = [
        e
        for e in events[resume_idx + 1 :]
        if isinstance(e, StatusEvent) and e.status == ConversationStatus.RUNNING
    ]
    assert len(running_events) >= 1


async def test_resume_sealed_workflow_mints_v1_run_intent():
    """Resuming a sealed FINISHED workspace (via IDLE+unfinished plan) must
    also mint a v1 run-intent atomically."""
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store)
    rt.settings._set_surface(CID, "build")
    # Sealed but with an approved unfinished PlanEvent
    await store.append(CID, _user("build it"))
    await store.append(CID, PlanEvent(summary="plan", steps=[PlanStep(title="step 1")], revision=1))
    from disco.core.events import StatusEvent as SE

    await store.append(CID, SE(status=ConversationStatus.RUNNING, detail="plan_approved"))
    await store.append(CID, SE(status=ConversationStatus.IDLE))

    result = await rt._resume.resume_conversation(CID)
    await _cancel_task(rt)

    assert result["ok"] is True
    events = await store.get_events(CID)
    intents = [
        e
        for e in events
        if isinstance(e, WorkspaceMutationEvent) and e.operation == "agent.run-intent.resume"
    ]
    assert len(intents) >= 1
    assert intents[0].run_protocol_version == 1
