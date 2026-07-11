"""Sandbox subsystem — spec/instance/service + backends (tool-sandbox §5/§7)."""

from __future__ import annotations

from .base import (
    REGISTRY_EGRESS_ALLOW,
    ExecResult,
    ProductionValidityError,
    SandboxError,
    SandboxInstance,
    SandboxService,
    SandboxSpec,
    SandboxUnavailableError,
)
from .base import (
    HostServiceRelayInstance as HostServiceRelayInstance,
)
from .config import (
    SandboxConfig,
    default_local_config,
    default_podman_config,
    default_sandbox_config,
)
from .gvisor import GvisorSandboxInstance, GvisorSandboxService
from .isolation import IsolationProfile, isolation_for
from .local import LocalSandboxInstance, LocalSandboxService
from .podman import PodmanSandboxInstance, PodmanSandboxService
from .probe import (
    DEFAULT_PROBE_TIMEOUT_S,
    probe_sandbox_reachability,
    sandbox_endpoint_label,
)
from .process import ProcessSandboxInstance, ProcessSandboxService
from .session import SandboxSession


def service_from_config(cfg: SandboxConfig) -> SandboxService:
    """[W-48] Map a SandboxConfig to its concrete backend service — the ONE shared
    backend↔config mapping used by BOTH the agent-server's live builder
    (`build_sandbox_service`) and the Settings connectivity preflight
    (`ConfigState.test_sandbox`). An EXPLICIT `process` → the dev backend (runs on
    host); an unknown backend FAILS CLOSED (raises) — never a silent host downgrade."""
    if cfg.backend == "gvisor":
        return GvisorSandboxService(cfg)
    if cfg.backend == "local":
        # DURABLE #3 FIX (codex-RCA'd, load-bearing): a local PODMAN socket must use the
        # libpod-native podman CLI path (PodmanSandboxService, `podman --url unix:// exec`),
        # NOT docker-py's /v1.44 docker-COMPAT API. Under concurrent build load the compat
        # `/containers/<id>/exec` endpoint returns spurious 404s for LIVE containers, which the
        # engine misreads as container death → needless sandbox recreate → builds churn/derail.
        # The CLI path removes that endpoint class entirely. Discriminate by the socket identity
        # (a podman socket); a real local DOCKER daemon (/var/run/docker.sock) stays on the
        # docker-py LocalSandboxService unchanged. Copy the config (no mutation): map it to a
        # podman config pointed at the SAME local socket.
        if "podman" in (cfg.docker_socket or "").lower():
            return PodmanSandboxService(
                cfg.model_copy(
                    update={"backend": "podman", "podman_url": cfg.docker_socket}
                )
            )
        return LocalSandboxService(cfg)
    if cfg.backend == "podman":
        return PodmanSandboxService(cfg)
    if cfg.backend == "process":
        # The unisolated dev backend (shares the host PID + net namespace).
        # Reaching it requires an EXPLICIT backend=="process"; the Build/soak path
        # additionally gates it behind the dev opt-out (preflight_build_sandbox_backend).
        return ProcessSandboxService(config=cfg)
    # W3 C-1: FAIL CLOSED. An unknown / garbage backend must NEVER silently fall
    # through to host execution — that turned a typo or a poisoned config write into
    # an un-sandboxed run. Refuse it here; the caller surfaces the error rather than
    # downgrading the isolation boundary.
    raise ValueError(
        f"unknown sandbox backend {cfg.backend!r} "
        "(expected one of: gvisor, local, podman, process)"
    )


# EPIC H (P0) — the dev-only opt-out that re-permits the unisolated `process` backend on
# a Build/soak path. FAIL-CLOSED is the default (Build/soak refuses `process`); a developer
# running plain local dev sets DISCO_ALLOW_PROCESS_SANDBOX_FOR_DEV=1 to opt back in. This
# REPLACES the old fail-OPEN DISCO_REQUIRE_PRODUCTION_SANDBOX opt-in, which left soak/Build
# silently running on `process` unless an operator REMEMBERED to set the protection var.
PROCESS_SANDBOX_DEV_OPT_OUT_ENV = "ALLOW_PROCESS_SANDBOX_FOR_DEV"  # disco_env prefixes DISCO_


