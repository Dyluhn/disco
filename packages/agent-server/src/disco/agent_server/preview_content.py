"""Contract-bound reads from an already-owned finished Preview runtime."""

from __future__ import annotations

from .live_session_directory import LiveSessionDirectory
from .preview_projection import SealedPreviewRuntimeContract


class PreviewContentService:
    def __init__(self, live_sessions: LiveSessionDirectory) -> None:
        self._live_sessions = live_sessions

    async def request_finished_inside(
        self,
        conversation_id: str,
        contract: SealedPreviewRuntimeContract,
        port: int,
        method: str,
        target: str,
        headers: list[tuple[str, str]],
        body: bytes,
        *,
        projection_id: str,
    ) -> tuple[int, list[tuple[str, str]], bytes] | None:
        if contract.conversation_id != conversation_id:
            return None
        session = self._live_sessions.live_session(conversation_id)
        manager = getattr(session, "_preview_manager", None) if session is not None else None
        if manager is None:
            return None
        return await manager.request_finished_inside(
            contract, port, method, target, headers, body, projection_id=projection_id
        )

    async def fetch_finished_inside(
        self,
        conversation_id: str,
        contract: SealedPreviewRuntimeContract,
        port: int,
        path: str,
    ) -> tuple[int, bytes, str] | None:
        """Read a sealed preview from its manager-owned runtime, never the host fallback."""
        if contract.conversation_id != conversation_id:
            return None
        session = self._live_sessions.live_session(conversation_id)
        manager = getattr(session, "_preview_manager", None) if session is not None else None
        if manager is None:
            return None
        return await manager.fetch_finished_inside(contract, port, path)
