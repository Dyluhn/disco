"""C15 — idle-kernel cull for the persistent CodeAct kernel.

The persistent IPython kernel holds RAM + tmux sessions + (for the container
backend) an inner container kernel forever — even when the agent goes quiet.
`ManagedKernel` wraps any `KernelSession` transport and shuts the inner kernel
down after `idle_timeout_s` of inactivity, re-spawning a fresh one on the next
`execute()`.

These tests use:
  * a fake `KernelSession` (no real jupyter) — cheap and deterministic
  * an injectable monotonic clock — no real wall-clock sleep
  * the public `ManagedKernel` interface — no monkey-patching internals
"""

from __future__ import annotations

import pytest
from disco.tools.sandbox.kernel import (
    KernelResult,
    ManagedKernel,
    _default_idle_timeout_s,
)

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeKernel:
    """A minimal `KernelSession` stand-in. One instance == one kernel lifetime:
    `start()` is the birth, `shutdown()` is the death. Tracks calls so tests can
    assert on lifecycle."""

    # Class-level counter, but reset by the `reset_fake_kernel_seq` fixture
    # before each test so per-instance ids are predictable within a test.
    _spawn_seq = 0

    def __init__(self) -> None:
        type(self)._spawn_seq += 1
        self.id: int = type(self)._spawn_seq
        self.started: bool = False
        self.was_shut_down: bool = False
        self.exec_calls: int = 0
        self.interrupt_calls: int = 0
        self.restart_calls: int = 0
        # Test hook: assign a coroutine that raises to simulate a broken
        # shutdown(). The cull path must tolerate it.
        self.shutdown_impl = None  # type: ignore[assignment]

    async def start(self) -> None:
        self.started = True

    async def execute(self, code: str, *, timeout_s: int) -> KernelResult:
        self.exec_calls += 1
        return KernelResult(ok=True, stdout=f"fake#{self.id}:{code}", stderr="")

    async def interrupt(self) -> None:
        self.interrupt_calls += 1

    async def restart(self) -> None:
        self.restart_calls += 1
        self.started = True

    async def shutdown(self) -> None:
        if self.shutdown_impl is not None:
            await self.shutdown_impl()
        self.was_shut_down = True


class _Clock:
    """A monotonic, manually-advanced clock. The default time source for
    `ManagedKernel` is `time.monotonic`, which would force tests to `sleep()`
    — not what we want."""

    def __init__(self, t0: float = 1000.0) -> None:
        self.t: float = t0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


@pytest.fixture(autouse=True)
def reset_fake_kernel_seq() -> None:
    """Reset the class-level spawn counter so per-instance `id` is predictable
    inside a test (and tests don't leak ids into each other)."""
    _FakeKernel._spawn_seq = 0


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def make_manager(clock: _Clock):
    """Returns `(manager_factory, spawned_list)` so each test can build a
    `ManagedKernel` and observe every inner kernel that the factory emitted."""
    spawned: list[_FakeKernel] = []

    def factory() -> _FakeKernel:
        k = _FakeKernel()
        spawned.append(k)
        return k

    def build(threshold: float) -> ManagedKernel:
        return ManagedKernel(factory, idle_timeout_s=threshold, time_source=clock)

    return build, spawned