def process_sandbox_dev_opt_out_enabled() -> bool:
    """True iff the dev-only opt-out env explicitly permits the unisolated `process`
    backend on a Build/soak path. Fail-closed: ONLY an explicit truthy value
    (DISCO_ALLOW_PROCESS_SANDBOX_FOR_DEV=1|true|yes|on) enables it; unset/anything else
    means NO. Read live (no caching) so a test/monkeypatch toggle is honored."""
    from disco.core.env import disco_env

    return disco_env(PROCESS_SANDBOX_DEV_OPT_OUT_ENV, "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def require_production_valid_backend(service: SandboxService) -> SandboxService:
    """EPIC H (§1.4/§9.3) production-validity GATE. Returns the service unchanged when
    its backend is production-valid (a container backend with its own PID + network
    namespace — gvisor/local/podman); raises ``ProductionValidityError`` with an
    actionable message when it is NOT (the `process` dev backend, which shares the host
    PID + net namespace and is the source of the isolation incidents — a build
    `kill <pid>` took down the agent-server).

    UNCONDITIONAL: this is the low-level gate that always refuses a non-valid backend.
    The Build/soak entrypoint goes through :func:`preflight_build_sandbox_backend`, which
    layers the dev-only opt-out on top. NOTE: the process backend's host-signal
    kill-refusal (`process_backend_signal_command_violation`) is a BEST-EFFORT dev-only
    nicety, NOT containment — this gate refusing the backend outright is the real
    containment, so the production path never relies on that refusal."""
    if not getattr(service, "is_production_valid", False):
        raise ProductionValidityError(
            f"the {service.name!r} backend is dev-only and is NOT valid for a production "
            "or Build/soak build — it shares the host PID + network namespace, so a build "
            "can take down the platform (e.g. `kill <pid>`). Select a container backend: "
            "Local, Podman, or gVisor. (For LOCAL DEV ONLY, set "
            "DISCO_ALLOW_PROCESS_SANDBOX_FOR_DEV=1 to re-permit the process backend.)"
        )
    return service


def preflight_build_sandbox_backend(
    service: SandboxService, *, allow_process_dev: bool | None = None
) -> SandboxService:
    """EPIC H (P0) FAIL-CLOSED Build/soak preflight — the entrypoint the live Build
    builder + the soak harness call BEFORE any build runs.

    Inverts the old fail-open posture: the `process` backend is refused BY DEFAULT for
    Build/soak (it shares the host PID + net namespace — the `kill <pid>` takedown
    source), and is re-permitted ONLY by the explicit dev-only opt-out
    DISCO_ALLOW_PROCESS_SANDBOX_FOR_DEV=1 (so plain local dev still works). A container
    backend (gvisor/local/podman) always passes. `allow_process_dev` defaults to the env
    read; pass it explicitly in tests."""
    if allow_process_dev is None:
        allow_process_dev = process_sandbox_dev_opt_out_enabled()
    if allow_process_dev and not getattr(service, "is_production_valid", False):
        return service  # explicit dev opt-out: the unisolated process backend is permitted
    return require_production_valid_backend(service)  # else fail-closed


__all__ = [
    "ExecResult",
    "ProductionValidityError",
    "GvisorSandboxInstance",
    "GvisorSandboxService",
    "IsolationProfile",
    "LocalSandboxInstance",
    "LocalSandboxService",
    "PodmanSandboxInstance",
    "PodmanSandboxService",
    "ProcessSandboxInstance",
    "ProcessSandboxService",
    "DEFAULT_PROBE_TIMEOUT_S",
    "probe_sandbox_reachability",
    "sandbox_endpoint_label",
    "REGISTRY_EGRESS_ALLOW",
    "SandboxConfig",
    "SandboxError",
    "SandboxInstance",
    "SandboxService",
    "SandboxSession",
    "SandboxSpec",
    "SandboxUnavailableError",
    "default_local_config",
    "default_podman_config",
    "default_sandbox_config",
    "isolation_for",
    "preflight_build_sandbox_backend",
    "process_sandbox_dev_opt_out_enabled",
    "require_production_valid_backend",
    "service_from_config",
]
