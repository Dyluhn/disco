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
from disco.agent_server.lifecycle_ports import (
    LifecycleRunState,
    LifecycleSandboxAccess,
    LifecycleStoreAccess,
)
from disco.agent_server.run_registry import RunResourceRegistry


class _Sandbox:
    """Identity marker — the pinned reference we expect capture to receive."""


class _Executor:
    def __init__(self, sandbox: object) -> None:
        self._sandbox = sandbox


class _EmptyStore:
    async def get_events(self, _cid: str) -> list[object]:
        return []


class _ProjectStore:
    """A stand-in project store; status() is OK so capture proceeds."""

    def status(self):
        from disco.tools.projects import StorageStatus

        return StorageStatus.OK


class _Persistence:
    """Records what capture was handed, and DROPS the executor first —
    reproducing teardown/rotation landing between the boundary and the capture."""

    def __init__(self, run_resources: RunResourceRegistry) -> None:
        self._run_resources = run_resources
        self.seen: dict[str, object] = {}

    async def capture_workspace(
        self,
        conversation_id: str,
        *,
        trigger: str,
        version_label: str = "",
        seal_fence: tuple[int, int | None] | None = None,
        journal: dict | None = None,
        pinned_session: object | None = None,
        snapshot_fn: object | None = None,
    ):
        self._run_resources.pop_executor(conversation_id)  # the race, made deterministic
        self.seen = {
            "trigger": trigger,
            "version_label": version_label,
            "seal_fence": seal_fence,
            "journal": journal,
            "pinned_session": pinned_session,
            "snapshot_fn": snapshot_fn,
        }
        # What `_resolve_capture_session` would find, given what it was handed.
        executor = self._run_resources.executor(conversation_id)
        live = getattr(executor, "_sandbox", None) if executor is not None else None
        self.seen["resolved_session"] = pinned_session or live
        return None


def _manager(
    executor: object | None,
) -> tuple[LifecycleManager, _Persistence, RunResourceRegistry]:
    run_resources = RunResourceRegistry()
    if executor is not None:
        run_resources.set_executor("conv_621005", executor)  # type: ignore[arg-type]
    store = _EmptyStore()
    persistence = _Persistence(run_resources)

    manager = LifecycleManager.__new__(LifecycleManager)
    manager._store = LifecycleStoreAccess(store)  # type: ignore[arg-type]
    manager._sandbox = LifecycleSandboxAccess.__new__(LifecycleSandboxAccess)
    manager._sandbox._projects = type(
        "_Projects", (), {"current_project_store": lambda self: _ProjectStore()}
    )()  # type: ignore[attr-defined]
    manager._run_state = LifecycleRunState.__new__(LifecycleRunState)
    manager._run_state._resources = run_resources
    manager._run_state._runs = type("_Runs", (), {"generation": lambda self, cid: None})()
    manager._run_state._loops = type("_Loops", (), {"forget": lambda self, cid: None})()
    manager._persistence = persistence  # type: ignore[attr-defined]
    return manager, persistence, run_resources


@pytest.mark.asyncio
async def test_the_boundary_pins_the_sandbox_before_the_capture_await():
    sandbox = _Sandbox()
    manager, persistence, _resources = _manager(_Executor(sandbox))
    await manager._maybe_snapshot("conv_621005", trigger="suspend")
    assert persistence.seen["pinned_session"] is sandbox


@pytest.mark.asyncio
async def test_capture_still_resolves_a_session_after_the_executor_is_dropped():
    """The regression itself: pre-fix this resolved to None and the workspace was lost."""
    sandbox = _Sandbox()
    manager, persistence, _resources = _manager(_Executor(sandbox))
    await manager._maybe_snapshot("conv_621005", trigger="stuck")
    assert persistence.seen["resolved_session"] is sandbox, (
        "the executor was dropped between the boundary and the capture; without a pin "
        "the capture resolves nothing and the whole workspace is lost"
    )


@pytest.mark.asyncio
async def test_no_executor_at_the_boundary_pins_nothing_and_still_fails_closed():
    # Negative control: a pin must not invent authority that was never there.
    manager, persistence, _resources = _manager(None)
    await manager._maybe_snapshot("conv_621005", trigger="error")
    assert persistence.seen["pinned_session"] is None
    assert persistence.seen["resolved_session"] is None


