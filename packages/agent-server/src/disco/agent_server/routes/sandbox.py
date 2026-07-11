"""Sandbox reachability routes — served by the AGENT-server on purpose.

The agent-server is the process that actually runs sandboxes (it owns the
container socket / SSH transport); the app-server does not. So the health banner
and the Settings "Test connection" probe must run HERE, against the same service
the run path builds — otherwise a split-container deploy shows a false "sandbox
unreachable" banner while builds run fine (found live 2026-07-09). The persisted
sandbox CONFIG (GET/PUT) stays on the app-server; only the live PROBE lives here.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from ..runtime import ConversationRuntime


class SandboxProbeBody(BaseModel):
    """A sandbox config to probe before saving (mirror of the app-server's
    SandboxConfigDTO wire shape). `connections` is ignored here — only the active
    connection fields are probed."""

    backend: Literal["gvisor", "local", "podman", "process"]
    docker_socket: str = ""
    podman_url: str = ""
    runtime: str = ""
    image: str = ""
    workspace_root: str = ""


def make_sandbox_router(runtime: ConversationRuntime | None) -> APIRouter:
    router = APIRouter()

    @router.get("/api/sandbox/health")
    async def sandbox_health() -> dict:
        """Reachability of the ACTIVE (persisted) sandbox backend — the signal the app
        shell polls to warn BEFORE a run. Probed on the agent-server so it reflects the
        real run environment. `{reachable, backend, detail}` (SandboxHealthDTO shape)."""
        if runtime is None:
            return {"reachable": False, "backend": "unknown", "detail": "runtime unavailable"}
        reachable, backend, detail = await runtime.probe_active_sandbox()
        return {"reachable": reachable, "backend": backend, "detail": detail}

    @router.post("/api/sandbox/test")
    async def sandbox_test(body: SandboxProbeBody) -> dict:
        """Connectivity preflight for a GIVEN backend config (the Settings 'Test
        connection' button). Real, bounded probe → a typed host-naming verdict at HTTP
        200 (never a 500). `{ok, status, detail, provider}` (ProbeResult shape)."""
        from disco.core.llm import SandboxSettings

        if runtime is None:
            return {
                "ok": False,
                "status": "error",
                "detail": "runtime unavailable",
                "provider": body.backend,
            }
        settings = SandboxSettings(
            backend=body.backend,
            docker_socket=body.docker_socket or "unix:///var/run/docker.sock",
            podman_url=body.podman_url or "",
            runtime=body.runtime or "runc",
            image=body.image or "disco-sandbox:base",
            workspace_root=body.workspace_root or "/opt/sandbox/workspaces",
        )
        ok, status, detail = await runtime.probe_sandbox_config(settings)
        return {"ok": ok, "status": status, "detail": detail, "provider": body.backend}

    return router
