"""W-48 — sandbox connectivity PREFLIGHT (first-use) + reconcile cached sessions
on a persisted backend change.

Hermetic + bounded: a fake SandboxService stands in for the real backend (its
`healthcheck` is whatever the test wires); fake sessions record `destroy()`. No
container/Docker is touched.
"""

from __future__ import annotations

import asyncio
import time
import types

from disco.agent_server import ConversationRuntime
from disco.core import ConversationStatus, SqliteEventStore, StatusEvent
from disco.tools.sandbox import SandboxConfig, SandboxUnavailableError


class _FakeService:
    """Stand-in SandboxService: a `name`, a `_cfg` (so the endpoint label resolves)
    and a `healthcheck` coroutine the test controls."""

    def __init__(self, name="gvisor", cfg=None, on_health=None):
        self.name = name
        self._cfg = cfg or SandboxConfig(backend=name)
        self._on_health = on_health

    async def healthcheck(self) -> None:
        if self._on_health is not None:
            await self._on_health()


class _FakeSession:
    def __init__(self, backend_name: str):
        self.backend_name = backend_name
        self.destroyed = False

    async def destroy(self) -> None:
        self.destroyed = True


def _set_backend(rt: ConversationRuntime, backend: str) -> None:
    """Pin the persisted sandbox backend the reconcile/preflight read."""
    rt._config_store = types.SimpleNamespace(  # type: ignore[attr-defined]
        load=lambda: types.SimpleNamespace(sandbox=types.SimpleNamespace(backend=backend))
    )


# ---- (a) first-use preflight ------------------------------------------------


async def test_preflight_sandbox_passes_when_reachable():
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(store)  # no injected sandbox → preflight active
    rt._sandbox_service_now = lambda: _FakeService()  # type: ignore[assignment]
    assert await rt._preflight_sandbox("c1") is None


async def test_preflight_sandbox_typed_named_reason_when_unreachable():
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(store)

    async def _dead():
        raise SandboxUnavailableError(
            "Docker unreachable at ssh://sandbox@100.81.82.115: connection refused"
        )

    cfg = SandboxConfig(backend="gvisor", docker_socket="ssh://sandbox@100.81.82.115")
    rt._sandbox_service_now = lambda: _FakeService(  # type: ignore[assignment]
        name="gvisor", cfg=cfg, on_health=_dead
    )
    reason = await rt._preflight_sandbox("c1")
    assert reason is not None
    assert "gvisor sandbox host ssh://sandbox@100.81.82.115" in reason  # endpoint NAMED
    assert "unreachable" in reason


async def test_preflight_sandbox_is_wall_bounded_on_black_hole():
    """A black-holed host (healthcheck that never returns) must FAIL FAST at the
    preflight deadline — a named timeout reason, not a minutes-long hang."""
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(store)

    async def _hang():
        await asyncio.sleep(60)

    rt._sandbox_service_now = lambda: _FakeService(on_health=_hang)  # type: ignore[assignment]
    rt._SANDBOX_PREFLIGHT_TIMEOUT_S = 0.1  # tiny bound for the test

    t0 = time.monotonic()
    reason = await rt._preflight_sandbox("c1")
    elapsed = time.monotonic() - t0

    assert reason is not None and "timed out" in reason and "unreachable" in reason
    assert elapsed < 5.0  # bounded by the deadline, not the 60s stall


async def test_preflight_sandbox_skipped_when_backend_injected():
    store = SqliteEventStore(":memory:")
    # An injected sandbox is authoritative — no endpoint to probe.
    rt = ConversationRuntime(store, sandbox_service=_FakeService(name="process"))
    assert await rt._preflight_sandbox("c1") is None


