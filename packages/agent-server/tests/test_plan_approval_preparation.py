"""A slow sandbox seed must not resemble an orphaned RUNNING task."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from disco.agent_server.runtime import ConversationRuntime
from disco.core import ConversationStatus, PlanEvent, SqliteEventStore, StatusEvent
from disco.core.llm import OperatingMode
from disco.core.loop import AgentLoop

CID = "approval-preparation"


class HeldContextSandbox:
    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.files = {}

    async def write_file(self, path, data):
        self.entered.set()
        await self.release.wait()
        self.files[path] = data

    async def read_file(self, path):
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]


async def setup(monkeypatch):
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local", surface="build")
    runtime = ConversationRuntime(store, router=MagicMock())
    sandbox = HeldContextSandbox()
    loop = AgentLoop(
        CID,
        store,
        MagicMock(),
        SimpleNamespace(sandbox=sandbox),
        None,
        MagicMock(),
        MagicMock(),
        MagicMock(),
        MagicMock(),
        mode=OperatingMode.PLANNING,
        execution_mode=OperatingMode.LONG_HORIZON,
    )
    loop._control_fence = lambda: runtime.workspace.lock(CID)
    plan = await store.append(
        CID,
        PlanEvent(
            summary="Build the requested file.",
            steps=[
                {"title": "Create result.txt"},
            ],
        ),
    )
    await store.append(
        CID,
        StatusEvent(
            status=ConversationStatus.AWAITING_PLAN_APPROVAL,
            detail=plan.id,
        ),
    )
    runtime._loop_registry.bind(CID, loop)
    monkeypatch.setattr(runtime.run_controller, "loop_for", lambda cid: loop)

    # Preserve the production kick/registry ownership path without inference.
    async def hold_run():
        await asyncio.Event().wait()
        return await store.get_state(CID)

    def launch(cid, **kwargs):
        task = asyncio.create_task(hold_run())
        generation = runtime.run_registry.register_task(cid, task)
        return task, generation

    launches = MagicMock(side_effect=launch)
    monkeypatch.setattr(runtime.titles, "schedule", MagicMock())
    monkeypatch.setattr(runtime.run_controller._sandboxes, "evict_stale", MagicMock())
    monkeypatch.setattr(runtime._run_supervisor, "create_task", launches)
    completion = AsyncMock()
    monkeypatch.setattr(runtime.run_sweep, "_completion", completion)
    return runtime, loop, sandbox, store, launches, completion, plan


async def test_sweep_does_not_classify_delayed_approval_preparation_as_stranded(monkeypatch):
    runtime, loop, sandbox, store, launches, completion, plan = await setup(monkeypatch)
    approval = asyncio.create_task(runtime._control.approve_plan(CID))
    try:
        await asyncio.wait_for(sandbox.entered.wait(), timeout=2)
        assert await runtime.run_sweep.sweep_once() == 0
        assert (await store.get_state(CID)).execution_status == (
            ConversationStatus.AWAITING_PLAN_APPROVAL
        )
        assert loop.mode == OperatingMode.PLANNING
        completion.finalize_clean.assert_not_awaited()
        launches.assert_not_called()
        sandbox.release.set()
        await approval
        launches.assert_called_once()
        assert launches.call_args.args == (CID,)
        assert (await store.get_state(CID)).execution_status == ConversationStatus.RUNNING
        assert loop.mode == OperatingMode.LONG_HORIZON
        approvals = [
            e
            for e in await store.get_events(CID)
            if isinstance(e, StatusEvent) and e.detail == "plan_approved"
        ]
        assert len(approvals) == 1
        assert approvals[0].plan_verification_transition.new_plan_event_id == plan.id
        assert b"Build the requested file." in sandbox.files[".disco/context/current_goal.md"]
    finally:
        sandbox.release.set()
        await asyncio.gather(approval, return_exceptions=True)
        await runtime.aclose()


async def test_cancelled_preparation_preserves_pending_plan_and_allows_explicit_retry(monkeypatch):
    runtime, loop, sandbox, store, launches, completion, plan = await setup(monkeypatch)
    approval = asyncio.create_task(runtime._control.approve_plan(CID))
    try:
        await asyncio.wait_for(sandbox.entered.wait(), timeout=2)
        approval.cancel()
        with pytest.raises(asyncio.CancelledError):
            await approval
        state = await store.get_state(CID)
        assert state.execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL
        assert state.pending_plan_id == plan.id
        assert loop.mode == OperatingMode.PLANNING
        assert not loop._lock.locked()
        assert not runtime.workspace.lock(CID).locked()
        assert await runtime.run_sweep.sweep_once() == 0
        launches.assert_not_called()
        completion.finalize_clean.assert_not_awaited()
        sandbox.release.set()
        await runtime._control.approve_plan(CID)
        launches.assert_called_once()
        assert launches.call_args.args == (CID,)
        assert (await store.get_state(CID)).execution_status == ConversationStatus.RUNNING
    finally:
        sandbox.release.set()
        await asyncio.gather(approval, return_exceptions=True)
        await runtime.aclose()


async def test_genuinely_orphaned_running_still_reaches_recovery(monkeypatch):
    runtime, loop, sandbox, store, launches, completion, plan = await setup(monkeypatch)
    try:
        await store.append(CID, StatusEvent(status=ConversationStatus.RUNNING))
        assert await runtime.run_sweep.sweep_once() == 1
        completion.finalize_clean.assert_awaited_once_with(CID)
    finally:
        await runtime.aclose()


async def test_duplicate_approvals_share_one_transition_and_one_live_run(monkeypatch):
    runtime, loop, sandbox, store, launches, completion, plan = await setup(monkeypatch)
    first = asyncio.create_task(runtime._control.approve_plan(CID))
    second = None
    try:
        await asyncio.wait_for(sandbox.entered.wait(), timeout=2)
        second = asyncio.create_task(runtime._control.approve_plan(CID))
        assert await runtime.run_sweep.sweep_once() == 0
        sandbox.release.set()
        await asyncio.gather(first, second)
        approvals = [
            e
            for e in await store.get_events(CID)
            if isinstance(e, StatusEvent) and e.detail == "plan_approved"
        ]
        assert len(approvals) == 1
        launches.assert_called_once()
        assert runtime.run_registry.active_task(CID) is not None
        completion.finalize_clean.assert_not_awaited()
    finally:
        sandbox.release.set()
        await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
        await runtime.aclose()
