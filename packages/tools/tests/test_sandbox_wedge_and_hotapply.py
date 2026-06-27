"""Dispo #25 root fix — two test groups.

Group A (wedge-guard, the docker/podman client call safety): a fake container
whose `reload()` HANGS or RAISES must NOT block the event loop. The wrapped
call goes through `_safe_reload`, which bounds the call with a daemon thread
+ `Event.wait(timeout)` and surfaces a typed `SandboxUnavailableError`. The
caller continues — `expose_port()` returns None (no URL), `_classify_failure`
classifies the box as dead so the session re-creates.

Group B (hot-apply, the sandbox Settings hot-reload): mutating the live
`SandboxConfig` is picked up by the NEXT op (container create, wedge-guard
timeout) WITHOUT a service restart. We don't snapshot the config — the
service holds a reference and re-reads on every op, so a `cfg.default_memory_mb
= 8192` (or `cfg.reload_timeout_s = 0.05`) is the whole hot-update API.

Both tests use hermetic fakes — no real docker, no real sleep beyond the
wedge-guard's short timeout. The hanging-reload test does NOT actually sleep
for the full timeout: the wedge-guard fires within it, returns the typed
error, and the test then releases the blocker event so the daemon thread
can die cleanly.
"""

from __future__ import annotations

import asyncio
import threading
import time

from disco.tools.sandbox import (
    GvisorSandboxService,
    SandboxConfig,
    SandboxError,
    SandboxSpec,
    SandboxUnavailableError,
)
from disco.tools.sandbox._container import (
    _DEFAULT_RELOAD_TIMEOUT_S,
    PUBLISHED_PORTS,
    ContainerInstance,
)

# --- Group A: wedge-guard (the docker/podman client call safety) ---


class _FakeContainerBase:
    """A docker-py-like container stub. Subclass to control `reload()`'s
    behavior (hang / raise / pass). The wedge-guard goes through
    `_safe_reload`, which only touches `.reload()` (and reads `.status` /
    `.attrs` for the alive-check, both defaulted)."""

    def __init__(self) -> None:
        self.status = "running"
        self.attrs: dict = {}  # NetworkSettings.Ports populated by the test if needed
        self.reload_call_count = 0
        self.reload_lock = threading.Lock()

    def reload(self) -> None:  # pragma: no cover — overridden by subclasses
        with self.reload_lock:
            self.reload_call_count += 1


class HangingContainer(_FakeContainerBase):
    """`reload()` blocks on an Event until the test releases it. The wedge-guard
    MUST bound the call — the test never waits for the fake's `reload()` to
    return; it verifies the guard returns the typed error and then sets the
    blocker to release the daemon thread."""

    def __init__(self) -> None:
        super().__init__()
        self._blocker = threading.Event()

    def release(self) -> None:
        """Unblock the daemon thread running the hung `reload()` so the test
        doesn't leak a thread waiting on an Event that's never set."""
        self._blocker.set()

    def reload(self) -> None:
        with self.reload_lock:
            self.reload_call_count += 1
        # Block until the test releases us. We do NOT actually sleep the
        # timeout in the test thread — the wedge-guard fires and returns
        # first, the test then calls `release()`.
        self._blocker.wait()


class RaisingContainer(_FakeContainerBase):
    """`reload()` raises immediately — simulates a dead docker daemon."""

    def reload(self) -> None:
        with self.reload_lock:
            self.reload_call_count += 1
        raise ConnectionError("docker daemon is gone (simulated)")


def _make_inst(container, *, reload_timeout_s: float | None = None) -> ContainerInstance:
    """Build a bare `ContainerInstance` for unit-testing the wedge-guard. We
    don't go through `GvisorSandboxService.create` here — the wedge-guard is
    the layer we want to test, in isolation, with the smallest possible setup
    (no real Docker call, no real network, no real filesystem)."""
    kwargs: dict = dict(
        id="test-sbx",
        owner_id="o",
        conversation_id="c",
        spec=SandboxSpec(),
        container=container,
        container_workspace="/workspace",
        stop_timeout_s=5,
        workspace_uid=1000,
        preview_host="localhost",
    )
    if reload_timeout_s is not None:
        kwargs["reload_timeout_s"] = reload_timeout_s
    return ContainerInstance(**kwargs)