# ---------------------------------------------------------------------------
# Acceptance: the two required behaviors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cull_when_idle_past_threshold_then_respawn(clock: _Clock, make_manager) -> None:
    """ACCEPTANCE 1: a kernel idle past the threshold is culled, and a
    subsequent exec re-spawns it (works)."""
    build, spawned = make_manager
    mgr = build(threshold=10.0)

    # t = 1000.0 — first exec spawns inner #1, last_exec_end_at = 1000.0
    res1 = await mgr.execute("a = 1", timeout_s=5)
    assert res1.stdout == "fake#1:a = 1"
    assert len(spawned) == 1
    assert spawned[0].started is True
    assert spawned[0].was_shut_down is False
    assert mgr.cull_count == 0
    assert mgr.spawn_count == 1

    # Advance 4s — t = 1004.0, 4s idle since last exec. Within the 10s window.
    clock.advance(4.0)
    res2 = await mgr.execute("a = 2", timeout_s=5)
    assert res2.stdout == "fake#1:a = 2"
    assert len(spawned) == 1
    assert mgr.cull_count == 0

    # Advance 11s — t = 1015.0, 11s idle since last exec. Past the 10s window.
    clock.advance(11.0)
    res3 = await mgr.execute("a = 3", timeout_s=5)
    # The old inner was shut down; a fresh one (#2) was spawned and ran the cell.
    assert res3.stdout == "fake#2:a = 3"
    assert len(spawned) == 2
    assert spawned[0].was_shut_down is True, "stale inner must be shut down on cull"
    assert spawned[1].started is True, "fresh inner must be started before exec"
    assert mgr.cull_count == 1
    assert mgr.spawn_count == 2
    assert mgr.exec_count == 3

    # State the new inner has: it ran exactly one cell so far.
    assert spawned[1].exec_calls == 1


@pytest.mark.asyncio
async def test_active_kernel_is_not_culled(clock: _Clock, make_manager) -> None:
    """ACCEPTANCE 2: a kernel used within the window is NOT culled."""
    build, spawned = make_manager
    mgr = build(threshold=10.0)

    # 8 execs spaced 1s apart — every idle gap is 1s, well within 10s.
    for i in range(8):
        res = await mgr.execute(f"step_{i}", timeout_s=5)
        assert res.stdout == f"fake#1:step_{i}"
        clock.advance(1.0)

    # Still the same inner; cull never fired.
    assert len(spawned) == 1
    assert mgr.cull_count == 0
    assert mgr.exec_count == 8
    assert spawned[0].was_shut_down is False
    assert spawned[0].exec_calls == 8


# ---------------------------------------------------------------------------
# Boundary / semantics — protect the contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_threshold_zero_culls_every_exec(clock: _Clock, make_manager) -> None:
    """Threshold 0 -> cull on the very next exec, because `now - last_exec_end_at`
    is always > 0 (clock advanced by the time of the next call). Set 0 in
    production to effectively disable, but the contract is "anything past N"."""
    build, spawned = make_manager
    mgr = build(threshold=0.0)
    await mgr.execute("a = 1", timeout_s=5)
    clock.advance(0.5)
    await mgr.execute("a = 2", timeout_s=5)
    assert mgr.cull_count == 1
    assert len(spawned) == 2
    # The new inner ran the second cell.
    assert spawned[1].exec_calls == 1
    assert spawned[0].was_shut_down is True


@pytest.mark.asyncio
async def test_freshly_spawned_kernel_is_not_immediately_culled(
    clock: _Clock,
) -> None:
    """A slow `start()` (e.g. jupyter's `wait_for_ready`) must not burn into
    the cull timer. The manager captures `last_exec_end_at` AFTER `start()`
    returns, so the first exec always uses the new inner and the cull timer
    starts fresh from end-of-first-exec."""

    # A kernel whose `start()` advances the clock by 100s — simulates a
    # slow jupyter wait_for_ready.
    class SlowStartKernel(_FakeKernel):
        async def start(self) -> None:
            clock.advance(100.0)
            await super().start()

    spawned: list[_FakeKernel] = []
    mgr = ManagedKernel(
        lambda: (k := SlowStartKernel(), spawned.append(k))[0],  # type: ignore[misc]
        idle_timeout_s=10.0,
        time_source=clock,
    )

    # First exec: spawn + start (advances 100s) + exec. The manager captures
    # last_exec_end_at AFTER start, so the 100s of start-time does NOT eat
    # into the cull threshold for subsequent execs.
    res1 = await mgr.execute("a = 1", timeout_s=5)
    assert res1.stdout == "fake#1:a = 1"
    assert mgr.cull_count == 0
    assert len(spawned) == 1

    # 5s later, still within the 10s window measured from end-of-first-exec.
    clock.advance(5.0)
    res2 = await mgr.execute("a = 2", timeout_s=5)
    assert res2.stdout == "fake#1:a = 2", "slow start must not cause spurious cull"
    assert mgr.cull_count == 0
    assert len(spawned) == 1


