"""Durable user-visible persistence notices."""

from __future__ import annotations

from typing import Any

from disco.core import EventSource, LLMMessage, MessageEvent
from disco.core.store.sqlite import SqliteEventStore


class PersistenceNotifier:
    def __init__(self, store: SqliteEventStore) -> None:
        self._store = store

    async def emit(
        self,
        conversation_id: str,
        body: str,
        *,
        meta: dict[str, Any] | None = None,
    ) -> None:
        await self._store.append(
            conversation_id,
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(
                    role="user",
                    content=(
                        f"<system-reminder>\nProject persistence note: {body}\n"
                        "</system-reminder>"
                    ),
                ),
                meta=meta or {},
            ),
        )