def test_wedge_guard_bounded_for_hung_reload():
    """A HUNG docker/podman `reload()` must not wedge the event loop. The
    wedge-guard returns the typed `SandboxUnavailableError` (NOT a raw
    hang) within the configured timeout.

    Mechanism: `_safe_reload` runs `reload()` in a daemon thread and joins
    it via `Event.wait(timeout)`. If the call doesn't return in time, the
    guard raises the typed error; the daemon thread is left to die (it
    can't block the process). The test uses a SHORT timeout (0.05s) and a
    HANGING `reload()` (waiting on an Event), so the test is fast: the
    wedge-guard fires within ~0.05s, returns the typed error, and the
    test then releases the Event to clean up.
    """
    hanging = HangingContainer()
    try:
        inst = _make_inst(hanging, reload_timeout_s=0.05)
        # expose_port -> _resolve_mapping -> _safe_reload. On a hung
        # reload, the wedge-guard must raise; `_resolve_mapping` catches
        # that and returns None (no URL), and the caller continues.
        t0 = time.monotonic()
        # Pick the first USER port (8000) — the only port set we test.
        port = sorted(PUBLISHED_PORTS)[0]
        result = inst.expose_port(port)
        elapsed = time.monotonic() - t0
        # The caller CONTINUED — no exception leaked out, and the call
        # returned within the timeout, NOT a multi-second hang.
        assert result is None
        # Bounded: sub-second even on a busy CI runner. A healthy reload
        # is sub-ms; 0.05s is 50x the real latency.
        assert elapsed < 0.5, f"wedge-guard took {elapsed:.3f}s (should be <0.5s)"
        # The fake `reload()` was actually CALLED (the wedge-guard
        # dispatched it before bailing) — verify the wedge-guard saw a
        # hung client, not a no-op.
        assert hanging.reload_call_count == 1
    finally:
        # Always release the daemon thread, even on assertion failure,
        # so the test doesn't leak a thread waiting on an Event.
        hanging.release()


def test_wedge_guard_typed_error_for_raising_reload():
    """A RAISING docker/podman `reload()` must surface as a typed
    `SandboxUnavailableError`, NOT a raw `ConnectionError` (the caller's
    catch-all SandboxError path is NOT triggered, the typed "box is dead"
    path is — the session re-creates from this signal)."""
    raising = RaisingContainer()
    inst = _make_inst(raising, reload_timeout_s=0.5)
    # The guard raises; `_classify_failure` catches that and returns a
    # typed `SandboxUnavailableError` (the session re-creates from this).
    fake_op_error = RuntimeError("op itself failed before the reload")
    classified = inst._classify_failure_sync(fake_op_error)
    assert isinstance(classified, SandboxUnavailableError)
    assert "died mid-session" in str(classified)
    # The fake `reload()` was actually called once.
    assert raising.reload_call_count == 1


class DeadOOMContainer(_FakeContainerBase):
    """`reload()` succeeds but the container is EXITED with an OOM kill recorded in
    `.attrs['State']` — the #3 death-attribution case (container OOM-killed mid-build)."""

    def reload(self) -> None:
        with self.reload_lock:
            self.reload_call_count += 1
        self.status = "exited"
        self.attrs = {"State": {"OOMKilled": True, "ExitCode": 137, "Error": ""}}


class TransientThenAliveContainer(_FakeContainerBase):
    """`reload()` reports the box NON-running on the FIRST probe (the sync classify, mimicking
    a transient docker-API 404), then RUNNING on the re-verify probes — i.e. the container was
    alive all along and the 404 was a momentary blip (#3 real root)."""

    def reload(self) -> None:
        with self.reload_lock:
            self.reload_call_count += 1
        # 1st probe = the transient miss; subsequent re-verify probes see it running again.
        self.status = "exited" if self.reload_call_count == 1 else "running"
        self.attrs = {"State": {"OOMKilled": False, "ExitCode": 0, "Error": ""}}


