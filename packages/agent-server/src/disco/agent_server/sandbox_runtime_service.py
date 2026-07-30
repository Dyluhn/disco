"""Sandbox-runtime helpers extracted from runtime.py.

Owns probe, service resolution, and egress-spec construction.

God-file decomposition (pure move, zero behavior change). The sandbox-backend
resolution + probing + spec building move out of runtime.py into a
`SandboxRuntimeService` collaborator constructed once in `ConversationRuntime`:

  - backend resolution:   _sandbox_service_now  (builds from ConfigStore or injected override)
  - reachability probe:   probe_active_sandbox  (live-reachability of the active backend)
  - config preflight:     probe_sandbox_config  (test-connection for unsaved config)
  - egress spec:          _build_sandbox_spec   (egress-posture spec for a Build sandbox)

The mutable state (`_injected_sandbox`, `_sandbox_spec`) and the config store
stay declared on `ConversationRuntime`; the service reaches them via the
back-reference `self._rt`. Every moved method keeps a thin delegator on
`ConversationRuntime` because internal callers (`_compose_build_loop`,
`_preflight_sandbox`, `sessions_service`, routes) reach them directly on the
runtime.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from disco.core.env import disco_env
from disco.core.llm import SandboxSettings
from disco.tools import (
    REGISTRY_EGRESS_ALLOW,
    Capability,
    SandboxService,
    SandboxSpec,
)
from disco.tools.sandbox import (
    SandboxConfig,
    preflight_build_sandbox_backend,
    service_from_config,
)

if TYPE_CHECKING:
    from .runtime import ConversationRuntime

logger = logging.getLogger(__name__)


def effective_local_runtime(backend: str, runtime: str) -> str:
    """The OCI runtime the `local` (Docker/Podman socket) backend ACTUALLY runs under.

    DISCO_LOCAL_RUNTIME is the deployment knob for it — set it to `runsc` on a gVisor
    host so config-driven local sandboxes actually run under gVisor. It was previously
    honored ONLY on the DISCO_SANDBOX override path (__main__), so with the compose
    default (DISCO_SANDBOX unset -> config-driven) it was SILENTLY IGNORED and local
    sandboxes ran under plain runc despite the operator asking for runsc (found live
    2026-07-09). Scoped to `local` (the runtime isn't user-selectable in Settings for it,
    so this can't clash with a UI choice); gVisor/podman carry their own runtimes.

    Shared by the run path (`build_sandbox_service`) AND the Settings 'Test connection'
    probe (`probe_sandbox_config`) so they build the SAME runtime — otherwise Test
    connection validates `runc` (the DTO default) and false-greens on a runsc-only host
    while the real run path (runsc) fails its `_require_runtime` healthcheck."""
    if backend == "local":
        return disco_env("LOCAL_RUNTIME", runtime) or runtime
    return runtime


def effective_sandbox_config(settings: SandboxSettings) -> SandboxConfig:
    """Resolve the deployment's local daemon without losing its engine identity.

    Compose mounts both Docker and rootless Podman at the conventional in-container
    Docker-compatible path. The path therefore cannot identify the daemon, while
    Podman's compatibility API cannot reliably keep detached execs alive. The
    self-host deployment declares its local engine explicitly so a saved ``local``
    selection can use the native Podman transport. Non-local Settings selections
    remain authoritative.
    """

    engine = (disco_env("LOCAL_ENGINE", "") or "").strip().lower()
    if engine not in ("", "docker", "podman"):
        raise ValueError(f"DISCO_LOCAL_ENGINE={engine!r} is invalid; expected docker or podman")
    backend = settings.backend
    podman_url = settings.podman_url
    if backend == "local" and engine == "podman":
        backend = "podman"
        podman_url = settings.docker_socket
    return SandboxConfig(
        backend=backend,
        docker_socket=settings.docker_socket,
        podman_url=podman_url,
        runtime=effective_local_runtime(settings.backend, settings.runtime),
        image=settings.image,
        workspace_root=settings.workspace_root,
        preview_host=disco_env("PREVIEW_HOST", ""),
    )


def build_sandbox_service(settings: SandboxSettings) -> SandboxService:
    """Map persisted settings and deployment identity to the concrete backend."""
    cfg = effective_sandbox_config(settings)
    service = service_from_config(cfg)
    return preflight_build_sandbox_backend(service)


class SandboxRuntimeService:
    """Sandbox-resolution and probing for the conversation runtime.

    Reaches mutable runtime state + config via `self._rt` — the owning
    `ConversationRuntime`. See the module docstring for the full list of
    reached attributes.
    """

    def __init__(self, runtime: ConversationRuntime) -> None:
        self._rt = runtime

    def _sandbox_service_now(self) -> SandboxService:
        """The active sandbox backend: the injected override if present, else built from
        the persisted SandboxSettings (reloaded each time — the Settings selector drives it)."""
        if self._rt._injected_sandbox is not None:
            return self._rt._injected_sandbox
        return self._rt._build_sandbox_service_from_config(self._rt._config_store.load().sandbox)

    def effective_backend_name(self) -> str:
        """Return the concrete backend identity selected by Settings + deployment.

        The persisted ``local`` position can intentionally resolve to the native
        ``podman`` transport when ``DISCO_LOCAL_ENGINE=podman``.  Lifecycle
        reconciliation must compare cached sessions with that effective identity,
        not the raw Settings label, or every kick falsely treats a healthy Podman
        session as stale and discards its workspace.
        """

        settings = self._rt._config_store.load().sandbox
        cfg = effective_sandbox_config(settings)
        # ProcessSandboxService allocates a dev workspace root in its constructor;
        # its configured and concrete identities are already identical, so avoid
        # constructing it for this read-only comparison. Container service
        # construction is side-effect free and reuses the canonical mapper,
        # including its local-Podman-socket alias.
        if cfg.backend == "process":
            return "process"
        return service_from_config(cfg).name

    def backend_name(self) -> str | None:
        """Return the active backend name without making a read path fatal."""
        try:
            return self._sandbox_service_now().name
        except Exception:
            return None

    async def probe_active_sandbox(self) -> tuple[bool, str, str]:
        """Reachability of the ACTIVE (persisted) sandbox backend — probed HERE, on the
        agent-server, because this is the process that actually runs sandboxes (it owns
        the container socket; the app-server does not). Uses the SAME service the run
        path builds (`_sandbox_service_now`), so the banner and a real run can never
        disagree. Returns (reachable, backend, detail). Never raises."""
        from disco.tools.sandbox import probe_sandbox_reachability, sandbox_endpoint_label

        settings = self._rt._config_store.load().sandbox
        try:
            service = self._rt._sandbox_service_now()
        except Exception as exc:
            endpoint = sandbox_endpoint_label(
                settings.backend, settings.docker_socket, settings.podman_url
            )
            return False, settings.backend, f"{endpoint}: {exc}"
        backend = getattr(service, "name", settings.backend) or settings.backend
        endpoint = sandbox_endpoint_label(backend, settings.docker_socket, settings.podman_url)
        ok, _status, detail = await probe_sandbox_reachability(service, endpoint)
        return ok, backend, ("" if ok else detail)

    async def probe_sandbox_config(self, settings: SandboxSettings) -> tuple[bool, str, str]:
        """Probe a GIVEN sandbox config (the Settings 'Test connection' preflight, before
        it is saved) — same environment + classifier as the active-backend probe.
        Returns (ok, status, detail). Never raises."""
        from disco.tools.sandbox import (
            probe_sandbox_reachability,
            sandbox_endpoint_label,
        )
        from disco.tools.sandbox import (
            service_from_config as service_from_config_for_probe,
        )

        try:
            cfg = effective_sandbox_config(settings)
            endpoint = sandbox_endpoint_label(cfg.backend, cfg.docker_socket, cfg.podman_url)
            service = service_from_config_for_probe(cfg)
        except Exception as exc:
            endpoint = sandbox_endpoint_label(
                settings.backend, settings.docker_socket, settings.podman_url
            )
            return False, "error", f"{endpoint}: {exc}"
        return await probe_sandbox_reachability(service, endpoint)

    def _build_sandbox_spec(
        self,
        *,
        surface: str = "build",
        mcp_egress_hosts: frozenset[str] | None = None,
    ) -> SandboxSpec:
        """The egress-posture spec for a Build sandbox. FILTERED by default
        (BP-G10: build boxes get the allowlisting proxy now that E8 wired the
        proxy on every backend — gVisor, podman, local). The legacy
        PMX_BUILD_EGRESS=open value widens to arbitrary public web through the
        same private-denying boundary; it never selects a raw bridge. Used both by
        _compose_build_loop and upload_session so pending sessions and build
        sessions share the same spec.

        When mcp_egress_hosts is provided, they are UNIONed into the egress_allow set
        (SUPERSET, not replacement) — the pre-existing registry hosts AND the MCP
        hosts both survive (rung B egress-proxy routing)."""
        if surface == "agent":
            egress = disco_env("AGENT_EGRESS", "public").lower().strip()
        else:
            egress = disco_env("BUILD_EGRESS", "filtered").lower().strip()
        if egress == "filtered":
            base_allow = REGISTRY_EGRESS_ALLOW
            if mcp_egress_hosts:
                base_allow = frozenset(base_allow | mcp_egress_hosts)
            return self._rt._sandbox_spec.model_copy(
                update={
                    "egress_allow": base_allow,
                    "public_web": False,
                    "permitted": self._rt._sandbox_spec.permitted - {Capability.NETWORK},
                }
            )
        if egress == "public":
            return self._rt._sandbox_spec.model_copy(
                update={
                    "egress_allow": frozenset(),
                    "public_web": True,
                    "permitted": self._rt._sandbox_spec.permitted - {Capability.NETWORK},
                }
            )
        if egress in {"sealed", "none"}:
            return self._rt._sandbox_spec.model_copy(
                update={
                    "egress_allow": frozenset(),
                    "public_web": False,
                    "permitted": self._rt._sandbox_spec.permitted - {Capability.NETWORK},
                }
            )
        if egress not in {"open", "raw"}:
            raise ValueError(
                f"invalid {surface.upper()}_EGRESS={egress!r}; expected one of "
                "filtered, public, sealed, open, or raw"
            )
        spec_update = {
            "permitted": self._rt._sandbox_spec.permitted - {Capability.NETWORK},
            "public_web": True,
            "egress_allow": frozenset(),
        }
        return self._rt._sandbox_spec.model_copy(update=spec_update)
