"""A non-FINISHED boundary must PIN the sandbox before its capture await.

Certified-lane seed 621005 (2026-07-27, `p4_ff_react_steer`): 68 actions over
1104s, then the conversation terminated with NO `<projects_root>/<cid>/` at all —
no tree, no versions, no manifest. Its browser screenshots were referenced as
durable evidence and had nowhere to live, surfacing as INVALID_RUN /
MISSING_REQUIRED_EVIDENCE.

`commit_finished_workspace` has pinned `executor._sandbox` since pilot seed
406546 because the executor may be gone by capture time. Every OTHER ended state
(STUCK / ERROR / PAUSED / suspend) re-resolved it inside
`_resolve_capture_session` and lost the race. `_suspend` already re-validates
`conversation_id not in self._rt._executors` immediately after this await — the
window was known; the snapshot just was not protected from it.

The reproduction is the ordering itself: the executor is dropped between the
boundary and the capture, exactly as teardown/rotation does.
"""

from __future__ import annotations

import pytest
from disco.agent_server.lifecycle import LifecycleManager


class _Sandbox:
    """Identity marker — the pinned reference we expect capture to receive."""


class _Executor:
    def __init__(self, sandbox: object) -> None:
        self._sandbox = sandbox


class _Rt:
    def __init__(self, executor: object | None) -> None:
        self._executors = {"conv_621005": executor} if executor is not None else {}


class _Persistence:
    """Records what capture was handed, and DROPS the executor first —
    reproducing teardown/rotation landing between the boundary and the capture."""

    def __init__(self, rt: _Rt) -> None:
        self._rt = rt
        self.seen: dict[str, object] = {}

    async def _do_capture_workspace(self, conversation_id: str, **kwargs: object):
        self._rt._executors.pop(conversation_id, None)  # the race, made deterministic
        self.seen = dict(kwargs)
        # What `_resolve_capture_session` would find, given what it was handed.
        executor = self._rt._executors.get(conversation_id)
        live = getattr(executor, "_sandbox", None) if executor is not None else None
        self.seen["resolved_session"] = kwargs.get("pinned_session") or live
        return None


def _manager(executor: object | None) -> tuple[LifecycleManager, _Persistence]:
    rt = _Rt(executor)
    manager = LifecycleManager.__new__(LifecycleManager)
    manager._rt = rt  # type: ignore[attr-defined]
    persistence = _Persistence(rt)
    manager._persistence = persistence  # type: ignore[attr-defined]
    return manager, persistence


@pytest.mark.asyncio
async def test_the_boundary_pins_the_sandbox_before_the_capture_await():
    sandbox = _Sandbox()
    manager, persistence = _manager(_Executor(sandbox))
    await manager._maybe_snapshot("conv_621005", trigger="suspend")
    assert persistence.seen["pinned_session"] is sandbox


@pytest.mark.asyncio
async def test_capture_still_resolves_a_session_after_the_executor_is_dropped():
    """The regression itself: pre-fix this resolved to None and the workspace was lost."""
    sandbox = _Sandbox()
    manager, persistence = _manager(_Executor(sandbox))
    await manager._maybe_snapshot("conv_621005", trigger="stuck")
    assert persistence.seen["resolved_session"] is sandbox, (
        "the executor was dropped between the boundary and the capture; without a pin "
        "the capture resolves nothing and the whole workspace is lost"
    )


@pytest.mark.asyncio
async def test_no_executor_at_the_boundary_pins_nothing_and_still_fails_closed():
    # Negative control: a pin must not invent authority that was never there.
    manager, persistence = _manager(None)
    await manager._maybe_snapshot("conv_621005", trigger="error")
    assert persistence.seen["pinned_session"] is None
    assert persistence.seen["resolved_session"] is None


@pytest.mark.asyncio
async def test_the_trigger_label_is_preserved():
    manager, persistence = _manager(_Executor(_Sandbox()))
    await manager._maybe_snapshot("conv_621005", trigger="paused")
    assert persistence.seen["trigger"] == "paused"


@pytest.mark.asyncio
async def test_an_executor_with_no_sandbox_pins_nothing():
    """Proof 7 at this seam: the pin must not FABRICATE authority.

    An executor can exist while its `_sandbox` is already None — killed, or
    between generations. `getattr(executor, "_sandbox", None)` must then yield
    None so capture still fails closed, rather than passing a truthy placeholder
    that would let `_resolve_capture_session` believe it has a session.
    """

    class _Killed:
        _sandbox = None

    manager, persistence = _manager(_Killed())
    await manager._maybe_snapshot("conv_621005", trigger="stuck")
    assert persistence.seen["pinned_session"] is None
    assert persistence.seen["resolved_session"] is None


@pytest.mark.asyncio
async def test_the_pin_does_not_make_capture_happen_that_otherwise_would_not():
    """Pinning changes WHICH session capture sees, never WHETHER it runs.

    `_maybe_snapshot` delegates exactly once either way; the pin must not add a
    second capture, nor a version for work that does not exist.
    """
    calls: list[str] = []

    manager, persistence = _manager(_Executor(_Sandbox()))
    original = persistence._do_capture_workspace

    async def _counting(conversation_id: str, **kwargs: object):
        calls.append(conversation_id)
        return await original(conversation_id, **kwargs)

    persistence._do_capture_workspace = _counting  # type: ignore[method-assign]
    await manager._maybe_snapshot("conv_621005", trigger="suspend")
    assert calls == ["conv_621005"], "exactly one capture, pinned or not"