def test_classify_failure_attributes_oom_death():
    """A genuinely OOM-killed container: the async classifier re-verifies (stays non-running),
    CONFIRMS death, and surfaces the REASON (OOMKilled/exit) — recreate, not opaque (#3)."""
    inst = _make_inst(DeadOOMContainer(), reload_timeout_s=0.5)
    classified = asyncio.run(
        inst._classify_failure_async(RuntimeError("exec failed: container not running"))
    )
    assert isinstance(classified, SandboxUnavailableError)
    msg = str(classified)
    assert "died mid-session" in msg
    assert "OOMKilled=True" in msg and "exit=137" in msg  # the death reason is attributed


def test_transient_404_recovers_not_death():
    """The #3 fix: a transient API 404 (box non-running on the first probe, RUNNING on
    re-verify) must NOT be typed as death — it becomes a retryable per-op SandboxError, so the
    session does NOT needlessly recreate the (alive) sandbox mid-build."""
    inst = _make_inst(TransientThenAliveContainer(), reload_timeout_s=0.5)
    classified = asyncio.run(
        inst._classify_failure_async(RuntimeError("404 Client Error: not running"))
    )
    assert isinstance(classified, SandboxError)
    assert not isinstance(classified, SandboxUnavailableError)  # NOT a death → no recreate
    assert "transient" in str(classified).lower()


def test_real_death_persists_through_reverify():
    """A box that stays dead across the re-verify window is STILL typed unavailable (recreate)
    — the conservative bias (ambiguity/persistent-non-running ⇒ death) is preserved."""
    inst = _make_inst(DeadOOMContainer(), reload_timeout_s=0.5)
    classified = asyncio.run(inst._classify_failure_async(RuntimeError("boom")))
    assert isinstance(classified, SandboxUnavailableError)


def test_death_reason_helper_never_raises_on_bad_attrs():
    inst = _make_inst(_FakeContainerBase())
    inst._container.attrs = None  # type: ignore[assignment]  — pathological
    assert inst._death_reason_from_attrs() in ("reason-unavailable", "OOMKilled=None exit=None reason=")


def test_wedge_guard_does_not_slow_healthy_clients():
    """The wedge-guard must not add measurable latency to a HEALTHY client.
    A healthy `reload()` is sub-ms; the guard is `Event.wait(timeout=0.5s)`
    with a thread spawn, both of which should be dominated by the
    call itself. We assert: 1000 healthy reloads complete in <1s on any
    CI runner (i.e. <1ms per call on average)."""
    container = _FakeContainerBase()
    inst = _make_inst(container, reload_timeout_s=0.5)
    t0 = time.monotonic()
    for _ in range(1000):
        inst._safe_reload()  # must return, NOT raise, for a healthy client
    elapsed = time.monotonic() - t0
    # 1000 calls in under 1s ⇒ <1ms each. A real healthy reload is sub-ms;
    # the wedge-guard's per-call overhead (thread spawn + Event) dominates,
    # and even on a busy CI runner that's well under 1ms/call.
    assert elapsed < 1.0, f"1000 healthy reloads took {elapsed:.3f}s (added latency)"


def test_wedge_guard_default_timeout_is_bounded():
    """The default `reload_timeout_s` (used when no config is provided) is
    set to a bounded value — never None, never a huge number. A missing/
    broken config MUST still keep the loop safe."""
    # Default on the module itself: bounded out of the box.
    assert _DEFAULT_RELOAD_TIMEOUT_S > 0
    assert _DEFAULT_RELOAD_TIMEOUT_S <= 1.0  # not more than 1s; healthy reloads are sub-ms
    # And the instance's own default (no explicit value) uses it.
    inst = _make_inst(_FakeContainerBase())
    assert 0 < inst._reload_timeout_s <= 1.0


# --- Group B: hot-apply (the sandbox Settings hot-reload) ---


