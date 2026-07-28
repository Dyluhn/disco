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

import pytest
from disco.agent_server.lifecycle import LifecycleManager

CID = "conv_621005"


class _Sandbox:
    pass


class _Executor:
    def __init__(self) -> None:
        self._sandbox = _Sandbox()


class _Rt:
    """Only the per-conversation state `_teardown_sandbox` is responsible for."""

    def __init__(self) -> None:
        self._executors = {CID: _Executor()}
        self._pending_sessions = {CID: object()}
        self._loops = {CID: object()}
        self._session_view_cache = {(CID, "a"): b"x" * 1024}
        self._session_view_locks = {(CID, "a"): asyncio.Lock()}
        self._wake_locks = {CID: asyncio.Lock()}
        self._last_sessions = {CID: ["s1"]}
        self._rehydrated = {CID}

    def _sandbox_service_now(self):
        return None


def _manager(rt: _Rt) -> LifecycleManager:
    manager = LifecycleManager.__new__(LifecycleManager)
    manager._rt = rt  # type: ignore[attr-defined]
    return manager


def _residue(rt: _Rt) -> dict[str, object]:
    return {
        "executors": CID in rt._executors,
        "pending_sessions": CID in rt._pending_sessions,
        "loops": CID in rt._loops,
        "session_view_cache": [k for k in rt._session_view_cache if k[0] == CID],
        "session_view_locks": [k for k in rt._session_view_locks if k[0] == CID],
        "wake_locks": CID in rt._wake_locks,
        "last_sessions": CID in rt._last_sessions,
        "rehydrated": CID in rt._rehydrated,
    }


@pytest.mark.asyncio
async def test_teardown_leaves_zero_relevant_resources():
    rt = _Rt()
    await _manager(rt)._teardown_sandbox(CID)
    assert _residue(rt) == {
        "executors": False,
        "pending_sessions": False,
        "loops": False,
        "session_view_cache": [],
        "session_view_locks": [],
        "wake_locks": False,
        "last_sessions": False,
        "rehydrated": False,
    }


@pytest.mark.asyncio
async def test_a_pinned_snapshot_does_not_keep_anything_alive():
    """The pin is a local of `_maybe_snapshot`; it must not survive the call."""
    rt = _Rt()
    manager = _manager(rt)

    captured: dict[str, object] = {}

    class _Persistence:
        async def _do_capture_workspace(self, conversation_id: str, **kwargs: object):
            captured.update(kwargs)
            return None

    manager._persistence = _Persistence()  # type: ignore[attr-defined]
    await manager._maybe_snapshot(CID, trigger="suspend")
    assert captured["pinned_session"] is not None, "the pin was taken"

    await manager._teardown_sandbox(CID)
    assert all(
        v in (False, []) for v in _residue(rt).values()
    ), "teardown after a pinned snapshot must still free everything"


@pytest.mark.asyncio
async def test_teardown_is_idempotent_on_an_already_clean_conversation():
    # Teardown runs on paths where the executor may already be gone; it must not
    # raise or resurrect keys.
    rt = _Rt()
    manager = _manager(rt)
    await manager._teardown_sandbox(CID)
    await manager._teardown_sandbox(CID)
    assert all(v in (False, []) for v in _residue(rt).values())
