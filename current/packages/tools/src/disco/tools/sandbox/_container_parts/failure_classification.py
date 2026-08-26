"""Wedge-guarded reload + death-vs-transient classification (`ContainerInstance`).

The load-bearing behavior: a raw backend throw usually means the box died, but
under concurrent load a transient docker/podman API error (a momentary 404
burst) can look identical. `_classify_failure_async` re-verifies across a
bounded window before accepting a death verdict, so a live container is never
misclassified into a needless recreate — and `_safe_reload` bounds every
`reload()` call so a hung/dead client can never wedge the event loop (the
original VM 201/202 incident this guards against).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import TYPE_CHECKING, Any

from ..base import SandboxError, SandboxUnavailableError

if TYPE_CHECKING:
    from .._container import ContainerInstance

_LOG = logging.getLogger(__name__)

# Re-verify cadence/window for confirming a container is REALLY dead (vs a transient
# docker/podman API 404 burst, which under concurrent load can last ~1s) before accepting a
# death verdict + recreating. The window must OUTLAST a typical burst so a fresh probe can read
# the container running again; it is fully async (asyncio.sleep) so the event loop never blocks.
_DEATH_REVERIFY_BACKOFF_S = 0.25
_DEATH_REVERIFY_WINDOW_S = 2.0


def safe_reload(instance: ContainerInstance, obj: Any = None) -> bool:
    """Bounded wrapper around `reload()` (docker-py / podman-py) on `obj`, which
    defaults to `instance._container`. [FIX6] passing the egress SIDECAR lets
    `_resolve_mapping` refresh the sidecar's published-port bindings under the
    SAME wedge-guard (the filtered-box preview is published on the sidecar).

    A hung or failing client must NEVER wedge the event loop. The original
    VM 201/202 incident that motivated this guard: a docker daemon stall
    caused the agent loop to block indefinitely inside `reload()`; the
    whole conversation hung waiting for an HTTP call that never returned.

    Mechanism: run the blocking call in a daemon thread, then wait on a
    `threading.Event` with `_reload_timeout_s`. The thread is `daemon=True`
    so even if it never returns, it cannot block process exit (the main
    thread bails out cleanly, the test fake's blocker is released by the
    test's `finally:` clause). Returning status: True if the reload
    succeeded AND the container reports 'running'; False on either a
    non-running state or a raised error.

    On ANY of:
      - the daemon thread didn't finish in `_reload_timeout_s` (hung client)
      - the daemon thread raised (dead daemon, connection refused, etc.)
    we raise a typed `SandboxUnavailableError`. The caller handles it
    uniformly — `_classify_failure` types the box as dead (session
    re-creates), `_resolve_mapping` returns None (no URL). The loop is
    NEVER blocked.

    Cost on a HEALTHY client: one thread spawn + one Event wait per call.
    That thread/Event boundary is mandatory for bounding a potentially
    blocking client call; the healthy path adds only the reload and status
    bookkeeping around it. Timing-sensitive checks should compare this path
    with an equivalent boundary under the same runner load.
    """
    target = instance._container if obj is None else obj
    done = threading.Event()
    # Result slot: written by the worker thread, read by the main thread
    # AFTER `done.wait()` returns. A small dict keeps the assignment atomic
    # under the GIL (no lock needed for the simple `setattr` we do here).
    result: dict[str, Any] = {"exc": None, "status": "unknown"}

    def _runner() -> None:
        try:
            target.reload()
            result["status"] = getattr(target, "status", "unknown")
        except Exception as exc:  # noqa: BLE001 — surface as typed infra error
            result["exc"] = exc
        finally:
            done.set()

    thread = threading.Thread(target=_runner, daemon=True, name="disco-sbx-reload")
    thread.start()
    if not done.wait(timeout=instance._reload_timeout_s):
        # Hung: the daemon thread is still running (we can't safely
        # interrupt a blocking C call in docker-py / podman-py), but
        # daemon=True means it can't block process exit, and we return
        # control to the event loop with a typed error. The caller
        # treats it as "box is dead / client is wedged" — uniformly.
        raise SandboxUnavailableError(
            f"sandbox client hung on reload() (>{instance._reload_timeout_s}s); "
            f"the container state is unverifiable"
        )
    if result["exc"] is not None:
        raise SandboxUnavailableError(f"sandbox client failed on reload(): {result['exc']}")
    return result["status"] == "running"


def classify_failure_sync(instance: ContainerInstance, exc: Exception) -> SandboxError:
    """Sync classify: a container op threw. If the box is no longer running (a SINGLE
    `_safe_reload` miss), type it dead (SandboxUnavailableError → session RE-CREATES);
    else a per-op SandboxError. Runs OFF the event loop (via to_thread from the async
    wrapper). Does NOT log the death warning — `_classify_failure_async` logs it ONLY
    after re-verify CONFIRMS death, so a TRANSIENT API 404 never emits a false death
    diagnostic. (Dispo #25 wedge-guard bounds the probe.)"""
    alive = False
    try:
        alive = instance._safe_reload()
    except SandboxUnavailableError:
        alive = False
    if not alive:
        return SandboxUnavailableError(f"sandbox container died mid-session: {exc}")
    return SandboxError(f"sandbox op failed in {instance.id}: {exc}")


async def confidently_alive(instance: ContainerInstance) -> bool:
    """STRICT liveness re-verify for a death verdict: FRESH probes across a bounded
    ~`_DEATH_REVERIFY_WINDOW_S` window (long enough to OUTLAST a transient docker/podman
    404 burst, ~1s under concurrent load). Returns True ONLY when a probe reads
    status=="running" (exactly what `_safe_reload` returns). ANY ambiguity — a raise,
    non-running, or the box dying for the whole window — returns False ⇒ the death stands.
    We override a death to "transient" only when we POSITIVELY CONFIRM running, because a
    real death misclassified as transient surfaces as repeated op failures (worse than a
    clean recreate). Off-loads each probe to a thread; sleeps are async so the event loop is
    never blocked (a real death just takes up to the window to confirm + recreate)."""
    deadline = time.monotonic() + _DEATH_REVERIFY_WINDOW_S
    while time.monotonic() < deadline:
        await asyncio.sleep(_DEATH_REVERIFY_BACKOFF_S)
        try:
            if await asyncio.to_thread(instance._safe_reload):
                return True
        except SandboxUnavailableError:
            pass
    return False


async def classify_failure_async(instance: ContainerInstance, exc: Exception) -> SandboxError:
    """Classify a raw op throw, RE-VERIFYING before accepting a death verdict. A transient
    docker/podman API 404 (a simultaneous burst under concurrent load) momentarily fails the
    reload on a container that is actually ALIVE — declaring death there triggers a needless
    sandbox RECREATE that derails the build (live-captured: 4 deaths, 0 OOM, all exit=0). So
    when the sync classify says death, re-probe; ONLY downgrade to a retryable per-op error if
    `_confidently_alive()` CONFIRMS running. Else the death stands + is logged with its
    attributed reason. Used by BOTH op paths (`_guarded`, `exec_shell`)."""
    base = await asyncio.to_thread(instance._classify_failure_sync, exc)
    if not isinstance(base, SandboxUnavailableError):
        return base
    if await instance._confidently_alive():
        _LOG.info(
            "sandbox container %s: transient API error, container running on re-verify: %s",
            instance.id,
            exc,
        )
        return SandboxError(f"transient sandbox API error in {instance.id}: {exc}")
    # Death CONFIRMED across the re-verify window — attribute it (OOMKilled/exit) in BOTH
    # the WARNING log and the returned error so a recreate is no longer opaque.
    reason = instance._death_reason_from_attrs()
    _LOG.warning("sandbox container %s died mid-session (%s): %s", instance.id, reason, exc)
    return SandboxUnavailableError(f"sandbox container died mid-session ({reason}): {exc}")


def death_reason_from_attrs(instance: ContainerInstance) -> str:
    """Best-effort read of the container's State (refreshed by _safe_reload) for
    OOMKilled / ExitCode / Error. NEVER raises (diagnostic on a failing path).

    The `getattr` is pre-existing docker-py/podman-py duck-typing, carried over
    unchanged from `ContainerInstance._death_reason_from_attrs`; the only edit is
    the receiver (`self` → the `instance` parameter) now that this is a free
    function. Not a new shim.
    """
    try:
        state = (getattr(instance._container, "attrs", {}) or {}).get("State", {}) or {}
        return (
            f"OOMKilled={state.get('OOMKilled')} exit={state.get('ExitCode')} "
            f"reason={state.get('Error') or ''}".strip()
        )
    except Exception:  # noqa: BLE001 — diagnostic only
        return "reason-unavailable"