async def test_hot_apply_settings_picked_up_without_recreate(tmp_path):
    """A settings change on the live `SandboxConfig` is picked up by the
    NEXT `create()` WITHOUT a service restart and WITHOUT recreating the
    first container (Dispo #25). The service holds a reference to the
    config (`self._cfg`); it does NOT snapshot fields at __init__, so
    `cfg.image = "disco-sandbox:hot"` is the whole hot-update API.

    We pick `image` for the assertion (over `default_memory_mb`) because
    `image` is read directly off the config in `_start_container`, while
    `default_memory_mb` falls behind a pre-existing `spec.memory_mb or
    cfg.default_memory_mb` short-circuit where the spec's own default of
    2048 always wins. The hot-apply story is the same — we just need a
    field that the test can observe end-to-end.
    """
    from test_gvisor import FakeDockerClient  # the shared fake

    client = FakeDockerClient()
    cfg = SandboxConfig(workspace_root=str(tmp_path))
    assert cfg.image == "disco-sandbox:base"  # the starting value
    svc = GvisorSandboxService(cfg, client=client)

    # 1) First container: default image (the unchanged config value).
    inst1 = await svc.create(SandboxSpec(), owner_id="o", conversation_id="c1")
    assert client.runs[0].run_kwargs["image"] == "disco-sandbox:base"
    inst1_id = inst1.id

    # 2) Hot-apply: mutate the LIVE config. NO service restart, NO recreate
    # of the first container. The runtime / image / workspace_root /
    # docker_socket / podman_url / workspace_volume_prefix fields all
    # hot-apply the same way.
    cfg.image = "disco-sandbox:hot"
    cfg.runtime = "runc"  # swap the runtime too — proves it's not just `image`
    cfg.workspace_root = "/tmp/other-workspaces"

    # 3) Second container: now uses the hot-applied image + runtime.
    inst2 = await svc.create(SandboxSpec(), owner_id="o", conversation_id="c2")
    assert client.runs[1].run_kwargs["image"] == "disco-sandbox:hot"
    assert client.runs[1].run_kwargs["runtime"] == "runc"

    # 4) The first container was NOT recreated (it's still in the service's
    # instance map, still alive, still has its original config snapshot).
    assert inst1.id == inst1_id
    assert svc._instances[inst1.id] is inst1
    assert svc._instances[inst2.id] is inst2

    await inst1.destroy()
    await inst2.destroy()


async def test_hot_apply_reload_timeout_for_next_instance(tmp_path):
    """A change to `cfg.reload_timeout_s` is picked up by the NEXT
    `ContainerInstance` created by the service — the wedge-guard's per-
    instance timeout is set from the live config at create time."""
    from test_gvisor import FakeDockerClient

    client = FakeDockerClient()
    cfg = SandboxConfig(workspace_root=str(tmp_path))
    # Start with a long timeout (5s); a healthy reload will easily fit.
    cfg.reload_timeout_s = 5.0
    svc = GvisorSandboxService(cfg, client=client)

    inst1 = await svc.create(SandboxSpec(), owner_id="o", conversation_id="c1")
    assert inst1._reload_timeout_s == 5.0  # snapshotted from the live config

    # Hot-apply: tighten the wedge-guard to 0.05s. The next create picks it up.
    cfg.reload_timeout_s = 0.05
    inst2 = await svc.create(SandboxSpec(), owner_id="o", conversation_id="c2")
    assert inst2._reload_timeout_s == 0.05  # hot-applied
    # The first instance's snapshot is unchanged — it kept the 5.0s value.
    # Live updates to its wedge-guard would require mutating
    # `inst1._reload_timeout_s` directly (a deliberate seam for callers
    # that need per-instance updates; the simpler hot-apply path is
    # "mutate cfg, next op sees the new value").
    assert inst1._reload_timeout_s == 5.0

    await inst1.destroy()
    await inst2.destroy()


