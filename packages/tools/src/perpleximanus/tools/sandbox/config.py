"""Sandbox backend config — the host details the gVisor backend drives, NOT
constants in the code (tool-sandbox-contract §5.1 leaves these to the builder).

Initial values are the VM 201 host contract (`/opt/sandbox/CONTRACT.md`): the
local Docker socket, the `runsc` (gVisor) runtime, the `pmx-sandbox:base` image,
and the `/opt/sandbox/workspaces` workspace root. Same pydantic-defaults shape as
the LLM `RouterConfig` / `default_config()`, so the settings layer that selects the
backend (gvisor / process / remote) reuses one config pattern, not a parallel one.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class SandboxConfig(BaseModel):
    """[config] How and where a sandbox backend runs. Frozen; passed to the
    backend at construction so nothing host-specific is hardcoded."""

    model_config = ConfigDict(frozen=True)

    # which backend the settings layer selected (positions on one interface).
    backend: str = "gvisor"  # "process" | "gvisor" | "remote"

    # --- gVisor / Docker host (VM 201 contract) ---
    docker_socket: str = "unix:///var/run/docker.sock"  # local socket, no TCP/TLS
    runtime: str = "runsc"  # gVisor; host default stays runc
    image: str = "pmx-sandbox:base"
    workspace_root: str = "/opt/sandbox/workspaces"  # host dir bind-mounted to /workspace
    container_workspace: str = "/workspace"

    # default resource bounds applied on create (a SandboxSpec may tighten them).
    default_cpu: float = 1.0
    default_memory_mb: int = 2048

    # how long to wait for the container to stop on close, before force-remove.
    stop_timeout_s: int = 5


def default_sandbox_config() -> SandboxConfig:
    """The starting config, wired to the VM 201 host contract."""
    return SandboxConfig()
