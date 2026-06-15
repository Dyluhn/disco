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
    """[config] How and where a sandbox backend runs. MUTABLE on purpose: the
    Settings layer is the live control surface (Dispo #25 hot-apply). The service
    holds a reference to this object (`self._cfg`) and re-reads fields on every
    op, so `cfg.image = "pmx-sandbox:hot"` (or `cfg.reload_timeout_s = 0.05`) is
    the whole hot-update API — no service restart, no container recreate. If you
    want a true frozen snapshot, copy it (`SandboxConfig(**cfg.model_dump())`)."""

    # No `frozen=True` here. Frozen would re-introduce the Dispo #25 bug: a
    # settings change would require recreating the service (and any containers
    # built from a captured snapshot) instead of just being picked up by the
    # next op. Mutable-by-construction is the whole point.
    model_config = ConfigDict()

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

    # Wedge-guard timeout for the docker/podman `reload()` client call (Dispo #25).
    # A hung or failing client must NEVER block the event loop — this is the
    # BOUND on `_safe_reload()`'s wait, after which a typed SandboxUnavailableError
    # is raised (the caller catches, the loop continues). 0.5s is a healthy
    # reload is sub-ms, so this is ~500x the real latency; tight enough that a
    # stuck docker daemon can't wedge a session. Hot-applied: the next instance
    # created by the service picks up any change to this field.
    reload_timeout_s: float = 0.5

    # Socket/HTTP timeout (s) handed to the docker-py client at construction
    # (Dispo #25, completing the wedge guard). The create path — `ping()`,
    # `info()`, `containers.run()` — runs inside `asyncio.to_thread`, so it never
    # blocks the loop directly; BUT `to_thread` cannot cancel its worker, and
    # docker-py defaults to a 60s socket timeout, so a hung daemon would leak a
    # pool thread for a full minute per call (enough leaks exhaust the executor).
    # Bounding the client itself makes those calls fail fast, the worker return,
    # and the typed SandboxUnavailableError surface. `reload()` keeps its tighter
    # async 0.5s guard above; this covers everything else. Hot-applied: a new
    # client (next service instance) picks up a change to this field.
    client_timeout_s: int = 30


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