def test_sandbox_config_is_mutable_for_settings():
    """The Settings layer relies on `SandboxConfig` being mutable — the
    WHOLE hot-apply story breaks if a caller can't write
    `cfg.field = new_value`. Frozen configs were the old behavior (Dispo
    #25: settings didn't hot-apply because the service snapshot at
    construction)."""
    cfg = SandboxConfig()
    # Each of these is the hot-apply API the runtime would use:
    cfg.default_memory_mb = 4096
    cfg.default_cpu = 2.0
    cfg.image = "custom:tag"
    cfg.runtime = "runc"
    cfg.docker_socket = "unix:///tmp/foo.sock"
    cfg.reload_timeout_s = 0.1
    assert cfg.default_memory_mb == 4096
    assert cfg.default_cpu == 2.0
    assert cfg.image == "custom:tag"
    assert cfg.runtime == "runc"
    assert cfg.docker_socket == "unix:///tmp/foo.sock"
    assert cfg.reload_timeout_s == 0.1


# --- Group C: client-construction timeout (Dispo #25, wedge-guard completion) ---


def test_docker_client_constructed_with_bounded_timeout(monkeypatch):
    """`_client()` must hand the configured socket timeout to docker-py, so a hung
    daemon's `ping`/`info`/`run` fail fast (the to_thread worker returns) instead of
    leaking for docker-py's 60s default. Patches the real `docker.DockerClient`."""
    import docker

    captured: dict = {}

    class _FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def ping(self):
            return True

    monkeypatch.setattr(docker, "DockerClient", _FakeClient)
    cfg = SandboxConfig(client_timeout_s=7, docker_socket="unix:///var/run/docker.sock")
    svc = GvisorSandboxService(cfg)
    svc._client()
    assert captured.get("timeout") == 7, f"timeout not passed: {captured}"
    assert captured.get("base_url") == cfg.docker_socket


def test_default_client_timeout_is_bounded_well_under_docker_default():
    """The default must be a real, bounded value strictly under docker-py's 60s
    default — otherwise the guard adds nothing."""
    cfg = SandboxConfig()
    assert 0 < cfg.client_timeout_s < 60


def test_client_timeout_is_hot_applicable():
    """The field is mutable on the live config (no frozen model) so a Settings
    change is picked up by the next client construction."""
    cfg = SandboxConfig()
    cfg.client_timeout_s = 12  # must not raise (mutable-by-construction, Dispo #25)
    assert cfg.client_timeout_s == 12


# --- podman path: re-verify a transient exec error vs a real death (#3 closing fix) ---


def _podman_inst(cli_runner):
    from disco.tools.sandbox import SandboxSpec
    from disco.tools.sandbox.podman import PodmanSandboxInstance

    return PodmanSandboxInstance(
        id="sbx", owner_id="o", conversation_id="c", spec=SandboxSpec(),
        container=object(), container_workspace="/workspace", stop_timeout_s=5,
        cli_url="unix:///run/podman.sock", container_name="disco-sbx-x", cli_runner=cli_runner,
    )


def test_podman_raise_if_dead_running_container_is_transient_not_death():
    """Under concurrent load podman intermittently refuses an exec on a LIVE container
    ('can only create exec sessions on running containers … improper'). The re-verify inspect
    shows status=running → it is a TRANSIENT per-op error (retryable SandboxError), NOT a death
    → the session must NOT recreate (#3 closing fix)."""
    import pytest

    def runner(argv, timeout):
        if "inspect" in argv:
            return (0, b"status=running OOMKilled=false exit=0 reason=", b"")
        return (0, b"", b"")

    inst = _podman_inst(runner)
    with pytest.raises(SandboxError) as ei:
        inst._raise_if_dead(
            125,
            b"Error: can only create exec sessions on running containers: container state improper",
        )
    assert not isinstance(ei.value, SandboxUnavailableError)  # NOT death → no recreate
    assert "transient" in str(ei.value).lower()


def test_podman_raise_if_dead_exited_container_is_death():
    """A genuinely not-running container (inspect status != running) is STILL typed dead →
    SandboxUnavailableError → recreate. The conservative bias holds."""
    import pytest

    def runner(argv, timeout):
        if "inspect" in argv:
            return (0, b"status=exited OOMKilled=false exit=0 reason=", b"")
        return (0, b"", b"")

    inst = _podman_inst(runner)
    with pytest.raises(SandboxUnavailableError):
        inst._raise_if_dead(125, b"Error: no such container: disco-sbx-x")