@pytest.mark.asyncio
async def test_the_trigger_label_is_preserved():
    manager, persistence, _resources = _manager(_Executor(_Sandbox()))
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

    manager, persistence, _resources = _manager(_Killed())
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

    manager, persistence, _resources = _manager(_Executor(_Sandbox()))
    original = persistence.capture_workspace

    async def _counting(
        conversation_id: str,
        *,
        trigger: str,
        version_label: str = "",
        seal_fence: tuple[int, int | None] | None = None,
        journal: dict | None = None,
        pinned_session: object | None = None,
        snapshot_fn: object | None = None,
    ):
        calls.append(conversation_id)
        return await original(
            conversation_id,
            trigger=trigger,
            version_label=version_label,
            seal_fence=seal_fence,
            journal=journal,
            pinned_session=pinned_session,
            snapshot_fn=snapshot_fn,
        )

    persistence.capture_workspace = _counting  # type: ignore[method-assign]
    await manager._maybe_snapshot("conv_621005", trigger="suspend")
    assert calls == ["conv_621005"], "exactly one capture, pinned or not"


# ---- F-25: an already-sealed terminal must not get a redundant capture --------


class _Store:
    def __init__(self, events: list[object]) -> None:
        self._events = events

    async def get_events(self, _cid: str) -> list[object]:
        return self._events


def _manager_with_store(executor: object | None, sealed: bool):
    import disco.agent_server.lifecycle as lifecycle_mod

    run_resources = RunResourceRegistry()
    if executor is not None:
        run_resources.set_executor("conv_621005", executor)  # type: ignore[arg-type]
    store = _Store([])
    persistence = _Persistence(run_resources)

    manager = LifecycleManager.__new__(LifecycleManager)
    manager._store = LifecycleStoreAccess(store)  # type: ignore[arg-type]
    manager._sandbox = LifecycleSandboxAccess.__new__(LifecycleSandboxAccess)
    manager._sandbox._projects = type(
        "_Projects", (), {"current_project_store": lambda self: _ProjectStore()}
    )()  # type: ignore[attr-defined]
    manager._run_state = LifecycleRunState.__new__(LifecycleRunState)
    manager._run_state._resources = run_resources
    manager._run_state._runs = type("_Runs", (), {"generation": lambda self, cid: None})()
    manager._run_state._loops = type("_Loops", (), {"forget": lambda self, cid: None})()
    manager._persistence = persistence  # type: ignore[attr-defined]

    def _resolve(_events, _store, _cid):
        if sealed:
            return object()
        raise lifecycle_mod.WorkspaceCommitUnavailable("no seal")

    return manager, persistence, _resolve


@pytest.mark.asyncio
async def test_a_sealed_terminal_gets_NO_recovery_capture(monkeypatch):
    """The redundant capture the F-21 pin exposed: it must not run."""
    import disco.agent_server.lifecycle as lifecycle_mod

    manager, persistence, resolve = _manager_with_store(_Executor(_Sandbox()), sealed=True)
    monkeypatch.setattr(lifecycle_mod, "resolve_committed_workspace", resolve)
    await manager._maybe_snapshot("conv_621005", trigger="suspend")
    assert persistence.seen == {}, (
        "a FINISHED run is already sealed; cutting a suspend version after the "
        "terminal displaces the finish marker"
    )


@pytest.mark.asyncio
async def test_an_UNSEALED_terminal_still_gets_its_pinned_capture(monkeypatch):
    """The control: F-21's fix must keep working where no seal exists."""
    import disco.agent_server.lifecycle as lifecycle_mod

    sandbox = _Sandbox()
    manager, persistence, resolve = _manager_with_store(_Executor(sandbox), sealed=False)
    monkeypatch.setattr(lifecycle_mod, "resolve_committed_workspace", resolve)
    await manager._maybe_snapshot("conv_621005", trigger="stuck")
    assert persistence.seen["pinned_session"] is sandbox
    assert persistence.seen["resolved_session"] is sandbox
