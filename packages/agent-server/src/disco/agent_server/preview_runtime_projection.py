"""Passive Preview runtime discovery, status, and presentation."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from disco.core import DEFAULT_OWNER_ID
from disco.core.loop.preview_target import is_managed_host_preview_port
from disco.tools.sandbox._container import NOVNC_PORT, PREVIEW_PORT, USER_PORTS
from disco.tools.sandbox.port_owner import PortOwner

from .preview_projection import SealedPreviewRuntimeContract
from .preview_status import (
    empty_preview_metadata,
    managed_preview_metadata,
    managed_unavailable_reason,
)
from .runtime_settings import _BUILD_LIKE_SURFACES

if TYPE_CHECKING:
    from disco.core.llm import ConfigStore
    from disco.core.store.sqlite import SqliteEventStore

    from .connection_tracker import ConnectionTracker
    from .live_session_directory import LiveSessionDirectory
    from .runtime_settings import RuntimeSettings

PortOwners = Callable[[Any, list[int]], Awaitable[dict[int, PortOwner | None]]]


def _owns_isolated_sandbox(manager: Any) -> bool:
    owns_sandbox = getattr(manager, "owns_sandbox", None)
    return owns_sandbox() is True if callable(owns_sandbox) else False


class PreviewRuntimeProjection:
    """Projection-only Preview collaborator with explicit dependencies."""

    def __init__(
        self,
        live_sessions: LiveSessionDirectory,
        store: SqliteEventStore,
        config_store: ConfigStore,
        settings: RuntimeSettings,
        connections: ConnectionTracker,
    ) -> None:
        self._live_sessions = live_sessions
        self._store = store
        self._config_store = config_store
        self._settings = settings
        self._connections = connections

    def live_browser_enabled(self) -> bool:
        try:
            return bool(self._config_store.load().live_browser.enabled)
        except Exception:  # noqa: BLE001 — config unavailable ⇒ feature OFF
            return False

    def preview_target_port(self, conversation_id: str) -> int | None:
        session = self._live_sessions.live_session(conversation_id)
        manager = getattr(session, "_preview_manager", None) if session is not None else None
        if manager is None:
            if self._settings._surface_of(conversation_id) in _BUILD_LIKE_SURFACES or getattr(
                session, "_auto_preview_disabled", False
            ):
                return None
            return PREVIEW_PORT
        try:
            port = manager.canonical_port()
        except Exception:  # noqa: BLE001 — corrupt registry cannot select an upstream
            return None
        if port is None:
            return None
        managed_host_port = is_managed_host_preview_port(port)
        if (
            not isinstance(port, int)
            or isinstance(port, bool)
            or (port not in USER_PORTS and not managed_host_port)
            or (managed_host_port and not getattr(session, "shares_host_network", False))
            or port == NOVNC_PORT
        ):
            return None
        return port

    async def resolve_active_preview_projection(
        self,
        conversation_id: str,
        projection: Any,
    ) -> bool:
        session = self._live_sessions.live_session(conversation_id)
        manager = getattr(session, "_preview_manager", None) if session is not None else None
        if manager is None:
            return False
        try:
            return await manager.resolve_active_projection(projection) is not None
        except Exception:  # noqa: BLE001 — a stale/malformed generation fails closed
            return False

    async def resolve_finished_preview_runtime(
        self,
        conversation_id: str,
        contract: SealedPreviewRuntimeContract,
    ) -> dict[str, Any] | None:
        session = self._live_sessions.live_session(conversation_id)
        manager = getattr(session, "_preview_manager", None) if session is not None else None
        if manager is None:
            return None
        try:
            resolved = await manager.resolve_active_projection(contract.projection)
            if resolved is None:
                resolved = await manager.resolve_sealed_contract(contract)
            data = resolved.to_dict() if resolved is not None else None
        except Exception:  # noqa: BLE001 - stale or malformed authority fails closed
            return None
        if not isinstance(data, dict) or data.get("status") not in {"running", "unavailable"}:
            return None
        return data

    def port_upstream(self, conversation_id: str, port: int) -> str | None:
        session = self._live_sessions.live_session(conversation_id)
        if session is None:
            return None
        manager = getattr(session, "_preview_manager", None)
        if manager is not None and _owns_isolated_sandbox(manager):
            if port == manager.canonical_port():
                return manager.canonical_url()
            if port != NOVNC_PORT:
                return None
        if port not in USER_PORTS and (
            not is_managed_host_preview_port(port)
            or not getattr(session, "shares_host_network", False)
            or port != self.preview_target_port(conversation_id)
        ):
            return None
        if port == NOVNC_PORT and not (
            self.live_browser_enabled() and getattr(session, "supports_live_view", False)
        ):
            return None
        return session.expose_port(port)

    @staticmethod
    def _isolated_preview_payload(
        manager: Any,
        metadata: dict[str, Any],
        managed_detail: str,
        ports: list[dict[str, Any]],
    ) -> dict[str, Any]:
        url = manager.canonical_url()
        if metadata["status"] == "running" and url is not None:
            return {
                "available": True,
                "proxy": True,
                "owner": None,
                "ports": ports,
                **metadata,
            }
        return {
            "available": False,
            "reason": managed_unavailable_reason(metadata["status"], managed_detail)
            or "No health-verified managed preview is currently available.",
            "owner": None,
            "ports": ports,
            **metadata,
        }

    async def _resolve_sleeping_conversation(self, cid8: str, owner_id: str) -> str | None:
        try:
            summaries = await self._store.list_conversation_summaries(
                owner_id=owner_id, limit=500, cursor=None
            )
        except Exception:
            return None
        matches = [
            summary.conversation_id
            for summary in summaries
            if summary.conversation_id.removeprefix("conv_").startswith(cid8)
        ]
        return matches[0] if len(matches) == 1 else None

    async def wake_for_preview(
        self,
        cid8: str,
        port: int,
        *,
        owner_id: str = DEFAULT_OWNER_ID,
        ensure_preview: Callable[[str], Awaitable[bool]],
        resolve_upstream: Callable[[str, int], str | None],
    ) -> str | None:
        cid = await self._live_sessions.resolve_owned_cid_prefix(cid8, owner_id)
        if cid is not None:
            return resolve_upstream(cid, port)
        cid = await self._resolve_sleeping_conversation(cid8, owner_id)
        if cid is None:
            return None
        lock = self._connections.wake_lock_for(cid)
        async with lock:
            if self._live_sessions.live_session(cid) is not None:
                return resolve_upstream(cid, port)
            if not await ensure_preview(cid):
                return None
            for _ in range(10):
                url = resolve_upstream(cid, port)
                if url is not None:
                    return url
                await asyncio.sleep(0.3)
            return resolve_upstream(cid, port)

    @staticmethod
    def _owner_json(owner: PortOwner, namespace: str) -> dict[str, Any]:
        session = owner.session
        if session and session.startswith(namespace):
            session = session[len(namespace) :]
        return {"pid": owner.pid, "cmdline": owner.cmdline, "session": session}

    @classmethod
    def _ports_payload(
        cls,
        owners: dict[int, PortOwner | None],
        namespace: str,
    ) -> list[dict[str, Any]]:
        return [
            {"port": port, "owner": cls._owner_json(owner, namespace)}
            for port, owner in sorted(owners.items())
            if owner is not None and owner.pid is not None
        ]

    @staticmethod
    def _namespace_of(session: Any) -> str:
        try:
            return f"pmx-{session.sessions.namespace}"
        except Exception:  # noqa: BLE001 — a namespace is a label, never a failure
            return ""

    async def _session_ports_payload(
        self,
        session: Any,
        port_owners_fn: PortOwners,
    ) -> list[dict[str, Any]]:
        """Bound USER_PORTS in the AGENT's sandbox, from the one /proc probe."""
        if getattr(session, "_instance", None) is None:
            return []
        owners = await self._preview_owners(session, None, port_owners_fn)
        return self._ports_payload(owners, self._namespace_of(session))

    async def _preview_owners(
        self,
        session: Any,
        target_port: int | None,
        port_owners_fn: PortOwners,
    ) -> dict[int, PortOwner | None]:
        probe_ports = set(USER_PORTS)
        if target_port is not None:
            probe_ports.add(target_port)
        try:
            return await port_owners_fn(session._instance, sorted(probe_ports))
        except Exception:  # noqa: BLE001 — a probe must never 500 the preview endpoint
            return {}

    def _unavailable_payload(
        self,
        *,
        conversation_id: str,
        manager: Any,
        metadata: dict[str, Any],
        managed_detail: str,
        target_port: int | None,
        owner: PortOwner | None,
        ports: list[dict[str, Any]],
        namespace: str,
    ) -> dict[str, Any] | None:
        if metadata["status"] == "unavailable":
            return {
                "available": False,
                "reason": managed_unavailable_reason(metadata["status"], managed_detail),
                "owner": self._owner_json(owner, namespace)
                if owner is not None and owner.pid is not None
                else None,
                "ports": ports,
                **metadata,
            }
        if target_port is None:
            preparing = (
                "Preparing preview: waiting for the platform-managed runtime to start."
                if manager is None
                and self._settings._surface_of(conversation_id) in _BUILD_LIKE_SURFACES
                else None
            )
            return {
                "available": False,
                "reason": managed_unavailable_reason(metadata["status"], managed_detail)
                or preparing
                or "No health-verified managed preview is currently available.",
                "owner": None,
                "ports": ports,
                **metadata,
            }
        if owner is None or owner.pid is None:
            return {
                "available": False,
                "reason": managed_unavailable_reason(metadata["status"], managed_detail)
                or f"No dev server detected. Run one on port {target_port} inside the "
                "sandbox to see a live preview.",
                "owner": None,
                "ports": ports,
                **(metadata if manager is not None else empty_preview_metadata(port=target_port)),
            }
        return None

    async def preview(
        self,
        conversation_id: str,
        *,
        port_owners_fn: PortOwners,
    ) -> dict[str, Any]:
        session = self._live_sessions.live_session(conversation_id)
        if session is None:
            return {
                "available": False,
                "reason": "The agent hasn't started a sandbox yet.",
                "owner": None,
                "ports": [],
                **empty_preview_metadata(),
            }
        manager = getattr(session, "_preview_manager", None)
        metadata, managed_detail = managed_preview_metadata(manager)
        if manager is not None and _owns_isolated_sandbox(manager):
            # The preview runs in the manager's OWN sandbox, but the agent's
            # sandbox keeps running whatever the agent started there. Probe it
            # with the same /proc walk the non-isolated path uses so the Cockpit
            # still sees a server the preview did not start (UI-8) instead of
            # reporting every USER_PORT free.
            return self._isolated_preview_payload(
                manager,
                metadata,
                managed_detail,
                await self._session_ports_payload(session, port_owners_fn),
            )
        if getattr(session, "_instance", None) is None:
            return {
                "available": False,
                "reason": managed_unavailable_reason(metadata["status"], managed_detail)
                or "The agent's sandbox isn't running yet.",
                "owner": None,
                "ports": [],
                **metadata,
            }
        target_port = self.preview_target_port(conversation_id)
        owners = await self._preview_owners(session, target_port, port_owners_fn)
        owner = owners.get(target_port) if target_port is not None else None
        namespace = f"pmx-{session.sessions.namespace}"
        ports = self._ports_payload(owners, namespace)
        unavailable = self._unavailable_payload(
            conversation_id=conversation_id,
            manager=manager,
            metadata=metadata,
            managed_detail=managed_detail,
            target_port=target_port,
            owner=owner,
            ports=ports,
            namespace=namespace,
        )
        if unavailable is not None:
            return unavailable
        assert owner is not None
        return {
            "available": True,
            "proxy": True,
            "owner": self._owner_json(owner, namespace),
            "ports": ports,
            **(metadata if manager is not None else empty_preview_metadata(port=target_port)),
        }
