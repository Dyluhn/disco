"""Shared sandbox reachability probe — ONE classifier for both servers.

The reachability signal the UI surfaces (the health banner + the Settings "Test
connection" verdict) MUST reflect the environment the agent-server actually runs
sandboxes in. In a split-container deployment the app-server has no Docker socket,
so it cannot probe a `local` backend — the probe has to run on the agent-server,
against the SAME service the run path builds. This module is that shared probe so
the two callers (app-server preflight + agent-server route) classify identically
and can never drift.
"""

from __future__ import annotations

import asyncio

from .base import SandboxService, SandboxUnavailableError

# HARD wall-clock bound on a connectivity probe — a "test" must feel instant and
# never hang on a black-holed gVisor/Podman host. A timeout is itself "unreachable".
DEFAULT_PROBE_TIMEOUT_S = 12.0


def sandbox_endpoint_label(backend: str, docker_socket: str, podman_url: str) -> str:
    """A human, host-NAMING label for the probed endpoint (so an unreachable
    verdict points at WHAT was probed, not a bare error)."""
    if backend in ("gvisor", "local"):
        return f"{backend} sandbox host {docker_socket}"
    if backend == "podman":
        return f"podman sandbox host {podman_url}"
    return f"{backend} sandbox"


async def probe_sandbox_reachability(
    service: SandboxService,
    endpoint: str,
    *,
    timeout_s: float = DEFAULT_PROBE_TIMEOUT_S,
) -> tuple[bool, str, str]:
    """Run the backend's own ``healthcheck()`` and classify → (ok, status, detail).

    Never raises: a probe failure is a RESULT (ok=False + a typed, host-naming
    detail), so the caller returns HTTP 200 with the truth instead of a 500.
    ``status`` ∈ ok | unreachable | error, matching the ProbeResult vocabulary."""
    try:
        await asyncio.wait_for(service.healthcheck(), timeout_s)
    except TimeoutError:
        return (
            False,
            "unreachable",
            f"{endpoint} unreachable: no response within {timeout_s:.0f}s "
            "(the probe timed out).",
        )
    except SandboxUnavailableError as exc:
        return False, "unreachable", f"{endpoint} unreachable: {exc}"
    except Exception as exc:  # noqa: BLE001 — any probe failure is a RESULT, not a 500
        return False, "error", f"{endpoint}: {exc}"
    return True, "ok", f"{endpoint} is reachable."
