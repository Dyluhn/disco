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
    backend: str = "gvisor"  # "process" | "gvisor" | "podman" | "local"

    # --- gVisor / Docker host (VM 201 contract) ---
    docker_socket: str = "unix:///var/run/docker.sock"  # local socket, no TCP/TLS
    runtime: str = "runsc"  # gVisor (runsc) / Podman (crun, server-side); a value, not a branch
    image: str = "pmx-sandbox:base"
    workspace_root: str = "/opt/sandbox/workspaces"  # host dir bind-mounted to /workspace (gVisor)
    container_workspace: str = "/workspace"
    # the host a published preview port is reachable at. Empty → derived (localhost for a
    # local socket; the remote host's IP for Docker-over-SSH). Set a LAN/tailnet IP to make
    # previews reachable from other devices, not just the agent-server's own host.
    preview_host: str = ""
    # The image's run-user uid (contract: `agent` = 1000). Files written via the
    # interface are owned by it so the sandbox user can edit them, not just read them.
    workspace_uid: int = 1000

    # --- Podman native remote (VM 202 contract) ---
    # The non-standard ROOTLESS socket path is exactly why we use Podman's native
    # remote (not docker-py's ssh://, which assumes the default socket). The SSH leg
    # uses the system ssh client, so tailnet keyless auth applies. Driving Podman
    # through this socket means limits are enforced by the user@ systemd manager — a
    # bare-SSH `podman run` would silently fall back to cgroupfs (limits don't apply).
    podman_url: str = "http+ssh://sandbox@100.73.110.47/run/user/1000/podman/podman.sock"
    # Podman has no auto-created bind source + rootless can't write under root-owned
    # paths, so the workspace is a per-run NAMED VOLUME (auto-created, socket-mediated,
    # persists across the container). This prefix names it.
    workspace_volume_prefix: str = "pmx-ws"

    # default resource bounds applied on create (a SandboxSpec may tighten them).
    default_cpu: float = 1.0
    default_memory_mb: int = 2048

    # how long to wait for the container to stop on close, before force-remove.
    stop_timeout_s: int = 5


def default_sandbox_config() -> SandboxConfig:
    """The starting config, wired to the VM 201 host contract (gVisor/Docker)."""
    return SandboxConfig()


def default_podman_config() -> SandboxConfig:
    """The Podman-remote position, wired to the VM 202 contract (crun, rootless)."""
    return SandboxConfig(backend="podman", runtime="crun")


def default_local_config() -> SandboxConfig:
    """The local container position: the Docker/OCI backend on the LOCAL socket
    (`docker_socket` default) with the standard `runc` runtime — no SSH, no remote.
    The lowest-isolation tier (shared host kernel); see `isolation.py`. Workspace is a
    per-run named volume (`workspace_volume_prefix`), portable across local Docker and
    rootless Podman. The cross-platform target (Windows validation deferred)."""
    return SandboxConfig(backend="local", runtime="runc")