@pytest.mark.asyncio
async def test_failed_exec_still_marks_recent_use(clock: _Clock) -> None:
    """If the inner raises, the manager must still record last_exec_end_at —
    otherwise a failing cell would cause a spurious cull on the agent's very
    next retry (the same broken kernel would be replaced even though the
    agent's first action was to use it)."""
    state = {"calls": 0}

    async def first_raises_then_recovers(_code: str, *, timeout_s: int) -> KernelResult:
        state["calls"] += 1
        if state["calls"] == 1:
            raise RuntimeError("kernel crashed")
        return KernelResult(ok=True, stdout="recovered", stderr="")

    class FlakyKernel(_FakeKernel):
        def __init__(self) -> None:
            super().__init__()
            # Replace the bound method so the manager's inner.execute() call
            # hits our flaky coroutine, not the base class's.
            self.execute = first_raises_then_recovers  # type: ignore[method-assign]

    spawned: list[_FakeKernel] = []
    mgr = ManagedKernel(
        lambda: (k := FlakyKernel(), spawned.append(k))[0],  # type: ignore[misc]
        idle_timeout_s=10.0,
        time_source=clock,
    )

    with pytest.raises(RuntimeError):
        await mgr.execute("x", timeout_s=5)
    assert mgr.cull_count == 0
    assert len(spawned) == 1

    # Advance 5s — still within the 10s window. The cull MUST NOT fire, so
    # the second exec reuses the same kernel instance.
    clock.advance(5.0)
    res = await mgr.execute("y", timeout_s=5)
    assert res.stdout == "recovered"
    assert mgr.cull_count == 0
    assert len(spawned) == 1, "same kernel reused within the window"


@pytest.mark.asyncio
async def test_cull_tolerates_inner_shutdown_failure(clock: _Clock, make_manager) -> None:
    """If the stale inner's `shutdown()` raises, we MUST still spawn a fresh
    kernel — the agent's next exec has to land on a working kernel, not fail
    because the dying one choked."""
    build, spawned = make_manager
    mgr = build(threshold=1.0)
    await mgr.execute("x", timeout_s=5)

    async def boom() -> None:
        raise RuntimeError("kernel already dead")

    spawned[0].shutdown_impl = boom  # type: ignore[assignment]

    clock.advance(5.0)
    res = await mgr.execute("y", timeout_s=5)
    # Fresh inner was spawned and the cell ran.
    assert res.stdout == "fake#2:y"
    assert mgr.cull_count == 1
    assert len(spawned) == 2


@pytest.mark.asyncio
async def test_explicit_shutdown_then_respawn(clock: _Clock, make_manager) -> None:
    """`ManagedKernel.shutdown()` is distinct from an idle cull: it's an
    explicit tear-down. The next exec must spawn a fresh inner, but
    `cull_count` should not be incremented (this is a deliberate shutdown,
    not a cull)."""
    build, spawned = make_manager
    mgr = build(threshold=999.0)  # well above any test clock movement
    await mgr.execute("x", timeout_s=5)
    assert mgr.exec_count == 1
    assert mgr.cull_count == 0

    await mgr.shutdown()
    assert spawned[0].was_shut_down is True
    assert mgr.inner is None

    res = await mgr.execute("y", timeout_s=5)
    assert res.stdout == "fake#2:y"
    assert len(spawned) == 2
    assert mgr.cull_count == 0  # not a cull
    assert mgr.spawn_count == 2


@pytest.mark.asyncio
async def test_restart_resets_idle_clock(clock: _Clock, make_manager) -> None:
    """`restart()` is a user-driven action: the kernel is back and ready, so
    it should be treated as recently-used (the cull threshold restarts)."""
    build, spawned = make_manager
    mgr = build(threshold=10.0)
    await mgr.execute("x", timeout_s=5)

    # Advance just under the threshold, restart (resets clock), advance
    # the same amount again, exec — no cull.
    clock.advance(9.0)
    await mgr.restart()
    assert spawned[0].restart_calls == 1
    clock.advance(9.0)
    res = await mgr.execute("y", timeout_s=5)
    assert res.stdout == "fake#1:y"
    assert mgr.cull_count == 0
    assert len(spawned) == 1


