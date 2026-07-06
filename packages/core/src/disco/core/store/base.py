"""The EventStore interface — event-state-contract.md §6.

Every subsystem that reads or writes history goes through this seam. The
filesystem/SQLite implementation is v1; this interface is what keeps
Postgres/object-storage a swap, not a rewrite (BoD §4.1, §7.2).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

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
    space_id: str | None = None
    title: str | None = None
    created_at: str  # ISO-8601
    status: str | None = None
    surface: str = "research"  # "research" | "build" | "agent" | "deep_research" — History routing
    origin: str | None = None  # "imported" for a read-only bundle import; None otherwise


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

    # ---- external Definition-of-Done (C1a: storage + accessor) ----------------
    # The DoD spec is the structural fix for "the agent verifies its own work":
    # a list of machine-checkable acceptance predicates (file_exists / command
    # / http_ok) the agent itself never writes. It lives in a sibling table to
    # `conversations` and `events` — the agent has no tool that mutates it.
    # Set ONCE per conversation; subsequent set/replace calls raise
    # `DoDSpecAlreadySet` and leave the original intact (write-once gate).
    # The C1b evaluator will use `get_dod_spec` to gate `finish`; the C1c
    # wire-up will capture the spec from the user request / `submit_plan`.

    async def set_dod_spec(
        self, conversation_id: str, spec: Any, *, set_by: str = "system"
    ) -> Any:
        """Persist the DoD spec for a conversation. WRITE-ONCE: a second call
        with the same `conversation_id` raises `DoDSpecAlreadySet`. The agent
        has no tool that reaches this method — see `core/dod.py` for the
        immutability argument."""
        ...

    async def get_dod_spec(self, conversation_id: str) -> Any:
        """Accessor. Returns the stored `DoDSpec` or `None` when no spec has
        been captured yet. The accessor is a PURE READ — it does not copy or
        wrap the spec, and the spec itself is frozen (in-process mutation is
        a `ValidationError`)."""
        ...

    async def replace_dod_spec(
        self, conversation_id: str, spec: Any, *, actor: str = "system"
    ) -> Any:
        """Write-once BOOTSTRAP + MONOTONIC replacement. The spec may be
        EXTENDED (a mid-build steer that adds acceptance scope) but never
        WEAKENED. Contract:
          * no spec exists → bootstrap (equivalent to `set_dod_spec`);
          * spec exists AND the new spec is a monotonic extension of it
            (`dod.is_monotonic_extension` — only adds predicates, or renames a
            committed deliverable via an explicit `renamed_from`, never drops
            one) → update in place;
          * spec exists AND the new spec would WEAKEN it → raise
            `DoDSpecAlreadySet` (original preserved).
        `actor` is audited as `set_by` on an accepted update but cannot buy a
        weakening — the security property the write-once design protected is
        preserved as monotonicity (the agent can only tighten, never relax, its
        own acceptance bar)."""
        ...

    async def conversation_exists(self, conversation_id: str) -> bool: ...

    async def update_title(self, conversation_id: str, title: str) -> None: ...

    async def get_title(self, conversation_id: str) -> str | None: ...

    async def list_conversations(
        self, *, owner_id: str, limit: int = 50, cursor: str | None = None
    ) -> list[str]: ...

    async def list_conversation_summaries(
        self,
        *,
        owner_id: str,
        limit: int = 50,
        cursor: str | None = None,
        nonempty_only: bool = False,
        space_id: str | None = None,
    ) -> list[ConversationSummary]: ...

    async def set_conversation_space(
        self, conversation_id: str, space_id: str | None
    ) -> None: ...

    async def clear_space_members(self, space_id: str, *, owner_id: str | None = None) -> None: ...
