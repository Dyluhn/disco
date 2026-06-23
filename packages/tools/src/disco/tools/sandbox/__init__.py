"""Sandbox subsystem — spec/instance/service + backends (tool-sandbox §5/§7)."""

from __future__ import annotations

from .base import (
    REGISTRY_EGRESS_ALLOW,
    ExecResult,
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


__all__ = [
    "ExecResult",
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
    "service_from_config",
]
