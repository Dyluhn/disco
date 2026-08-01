"""Sandbox-admin repository split out of ConfigState (PY-0365): the persisted
sandbox backend + connection, its structural save-time validation (the
2026-06-23 outage guard), and the connectivity preflight probe the Settings
Save flow runs after persisting. `ConfigState` exposes an instance of this
class as the plain `sandbox` attribute.
"""

from __future__ import annotations

from disco.core.llm import ConfigStore

from .config.dtos import ProbeResult, SandboxConfigDTO, SandboxHealthDTO
from .config.mappers import _sandbox_from
from .config_state_errors import ConfigValidationError


class ConfigSandboxAdmin:
    """The persisted sandbox backend, its structural validation, and its probes."""

    # W-48: HARD wall-clock bound on the sandbox connectivity probe — a "test" must
    # feel instant and never hang Settings on a black-holed gVisor/Podman host. The
    # backends' own client_timeout_s bounds the socket, but the SSH transport is
    # outside that, so this caps the WHOLE probe; a timeout is itself "unreachable".
    _SANDBOX_PROBE_TIMEOUT_S = 12.0

    def __init__(self, store: ConfigStore) -> None:
        self._store = store

    def sandbox_config(self) -> SandboxConfigDTO:
        return _sandbox_from(self._store.load())

    def update_sandbox_config(self, dto: SandboxConfigDTO) -> SandboxConfigDTO:
        """Persist the chosen backend + connection. The agent-server reloads the shared
        config per request, so a new selection drives the NEXT conversation's sandbox.

        W-48: the connectivity PREFLIGHT is a SEPARATE probe (`test_sandbox` /
        POST /api/sandbox/test) the Save flow runs after persisting, so a config is
        never LOST just because the host is momentarily down — the user saves, then
        sees the typed named reachability verdict and can fix the host. The
        agent-server ALSO re-probes on first use (first-kick pre-flight).

        STRUCTURAL validation (the outage guard): a `gvisor` backend with an empty /
        host-less `docker_socket` (or `podman` with an empty `podman_url`) is REJECTED
        with a typed ConfigValidationError → the endpoint maps it to 400, rather than
        silently persisting an unrunnable config that fails every later run. The
        per-backend `connections` map is preserved by the store, so the rejected save
        leaves the last-good block for that backend intact."""
        from disco.core.llm import SandboxConnection, SandboxSettings, sandbox_connection_error

        reason = sandbox_connection_error(
            dto.backend,
            SandboxConnection(
                docker_socket=dto.docker_socket,
                podman_url=dto.podman_url,
                runtime=dto.runtime,
                image=dto.image,
                workspace_root=dto.workspace_root,
            ),
        )
        if reason is not None:
            raise ConfigValidationError(
                "unrunnable_sandbox",
                detail=f"Can't save the {dto.backend} sandbox: {reason}.",
            )

        self._store.sections.save_sandbox(
            SandboxSettings(
                backend=dto.backend,
                docker_socket=dto.docker_socket,
                podman_url=dto.podman_url,
                runtime=dto.runtime,
                image=dto.image,
                workspace_root=dto.workspace_root,
            )
        )
        return _sandbox_from(self._store.load())

    async def sandbox_health(self) -> SandboxHealthDTO:
        """Reachability of the ACTIVE (persisted) sandbox backend — the cheap,
        side-effect-free signal the app shell surfaces as a banner BEFORE a run is
        started. Reuses the SAME `test_sandbox` probe (→ the same `healthcheck()` the
        run path hits), so the banner and the real run can't disagree. Never raises:
        a probe failure is a RESULT (reachable=False with a host-naming detail)."""
        dto = self.sandbox_config()
        probe = await self.test_sandbox(dto)
        return SandboxHealthDTO(
            reachable=probe.ok,
            backend=dto.backend,
            detail=probe.detail,
        )

    async def test_sandbox(self, dto: SandboxConfigDTO) -> ProbeResult:
        """W-48: connectivity PREFLIGHT for a sandbox backend. Build the SAME backend
        service the agent-server would (`service_from_config`) and run its
        `healthcheck()` — a REAL probe of the configured endpoint (Docker socket /
        ssh:// host / Podman socket / process workspace root). Classifies the outcome
        into the ProbeResult vocabulary with a typed, host-NAMING detail
        (e.g. "gvisor sandbox host ssh://sandbox@<host> unreachable: …") instead of a
        silent failure or a generic 500 later. Bounded by _SANDBOX_PROBE_TIMEOUT_S so a
        dead host fails fast. Never raises for an expected failure — it's a RESULT."""
        from disco.tools.sandbox import (
            SandboxConfig,
            probe_sandbox_reachability,
            sandbox_endpoint_label,
            service_from_config,
        )

        # NOTE: this co-located probe only reflects reality when the app-server SHARES the
        # sandbox host with the run path (dev / single-process). In a split-container deploy
        # the run path is the AGENT-server (which owns the container socket), so the live UI
        # probes THERE (routes/sandbox.py) — this path stays for the co-located case + tests.
        # The classifier is shared (probe_sandbox_reachability) so the two can never drift.
        cfg = SandboxConfig(
            backend=dto.backend,
            docker_socket=dto.docker_socket,
            podman_url=dto.podman_url,
            runtime=dto.runtime,
            image=dto.image,
            workspace_root=dto.workspace_root,
        )
        endpoint = sandbox_endpoint_label(dto.backend, dto.docker_socket, dto.podman_url)
        try:
            service = service_from_config(cfg)
        except Exception as exc:  # noqa: BLE001 — a construction failure is a RESULT
            return ProbeResult(
                ok=False, status="error", detail=f"{endpoint}: {exc}", provider=dto.backend
            )
        ok, status, detail = await probe_sandbox_reachability(
            service, endpoint, timeout_s=self._SANDBOX_PROBE_TIMEOUT_S
        )
        return ProbeResult(ok=ok, status=status, detail=detail, provider=dto.backend)
