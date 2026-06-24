"""Sandbox backend config — the host details the gVisor backend drives, NOT
constants in the code (tool-sandbox-contract §5.1 leaves these to the builder).

Initial values are the VM 201 host contract (`/opt/sandbox/CONTRACT.md`): the
local Docker socket, the `runsc` (gVisor) runtime, the `disco-sandbox:base` image,
and the `/opt/sandbox/workspaces` workspace root. Same pydantic-defaults shape as
the LLM `RouterConfig` / `default_config()`, so the settings layer that selects the
backend (gvisor / process / remote) reuses one config pattern, not a parallel one.
"""

from __future__ import annotations

import math

from pydantic import BaseModel, ConfigDict, ValidationInfo, field_validator


class SandboxConfig(BaseModel):
    """[config] How and where a sandbox backend runs. MUTABLE on purpose: the
    Settings layer is the live control surface (Dispo #25 hot-apply). The service
    holds a reference to this object (`self._cfg`) and re-reads fields on every
    op, so `cfg.image = "disco-sandbox:hot"` (or `cfg.reload_timeout_s = 0.05`) is
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
    image: str = "disco-sandbox:base"
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
    workspace_volume_prefix: str = "disco-ws"

    # default resource bounds applied on create. EPIC H (P1): these are the deployment
    # MAXIMUM, not a mere fallback. A SandboxSpec may TIGHTEN a bound (request LESS), but a
    # model-influenced spec can NEVER loosen one above the configured max nor disable a
    # limit — `resolve_bounds` (in _container.py) clamps above-max values down and rejects
    # negatives, while the per-field 0 "unset" sentinel resolves to the default below.
    default_cpu: float = 1.0
    default_memory_mb: int = 2048
    # EPIC H host-protection: default cgroup pids.max for a created sandbox container,
    # so a runaway build (fork bomb, parallel-install storm) can't exhaust host PIDs and
    # freeze the box. Used when a SandboxSpec leaves `pids` unset (0); also the hard
    # MAXIMUM a spec can request (above-max is clamped). Overridable per deployment via
    # the Settings layer (same hot-apply path as the other bounds).
    default_pids_limit: int = 512

    # EPIC H (P1) — resource caps for the filtered-egress PROXY SIDECAR. A "filtered" box
    # stands up a SECOND container (the allowlisting proxy). Before this it was capped on
    # MEMORY only (256m) and left UNBOUNDED on CPU + PIDs — so a wedged/compromised proxy
    # could burn host CPU or fork-bomb host PIDs with no ceiling. These apply the same
    # host-protection bounds to the sidecar; smaller than the sandbox's because the proxy
    # is a thin stdlib server, not a build. NOT spec-influenced (the model never shapes the
    # sidecar), so they are plain config maxima with no resolve_bounds clamp needed.
    sidecar_cpu: float = 1.0
    sidecar_memory_mb: int = 256
    sidecar_pids_limit: int = 128

    # how long to wait for the container to stop on close, before force-remove.
    stop_timeout_s: int = 5

    # EPIC H (P1 hardening) — the resource MAXIMA above are the host-protection ceiling,
    # so a 0 / negative / NaN / inf value is not a "looser" cap, it is a DISABLED one
    # (Docker reads `mem_limit`/`pids_limit`/`nano_cpus` of 0 as UNLIMITED, and a NaN/inf
    # maximum makes `resolve_bounds`' clamp a no-op). A mis-set config must therefore fail
    # LOUD at construction, never silently ship an unbounded sandbox or sidecar. Both the
    # sandbox bounds (default_*) AND the sidecar bounds (sidecar_*) are guarded — the
    # proxy sidecar is just as capable of burning host CPU / fork-bombing host PIDs.
    @field_validator(
        "default_cpu",
        "default_memory_mb",
        "default_pids_limit",
        "sidecar_cpu",
        "sidecar_memory_mb",
        "sidecar_pids_limit",
    )
    @classmethod
    def _finite_positive_bound(cls, v: float, info: ValidationInfo) -> float:
        # `math.isfinite` rejects NaN/inf (the int fields are already non-finite-proof via
        # pydantic's int coercion; the float fields — *_cpu — are not, so this is load-bearing).
        if not math.isfinite(v) or v <= 0:
            raise ValueError(
                f"{info.field_name} must be a finite positive number (got {v!r}); a "
                "0/negative/non-finite resource maximum would DISABLE the host-protection "
                "cap (unlimited CPU/memory/PIDs)"
            )
        return v

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