@pytest.mark.asyncio
async def test_interrupt_does_not_reset_idle_clock(clock: _Clock, make_manager) -> None:
    """`interrupt()` is mid-exec cleanup — it does NOT extend the idle window.
    A kernel that was idle past the threshold is still culled on the next
    exec, even if interrupt() was called in the meantime."""
    build, spawned = make_manager
    mgr = build(threshold=10.0)
    await mgr.execute("x", timeout_s=5)

    clock.advance(11.0)  # past the 10s threshold
    await mgr.interrupt()  # shouldn't change the cull decision
    res = await mgr.execute("y", timeout_s=5)
    # Cull happened — interrupt is a no-op for the cull state machine.
    assert res.stdout == "fake#2:y"
    assert mgr.cull_count == 1
    assert len(spawned) == 2


# ---------------------------------------------------------------------------
# Active-path overhead — the cull must not slow down normal exec
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_active_exec_path_one_time_read(clock: _Clock, make_manager) -> None:
    """The cull check is a single float comparison at the start of execute().
    We assert that the active path does not increment cull_count and the
    inner kernel is reused — proxy for "no respawn latency injected"."""
    build, spawned = make_manager
    mgr = build(threshold=999.0)  # no cull will ever fire
    for i in range(20):
        res = await mgr.execute(f"step_{i}", timeout_s=5)
        assert res.stdout == f"fake#1:step_{i}"
        clock.advance(0.5)  # 0.5s idle, well within 999s
    assert len(spawned) == 1
    assert mgr.cull_count == 0
    assert spawned[0].exec_calls == 20


# ---------------------------------------------------------------------------
# Env-var knob
# ---------------------------------------------------------------------------


def test_env_default_idle_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """`DISCO_KERNEL_IDLE_TIMEOUT_S` controls the threshold; missing ->
    300.0; malformed -> 300.0; negative -> 0.0 (clamped)."""
    # Missing -> default
    monkeypatch.delenv("DISCO_KERNEL_IDLE_TIMEOUT_S", raising=False)
    assert _default_idle_timeout_s() == 300.0

    # Integer seconds
    monkeypatch.setenv("DISCO_KERNEL_IDLE_TIMEOUT_S", "42")
    assert _default_idle_timeout_s() == 42.0

    # Float seconds
    monkeypatch.setenv("DISCO_KERNEL_IDLE_TIMEOUT_S", "12.5")
    assert _default_idle_timeout_s() == 12.5

    # 0 -> disables culling
    monkeypatch.setenv("DISCO_KERNEL_IDLE_TIMEOUT_S", "0")
    assert _default_idle_timeout_s() == 0.0

    # Negative -> clamped to 0
    monkeypatch.setenv("DISCO_KERNEL_IDLE_TIMEOUT_S", "-5")
    assert _default_idle_timeout_s() == 0.0

    # Garbage -> default + warning (we just assert it doesn't crash)
    monkeypatch.setenv("DISCO_KERNEL_IDLE_TIMEOUT_S", "not-a-float")
    assert _default_idle_timeout_s() == 300.0

    # Empty -> default
    monkeypatch.setenv("DISCO_KERNEL_IDLE_TIMEOUT_S", "")
    assert _default_idle_timeout_s() == 300.0


@pytest.mark.asyncio
async def test_env_var_is_actually_honored(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock, make_manager
) -> None:
    """Integration: the env var flows through `_default_idle_timeout_s` and
    is what `SandboxSession` reads by default. We exercise the helper
    directly to prove the knob is wired."""
    monkeypatch.setenv("DISCO_KERNEL_IDLE_TIMEOUT_S", "5")
    assert _default_idle_timeout_s() == 5.0

    build, spawned = make_manager
    # Use the env-resolved threshold by hand for the test.
    mgr = build(threshold=_default_idle_timeout_s())
    await mgr.execute("x", timeout_s=5)
    clock.advance(6.0)  # past 5s
    res = await mgr.execute("y", timeout_s=5)
    assert res.stdout == "fake#2:y"
    assert mgr.cull_count == 1
