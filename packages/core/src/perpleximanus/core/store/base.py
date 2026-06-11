"""The EventStore interface — event-state-contract.md §6.

Every subsystem that reads or writes history goes through this seam. The
filesystem/SQLite implementation is v1; this interface is what keeps
Postgres/object-storage a swap, not a rewrite (BoD §4.1, §7.2).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from ..events import Event, EventKind, EventSource
from ..state import ConversationState


class EventFilter(BaseModel):
    """Query predicate for history reads. All fields AND together; None = no
    constraint on that dimension."""

    kinds: list[EventKind] | None = None
    sources: list[EventSource] | None = None
    after_seq: int | None = None  # exclusive lower bound
    before_seq: int | None = None  # exclusive upper bound
    since: datetime | None = None
    until: datetime | None = None


class Page(BaseModel):
    """One page of paginated history. `next_cursor` is passed back as
    `after_seq` to continue; None means the end."""

    events: list[Event]
    next_cursor: int | None


class ConversationSummary(BaseModel):
    """A conversation's library-row metadata — what the History surface lists
    (owner-scoped, §6.1). Distinct from the event log; read from the
    conversations table, not reconstructed from events."""

    conversation_id: str
    owner_id: str
    title: str | None = None
    created_at: str  # ISO-8601
    status: str | None = None
    surface: str = "research"  # "research" | "build" | "deep_research" — for History routing


@runtime_checkable
class EventStore(Protocol):
    """[CONTRACT] The persistence boundary for events.

    Guarantees implementations MUST uphold:
      G1 append() assigns a monotonic, gap-free, per-conversation seq and
         returns the event with seq populated. Appends to one conversation are
         serialized (no two events share a seq).
      G2 append() is atomic and durable before it returns.
      G3 get_events() returns events in ascending seq order, gap-free.
      G4 idempotency: appending an event whose (conversation_id, id) already
         exists is a no-op returning the existing event (enables safe retries).
      G5 reads never block writes pathologically.
    """

    async def append(self, conversation_id: str, event: Event) -> Event: ...

    async def append_many(self, conversation_id: str, events: list[Event]) -> list[Event]: ...

    async def get_events(
        self, conversation_id: str, filter: EventFilter | None = None
    ) -> list[Event]: ...

    async def paginate(
        self,
        conversation_id: str,
        *,
        after_seq: int | None = None,
        limit: int = 100,
        filter: EventFilter | None = None,
    ) -> Page: ...

    async def get_state(self, conversation_id: str) -> ConversationState: ...

    async def subscribe(
        self, conversation_id: str, after_seq: int | None = None
    ) -> AsyncIterator[Event]: ...

    def publish_ephemeral(self, conversation_id: str, frame: dict) -> None:
        """Broadcast a transient, NON-persisted frame to live subscribers (e.g.
        watch-it-write file-stream deltas). Fire-and-forget; dropped if no live
        listener. Display-only — the durable record is the final persisted event."""
        ...

    async def subscribe_ephemeral(self, conversation_id: str) -> AsyncIterator[dict]:
        """Live-only stream of transient frames (no history, no replay)."""
        ...

    async def conversation_exists(self, conversation_id: str) -> bool: ...

    async def list_conversations(
        self, *, owner_id: str, limit: int = 50, cursor: str | None = None
    ) -> list[str]: ...