async def test_run_with_persistence_emits_typed_error_and_skips_loop():
    """Build-surface integration: a failed sandbox preflight emits StatusEvent(ERROR)
    with the named reason AND the loop never runs."""
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(store, sandbox_service=_FakeService(name="process"))
    rt.set_surface("c1", "build")

    async def _ok_driver(cid, **kw):
        return None

    async def _fail_sandbox(cid):
        return "gvisor sandbox host ssh://sandbox@host unreachable: connection refused"

    rt._preflight_driver = _ok_driver  # type: ignore[assignment]
    rt._preflight_sandbox = _fail_sandbox  # type: ignore[assignment]

    class _Loop:
        def __init__(self):
            self.ran = False

        async def run(self):
            self.ran = True

    loop = _Loop()
    await rt._run_with_persistence("c1", loop)
    assert loop.ran is False  # the doomed loop never started

    events = await store.get_events("c1")
    errs = [
        e for e in events
        if isinstance(e, StatusEvent) and e.status == ConversationStatus.ERROR
    ]
    assert errs and "unreachable" in (errs[-1].detail or "")
    assert "ssh://sandbox@host" in (errs[-1].detail or "")  # endpoint NAMED


# ---- (c) reconcile on backend change ----------------------------------------


async def test_reconcile_destroys_stale_backend_sessions_only():
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(store)
    _set_backend(rt, "local")  # the NEW configured backend

    # c1: a pending session on the OLD gvisor backend → must be destroyed + dropped.
    stale_pending = _FakeSession("gvisor")
    rt._pending_sessions["c1"] = stale_pending
    # c2: a live executor session on the OLD gvisor backend → destroyed; loop evicted.
    stale_exec = _FakeSession("gvisor")
    rt._executors["c2"] = types.SimpleNamespace(_sandbox=stale_exec)
    rt._loops["c2"] = object()
    # c3: already on the NEW backend → untouched.
    fresh_pending = _FakeSession("local")
    rt._pending_sessions["c3"] = fresh_pending

    n = await rt.reconcile_sandbox_backend()

    assert n == 2
    assert stale_pending.destroyed and "c1" not in rt._pending_sessions
    assert stale_exec.destroyed and "c2" not in rt._executors and "c2" not in rt._loops
    assert not fresh_pending.destroyed and "c3" in rt._pending_sessions


async def test_reconcile_skips_live_running_conversation():
    """Don't reconnect mid-turn: a conversation with a LIVE run task is left alone."""
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(store)
    _set_backend(rt, "local")

    busy = _FakeSession("gvisor")
    rt._executors["c1"] = types.SimpleNamespace(_sandbox=busy)

    async def _running():
        await asyncio.sleep(5)

    task = asyncio.create_task(_running())
    rt._tasks["c1"] = task
    try:
        n = await rt.reconcile_sandbox_backend()
        assert n == 0
        assert not busy.destroyed and "c1" in rt._executors  # mid-turn → untouched
    finally:
        task.cancel()


async def test_evict_stale_backend_lazy_path_drops_cached_loop():
    """The lazy per-conversation evict (run at kick) drops a stale-backend cached
    loop/executor/pending session so the next compose builds on the new backend."""
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(store)
    _set_backend(rt, "local")

    stale = _FakeSession("gvisor")
    rt._executors["c1"] = types.SimpleNamespace(_sandbox=stale)
    rt._loops["c1"] = object()
    pending = _FakeSession("gvisor")
    rt._pending_sessions["c1"] = pending

    rt._evict_stale_backend("c1")  # synchronous eviction + scheduled async destroy

    assert "c1" not in rt._executors and "c1" not in rt._loops
    assert "c1" not in rt._pending_sessions
    # the async best-effort destroy was scheduled — let it run.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert stale.destroyed and pending.destroyed


async def test_evict_stale_backend_noop_when_backend_unchanged():
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(store)
    _set_backend(rt, "gvisor")  # SAME as the cached session's backend

    sess = _FakeSession("gvisor")
    rt._executors["c1"] = types.SimpleNamespace(_sandbox=sess)
    rt._loops["c1"] = object()

    rt._evict_stale_backend("c1")
    assert "c1" in rt._executors and "c1" in rt._loops and not sess.destroyed
