"""Sandbox spec / instance / service — tool-sandbox-contract.md §5, §7.

Spec-vs-instance split [OH]: a `SandboxSpec` (template: image, limits, permitted
capabilities, egress allowlist) is distinct from a `SandboxInstance` (a running,
lifecycle-managed environment). Tools execute against an instance. Backends are
pluggable behind `SandboxService` (process/e2b/gvisor/remote) — the executor and
tools depend only on these protocols (§5.1, the "door open" seam).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from ..anatomy import Capability


class SandboxError(Exception):
    """Raised by an instance on a backend/lifecycle failure (e.g. use after
    destroy). The executor maps it to a `sandbox_error` ToolResult."""


class ExecResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    exit_code: int
    stdout: str
    stderr: str


class SandboxSpec(BaseModel):
    """[CONTRACT] The template an instance is created from. Pure config."""

    model_config = ConfigDict(frozen=True)
    image: str = "python-node-base"  # base image id [VERIFY contents]
    # capabilities the instance is permitted — the union of the scope's tools'
    # `needs` (§2). Anything not listed is denied.
    permitted: frozenset[Capability] = frozenset()
    cpu: float = 1.0
    memory_mb: int = 2048
    disk_mb: int = 4096
    timeout_s: int = 300  # default per-call ceiling
    # egress allowlist (§7); empty => deny ALL network (deny-by-default).
    egress_allow: frozenset[str] = frozenset()

    def egress_allowed(self, host: str) -> bool:
        """[CONTRACT §7] Deny-by-default: a host is reachable only if explicitly
        allow-listed. Enforced outside the guest by the backend (§7 rule 3)."""
        return host in self.egress_allow


@runtime_checkable
class SandboxInstance(Protocol):
    """[CONTRACT] A running, isolated environment. Tools execute against it.
    Lifecycle is owned by the agent-server for its conversation (BoD §5.2)."""

    id: str
    owner_id: str
    conversation_id: str
    spec: SandboxSpec

    async def exec_shell(self, cmd: str, *, timeout_s: int) -> ExecResult: ...
    async def read_file(self, path: str) -> bytes: ...
    async def write_file(self, path: str, data: bytes) -> None: ...
    async def list_dir(self, path: str) -> list[str]: ...
    def display_url(self) -> str | None: ...  # live noVNC view, if supported
    async def destroy(self) -> None: ...


@runtime_checkable
class SandboxService(Protocol):
    """[CONTRACT] Creates/destroys instances from specs. Pluggable backend."""

    name: str  # "process" | "e2b" | "gvisor" | "remote"

    async def create(
        self, spec: SandboxSpec, *, owner_id: str, conversation_id: str
    ) -> SandboxInstance: ...

    async def get(self, instance_id: str) -> SandboxInstance | None: ...
