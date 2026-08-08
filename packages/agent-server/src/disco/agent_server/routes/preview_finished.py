"""Shared durable status query for finished Preview routes."""

from __future__ import annotations

from disco.core import ConversationStatus, StatusEvent
from disco.core.store.sqlite import SqliteEventStore

from ..runtime import ConversationRuntime
from .preview_browser import _canonical_preview_port


async def is_finished_conversation(
    store: SqliteEventStore,
    conversation_id: str,
) -> bool:
    """Return only a durably observed FINISHED state; uncertainty fails closed."""

    try:
        events = await store.get_events(conversation_id)
    except Exception:  # noqa: BLE001 — missing status authority fails closed
        return False
    latest = next((event for event in reversed(events) if isinstance(event, StatusEvent)), None)
    return latest is not None and latest.status is ConversationStatus.FINISHED


async def refresh_canonical_preview_port(
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
    conversation_id: str,
    selected_port: int | None,
    *,
    canonical: bool,
    current: bool,
) -> int | None:
    """Establish sealed authority before selecting a current FINISHED port."""

    if not canonical or runtime is None:
        return selected_port
    finished = current and await is_finished_conversation(store, conversation_id)
    if selected_port is not None and not finished:
        return selected_port
    restored = await runtime.preview.ensure_preview(conversation_id)
    if finished and not restored:
        return None
    return _canonical_preview_port(runtime, conversation_id)
