"""Teardown must leave zero per-conversation resources — including after a PIN.

`c595d289` makes `_maybe_snapshot` hold a reference to `executor._sandbox` across
its capture await, to close the window in which teardown or a sandbox rotation
destroyed the workspace before it was persisted (certified-lane seed 621005).

A retained reference is exactly the kind of change that can leak. This proves the
pin is a local that dies with the call, and that `_teardown_sandbox` still empties
every structure it owns for that conversation.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from disco.agent_server.connection_tracker import ConnectionState
from disco.agent_server.lifecycle import LifecycleManager
from disco.agent_server.lifecycle_ports import (
    LifecycleConnections,
    LifecycleRunState,
    LifecycleStoreAccess,
)
from disco.agent_server.lifecycle_rehydration import Rehydration
from disco.agent_server.run_registry import (
    LoopRegistry,
    RunRegistry,
    RunResourceRegistry,
)

CID = "conv_621005"


class _Sandbox:
    pass


class _Executor:
    def __init__(self) -> None:
        self._sandbox = _Sandbox()

    async def kill(self) -> None:
        pass


class _Store:
    async def get_events(self, _cid: str) -> list[object]:
        return []


class _SandboxAccess:
    """Lifecycle sandbox-access stand-in with no durable project store."""

    def sandbox_service_now(self):
        return None

    def current_project_store(self):
        return None


def _build_run_state(executor: Any) -> LifecycleRunState:
    resources = RunResourceRegistry()
    resources.set_executor(CID, executor)
    resources.set_pending_session(CID, _Sandbox())  # type: ignore[arg-type]
    return LifecycleRunState(RunRegistry(), resources, LoopRegistry())


def _build_manager(
    *,
    run_state: LifecycleRunState,
    connections: LifecycleConnections,
    rehydration: Rehydration,
    persistence: Any | None = None,
) -> LifecycleManager:
    manager = LifecycleManager.__new__(LifecycleManager)
    manager._run_state = run_state  # type: ignore[attr-defined]
    manager._connections = connections  # type: ignore[attr-defined]
    manager._rehydration = rehydration  # type: ignore[attr-defined]
    manager._store = LifecycleStoreAccess(_Store())  # type: ignore[arg-type, attr-defined]
    manager._sandbox = _SandboxAccess()  # type: ignore[attr-defined]
    manager._persistence = persistence  # type: ignore[attr-defined]
    return manager


def _residue(
    run_state: LifecycleRunState,
    connections: LifecycleConnections,
    rehydration: Rehydration,
) -> dict[str, object]:
    state = connections._connections
    return {
        "executors": run_state.executor(CID) is not None,
        "pending_sessions": run_state.pending_session(CID) is not None,
        "loops": run_state._loops.loop(CID) is not None,
        "session_view_cache": [k for k in state._session_view_cache if k[0] == CID],
        "session_view_locks": [k for k in state._session_view_locks if k[0] == CID],
        "wake_locks": CID in state._wake_locks,
        "last_sessions": CID in state._last_sessions,
        "rehydrated": CID in rehydration._rehydrated,
    }


def _empty_residue() -> dict[str, object]:
    return {
        "executors": False,
        "pending_sessions": False,
        "loops": False,
        "session_view_cache": [],
        "session_view_locks": [],
        "wake_locks": False,
        "last_sessions": False,
        "rehydrated": False,
    }


def _fresh_state() -> tuple[LifecycleRunState, LifecycleConnections, Rehydration]:
    run_state = _build_run_state(_Executor())
    connection_state = ConnectionState()
    connection_state._session_view_cache[(CID, "a")] = (0.0, None)  # type: ignore[arg-type]
    connection_state._session_view_locks[(CID, "a")] = asyncio.Lock()
    connection_state._wake_locks[CID] = asyncio.Lock()
    connection_state._last_sessions[CID] = []  # type: ignore[assignment]
    connections = LifecycleConnections(connection_state)
    rehydration = Rehydration.__new__(Rehydration)
    rehydration._rehydrated = {CID}  # type: ignore[attr-defined]
    return run_state, connections, rehydration


@pytest.mark.asyncio
async def test_teardown_leaves_zero_relevant_resources():
    run_state, connections, rehydration = _fresh_state()
    manager = _build_manager(run_state=run_state, connections=connections, rehydration=rehydration)
    await manager._teardown_sandbox(CID)
    assert _residue(run_state, connections, rehydration) == _empty_residue()


@pytest.mark.asyncio
async def test_a_pinned_snapshot_does_not_keep_anything_alive():
    """The pin is a local of `_maybe_snapshot`; it must not survive the call."""
    run_state, connections, rehydration = _fresh_state()

    captured: dict[str, object] = {}

    class _Persistence:
        async def capture_workspace(self, conversation_id: str, **kwargs: object):
            captured.update(kwargs)
            return None

    manager = _build_manager(
        run_state=run_state,
        connections=connections,
        rehydration=rehydration,
        persistence=_Persistence(),
    )
    await manager._maybe_snapshot(CID, trigger="suspend")
    assert captured["pinned_session"] is not None, "the pin was taken"

    await manager._teardown_sandbox(CID)
    assert all(v in (False, []) for v in _residue(run_state, connections, rehydration).values()), (
        "teardown after a pinned snapshot must still free everything"
    )


@pytest.mark.asyncio
async def test_teardown_is_idempotent_on_an_already_clean_conversation():
    # Teardown runs on paths where the executor may already be gone; it must not
    # raise or resurrect keys.
    run_state, connections, rehydration = _fresh_state()
    manager = _build_manager(run_state=run_state, connections=connections, rehydration=rehydration)
    await manager._teardown_sandbox(CID)
    await manager._teardown_sandbox(CID)
    assert all(v in (False, []) for v in _residue(run_state, connections, rehydration).values())


@pytest.mark.asyncio
async def test_resource_registry_close_joins_detached_reclaim():
    resources = RunResourceRegistry()
    reclaim_entered = asyncio.Event()
    reclaim_release = asyncio.Event()

    async def _blocked_reclaim() -> None:
        reclaim_entered.set()
        await reclaim_release.wait()

    resources._track_reclaim(CID, _blocked_reclaim())
    closing = asyncio.create_task(resources.close())
    await asyncio.wait_for(reclaim_entered.wait(), timeout=2)
    assert not closing.done()
    assert resources._has_reclaims(CID)

    reclaim_release.set()
    await asyncio.wait_for(closing, timeout=2)
    assert not resources._has_reclaims(CID)
