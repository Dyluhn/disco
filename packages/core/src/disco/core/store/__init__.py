"""Event persistence — the `EventStore` seam (event-state-contract.md §6)."""

from __future__ import annotations

from .base import ConversationSummary, EventFilter, EventStore, Page
from .sqlite import DEFAULT_OWNER_ID, SqliteEventStore

__all__ = [
    "DEFAULT_OWNER_ID",
    "ConversationSummary",
    "EventFilter",
    "EventStore",
    "Page",
    "SqliteEventStore",
]
