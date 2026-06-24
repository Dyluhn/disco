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
from .process import ProcessSandboxInstance, ProcessSandboxService
from .session import SandboxSession


def service_from_config(cfg: SandboxConfig) -> SandboxService:
    """[W-48] Map a SandboxConfig to its concrete backend service — the ONE shared
    backend↔config mapping used by BOTH the agent-server's live builder
    (`build_sandbox_service`) and the Settings connectivity preflight
    (`ConfigState.test_sandbox`). `process`/unknown → the dev backend (runs on host)."""
    if cfg.backend == "gvisor":
        return GvisorSandboxService(cfg)
    if cfg.backend == "local":
        return LocalSandboxService(cfg)
    if cfg.backend == "podman":
        return PodmanSandboxService(cfg)
    return ProcessSandboxService()


def require_production_valid_backend(service: SandboxService) -> SandboxService:
    """EPIC H (§1.4/§9.3) production-validity GATE. Returns the service unchanged when
    its backend is production-valid (a container backend with its own PID + network
    namespace — gvisor/local/podman); raises ``ProductionValidityError`` with an
    actionable message when it is NOT (the `process` dev backend, which shares the host
    PID + net namespace and is the source of the isolation incidents — a build
    `kill <pid>` took down the agent-server).

    The Build-Soak / production build path calls this BEFORE any build runs so a
    misconfigured deployment fails loud at selection time rather than letting an
    unisolated build loose on the host."""
    if not getattr(service, "is_production_valid", False):
        raise ProductionValidityError(
            f"the {service.name!r} backend is dev-only and is NOT valid for a production "
            "or Build-Soak build — it shares the host PID + network namespace, so a build "
            "can take down the platform (e.g. `kill <pid>`). Select a container backend: "
            "Local, Podman, or gVisor."
        )
    return service


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
    "require_production_valid_backend",
    "service_from_config",
]
