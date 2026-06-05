"""Sandbox subsystem — spec/instance/service + backends (tool-sandbox §5/§7)."""

from __future__ import annotations

from .base import (
    ExecResult,
    SandboxError,
    SandboxInstance,
    SandboxService,
    SandboxSpec,
    SandboxUnavailableError,
)
from .config import SandboxConfig, default_podman_config, default_sandbox_config
from .gvisor import GvisorSandboxInstance, GvisorSandboxService
from .podman import PodmanSandboxInstance, PodmanSandboxService
from .process import ProcessSandboxInstance, ProcessSandboxService

__all__ = [
    "ExecResult",
    "GvisorSandboxInstance",
    "GvisorSandboxService",
    "PodmanSandboxInstance",
    "PodmanSandboxService",
    "ProcessSandboxInstance",
    "ProcessSandboxService",
    "SandboxConfig",
    "SandboxError",
    "SandboxInstance",
    "SandboxService",
    "SandboxSpec",
    "SandboxUnavailableError",
    "default_podman_config",
    "default_sandbox_config",
]
