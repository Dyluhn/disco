"""Sandbox subsystem — spec/instance/service + backends (tool-sandbox §5/§7)."""

from __future__ import annotations

from .base import (
    ExecResult,
    SandboxError,
    SandboxInstance,
    SandboxService,
    SandboxSpec,
)
from .process import ProcessSandboxInstance, ProcessSandboxService

__all__ = [
    "ExecResult",
    "ProcessSandboxInstance",
    "ProcessSandboxService",
    "SandboxError",
    "SandboxInstance",
    "SandboxService",
    "SandboxSpec",
]
