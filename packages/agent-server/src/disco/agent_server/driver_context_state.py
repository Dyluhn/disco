"""Owned immutable driver-context snapshots and bounded resolution."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from disco.core.llm import ModelEntry

from .driver_context import (
    DriverContextResolver,
    ResolvedDriverContext,
)


class DriverContextState:
    """Own per-run composition and resolved driver snapshots."""

    def __init__(self, timeout_s: float = 5.0) -> None:
        self._resolver = DriverContextResolver(timeout_s=timeout_s)
        self._compose: dict[str, ResolvedDriverContext] = {}
        self._resolved: dict[str, ResolvedDriverContext] = {}

    async def resolve(
        self,
        *,
        model_key: str,
        entry: ModelEntry,
        probe: Callable[[], Awaitable[dict[str, Any]]] | None,
        unavailable_source: str | None,
    ) -> ResolvedDriverContext:
        return await self._resolver.resolve(
            model_key=model_key,
            entry=entry,
            probe=probe,
            unavailable_source=unavailable_source,
        )

    def compose_snapshot(self, conversation_id: str) -> ResolvedDriverContext | None:
        return self._compose.get(conversation_id)

    def resolved_snapshot(self, conversation_id: str) -> ResolvedDriverContext | None:
        return self._resolved.get(conversation_id)

    def compose_model_key(self, conversation_id: str) -> str | None:
        snapshot = self._compose.get(conversation_id)
        return snapshot.model_key if snapshot is not None else None

    def begin_compose(self, conversation_id: str, snapshot: ResolvedDriverContext) -> None:
        self._compose[conversation_id] = snapshot

    def end_compose(self, conversation_id: str, snapshot: ResolvedDriverContext) -> None:
        if self._compose.get(conversation_id) is snapshot:
            self._compose.pop(conversation_id, None)

    def bind_resolved(self, conversation_id: str, snapshot: ResolvedDriverContext) -> None:
        self._resolved[conversation_id] = snapshot

    def discard(self, conversation_id: str) -> None:
        self._compose.pop(conversation_id, None)
        self._resolved.pop(conversation_id, None)
