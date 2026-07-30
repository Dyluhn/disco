"""Compatibility facade for the canonical Preview resource authority."""

from __future__ import annotations

from typing import Any

from .preview_models import (
    NoPreviewPortAvailableError,
    PreviewCommandError,
    PreviewReloadStrategy,
    PreviewSession,
    PreviewStatus,
    _PreviewPortPool,
    preview_projection_digest,
    preview_requires_node_dependencies,
)
from .preview_port_allocator import _default_port_pool
from .preview_resource import PreviewResource


class _PreviewManagerLifecycleFacade:
    _resource: PreviewResource

    async def start(
        self,
        *,
        serve_dir: str | None = None,
        command: str | None = None,
        framework: str | None = None,
        cwd: str | None = None,
        name: str | None = None,
        supervise: bool = True,
    ) -> PreviewSession:
        return await self._resource.start(
            serve_dir=serve_dir,
            command=command,
            framework=framework,
            cwd=cwd,
            name=name,
            supervise=supervise,
        )

    async def status(self, name: str | None = None) -> list[PreviewSession]:
        return await self._resource.status(name)

    async def restart_canonical(self) -> PreviewSession | None:
        return await self._resource.restart_canonical()

    async def restore_sealed(self, contract: Any) -> PreviewSession | None:
        return await self._resource.restore_sealed(contract)

    async def resolve_sealed_contract(self, contract: Any) -> PreviewSession | None:
        return await self._resource.resolve_sealed_contract(contract)

    async def resolve_active_projection(self, projection: Any) -> PreviewSession | None:
        return await self._resource.resolve_active_projection(projection)


class _PreviewManagerReadFacade:
    _resource: PreviewResource

    def canonical_port(self) -> int | None:
        return self._resource.canonical_port()

    def canonical_session(self) -> PreviewSession | None:
        return self._resource.canonical_session()

    def canonical_url(self) -> str | None:
        return self._resource.canonical_url()

    def owns_sandbox(self) -> bool:
        return self._resource.owns_sandbox()

    def canonical_lifecycle_session(self) -> PreviewSession | None:
        return self._resource.canonical_lifecycle_session()

    async def logs(self, name: str | None = None, *, tail_chars: int = 4000) -> dict[str, str]:
        return await self._resource.logs(name, tail_chars=tail_chars)

    def list(self) -> list[PreviewSession]:
        return self._resource.list()


class _PreviewManagerStopFacade:
    _resource: PreviewResource

    async def stop(self, name: str | None = None) -> list[str]:
        return await self._resource.stop(name)

    async def aclose(self) -> None:
        await self._resource.aclose()


class PreviewManager(
    _PreviewManagerLifecycleFacade,
    _PreviewManagerReadFacade,
    _PreviewManagerStopFacade,
):
    """State-free adapter preserving the established manager API."""

    MAX_RESTARTS = PreviewResource.MAX_RESTARTS

    def __init__(
        self,
        sandbox: Any,
        *,
        port_pool: list[int] | None = None,
        health_attempts: int = 10,
        health_interval_s: float = 0.3,
        supervise_interval_s: float = 4.0,
        owns_sandbox: bool = False,
    ) -> None:
        self._resource = PreviewResource(
            sandbox,
            port_pool=(
                _PreviewPortPool(tuple(port_pool))
                if port_pool is not None
                else None
            ),
            health_attempts=health_attempts,
            health_interval_s=health_interval_s,
            supervise_interval_s=supervise_interval_s,
            owns_sandbox=owns_sandbox,
        )

    @property
    def _sandbox(self) -> Any:
        return self._resource._sandbox

    @property
    def _sessions(self) -> dict[str, PreviewSession]:
        return self._resource._sessions

    @property
    def _closed(self) -> bool:
        return self._resource._closed

    async def _allocate_port(self, *, reclaim_name: str | None = None) -> int:
        return await self._resource._allocate_port(reclaim_name=reclaim_name)

    async def _launch(self, session: PreviewSession) -> None:
        await self._resource._launch(session)

    def _owner_is_this_session(self, owner_session: str, name: str) -> bool:
        return self._resource._owner_is_this_session(owner_session, name)

    async def _probe_health(self, port: int, *, require_success: bool = False) -> bool:
        return await self._resource._probe_health(port, require_success=require_success)

    async def _session_alive(self, name: str) -> bool:
        return await self._resource._session_alive(name)

    async def _supervise_once(self) -> None:
        await self._resource._supervise_once()


__all__ = [
    "NoPreviewPortAvailableError",
    "PreviewCommandError",
    "PreviewManager",
    "PreviewReloadStrategy",
    "PreviewSession",
    "PreviewStatus",
    "_default_port_pool",
    "preview_projection_digest",
    "preview_requires_node_dependencies",
]
