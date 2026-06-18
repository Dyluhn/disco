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


class SandboxUnavailableError(SandboxError):
    """The backend infrastructure itself is unusable — Docker unreachable, the
    `runsc` runtime missing, the base image absent, the container failed to start.
    Carries the real underlying cause (reactive-error rule); distinct from a
    per-command failure so the caller can tell "the box is broken" from "the
    command failed"."""


class ExecResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    exit_code: int
    stdout: str
    stderr: str
    # [v1.1 adjustment, flagged] A command killed for exceeding its timeout is
    # reported with this flag set (not raised), so partial stdout/stderr + the exit
    # code survive and the caller can distinguish a timeout from a normal non-zero
    # exit. Additive + defaulted, so existing callers are unaffected.
    timed_out: bool = False


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
        allow-listed. Enforced outside the guest by the backend (§7 rule 3).

        Matching: an exact host (`api.example.com`) OR a leading-dot suffix entry
        (`.example.com`) which matches the apex and any subdomain. This is the
        predicate an egress-interception proxy consults per connection (Cluster 3).
        """
        h = host.lower().strip()
        for entry in self.egress_allow:
            e = entry.lower().strip()
            if e.startswith("."):
                # `.example.com` → matches `example.com` and `*.example.com`
                if h == e[1:] or h.endswith(e):
                    return True
            elif h == e:
                return True
        return False


REGISTRY_EGRESS_ALLOW: frozenset[str] = frozenset({
    "registry.npmjs.org",
    ".npmjs.org",
    "pypi.org",
    "files.pythonhosted.org",
    "github.com",
    "codeload.github.com",
    ".githubusercontent.com",
    "deb.debian.org",
    "security.debian.org",
    "cdn.jsdelivr.net",
    "unpkg.com",
    "esm.sh",
    "fonts.googleapis.com",
    "fonts.gstatic.com",
})


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
    # [B4] Existence check resolved in the BACKEND's own namespace — the only
    # honest answer for a container backend whose `workspace_path` is None (the
    # host has no view of the box FS). C18's done-condition `file_exists`
    # predicate calls this instead of a host `Path.exists()`. Returns False on a
    # missing file or a path escaping the workspace jail; never raises on absence.
    async def file_exists(self, path: str) -> bool: ...
    def display_url(self) -> str | None: ...  # live noVNC view, if supported
    # [ADDITIVE — flagged] Expose an inside-the-box dev-server port to a URL the user's
    # browser can reach, by the method appropriate to THIS backend's transport (the
    # backend owns "how to reach a port inside me"). Returns None if not supported /
    # not exposed. Like `timed_out` / `SandboxUnavailableError`, this is additive — it
    # does not change existing behavior; backends that don't implement it return None.
    def expose_port(self, port: int) -> str | None: ...
    async def destroy(self) -> None: ...
    # [W5 — workspace_path] Workspace root as an absolute host-FS path (process
    # backend) or None (container backends where the host has no direct view of
    # the box FS). C18 and C1c consume this to resolve done-condition predicates.
    @property
    def workspace_path(self) -> str | None: ...


@runtime_checkable
class SandboxService(Protocol):
    """[CONTRACT] Creates/destroys instances from specs. Pluggable backend."""

    name: str  # "process" | "e2b" | "gvisor" | "remote"

    async def create(
        self, spec: SandboxSpec, *, owner_id: str, conversation_id: str
    ) -> SandboxInstance: ...

    async def get(self, instance_id: str) -> SandboxInstance | None: ...

    async def list_live_instances(self) -> list[str]:
        """Return the conversation_ids of all live sandbox containers on this backend.
        Container backends return conversation_ids of running pmx-sbx-* containers;
        process backend returns [] (no containers to inspect after restart)."""
        return []

    async def destroy_by_conversation(self, conversation_id: str) -> None:
        """Destroy all containers (sandbox + egress sidecar) whose
        pmx.conversation_id label matches. No-op on process backend."""
