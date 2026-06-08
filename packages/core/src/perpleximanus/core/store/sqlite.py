"""SQLite EventStore — event-state-contract.md §6.2 (the v1 [INTERIOR] impl).

Dependency-free: stdlib `sqlite3` under a per-store asyncio.Lock. The store is
single-process/single-event-loop in v1; the lock makes the read-max-then-insert
critical section atomic across awaits, which is what gives G1 (monotonic,
gap-free seq) even under concurrent `append()` coroutines. WAL mode gives G5
(reads don't block writes pathologically).

`subscribe()` is an in-process pub/sub (per-conversation asyncio.Queue fan-out):
on connect it drains history after `after_seq`, then streams live appends,
deduping the overlap by seq. This is the §7 reconnect/replay path; the WebSocket
endpoint (deferred to the server package) wraps it.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections import defaultdict
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path

from ..events import Event, EventAdapter, event_to_json_dict
from ..migration import migrate_event
from ..state import ConversationState
from .base import ConversationSummary, EventFilter, Page

# v1 single-user: every conversation carries an owner_id from day one (§6.1).
# It is a constant until the auth module is enabled (BoD §4.1).
DEFAULT_OWNER_ID = "local"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    conversation_id TEXT    NOT NULL,
    seq             INTEGER NOT NULL,
    id              TEXT    NOT NULL,
    kind            TEXT    NOT NULL,
    source          TEXT    NOT NULL,
    created_at      TEXT    NOT NULL,
    payload         TEXT    NOT NULL,
    PRIMARY KEY (conversation_id, seq),
    UNIQUE (conversation_id, id)
);
CREATE TABLE IF NOT EXISTS conversations (
    conversation_id TEXT PRIMARY KEY,
    owner_id        TEXT NOT NULL,
    space_id        TEXT,
    title           TEXT,
    created_at      TEXT NOT NULL,
    status          TEXT,
    surface         TEXT  -- "research" | "build" | "deep_research" (set at create)
);
CREATE INDEX IF NOT EXISTS idx_conversations_owner
    ON conversations (owner_id, created_at DESC);
"""


def _row_to_event(payload: str) -> Event:
    """Deserialize a stored payload: JSON -> migrate -> validate (§4)."""
    return EventAdapter.validate_python(migrate_event(json.loads(payload)))


class SqliteEventStore:
    """An `EventStore` (store/base.py) backed by a single SQLite file.

    Pass a filesystem path for durability across reopens (the G2 restart path),
    or ":memory:" for ephemeral use (tests that don't reopen).
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=FULL;")  # durable on commit (G2)
        self._conn.executescript(_SCHEMA)
        # Migration: add `surface` to pre-existing conversations tables (CREATE TABLE
        # IF NOT EXISTS won't add a new column). Idempotent — ignore "duplicate".
        try:
            self._conn.execute("ALTER TABLE conversations ADD COLUMN surface TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists
        self._conn.commit()
        self._write_lock = asyncio.Lock()
        # conversation_id -> set of live subscriber queues.
        self._subscribers: dict[str, set[asyncio.Queue[Event]]] = defaultdict(set)
        # conversation_id -> set of EPHEMERAL subscriber queues. These carry
        # transient frames (watch-it-write file-stream deltas) that are broadcast
        # to live listeners but NEVER persisted — they're display-only and would
        # bloat the event log (hundreds per file). No history, live subscribers
        # only; a late joiner simply misses in-flight deltas and gets the final
        # persisted ActionEvent instead.
        self._eph_subscribers: dict[str, set[asyncio.Queue[dict]]] = defaultdict(set)

    def close(self) -> None:
        self._conn.close()

    # ---- conversation metadata (extends the protocol; used by app-server) ----

    def create_conversation(
        self,
        conversation_id: str,
        *,
        owner_id: str = DEFAULT_OWNER_ID,
        space_id: str | None = None,
        title: str | None = None,
        surface: str | None = None,
    ) -> None:
        """Register a conversation with explicit ownership (§6.1). Idempotent.
        Auto-creation on first append uses DEFAULT_OWNER_ID; call this to set a
        real owner/space/title/surface up front (the §7.5 POST /conversations path).
        `surface` ("research"|"build"|"deep_research") is persisted so History can
        route an item to the right surface — even a still-running one with no
        report yet (the durable answer to which kind of task this is)."""
        self._conn.execute(
            "INSERT OR IGNORE INTO conversations "
            "(conversation_id, owner_id, space_id, title, created_at, surface) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (conversation_id, owner_id, space_id, title, datetime.now().isoformat(), surface),
        )
        self._conn.commit()

    # ---- writes --------------------------------------------------------------

    def _store_one(self, conversation_id: str, event: Event) -> tuple[Event, bool]:
        """Persist one event under the write lock. Returns (stored_event,
        is_new). Idempotent on (conversation_id, id) — G4."""
        # G4: existing id -> no-op, return what's already there.
        existing = self._conn.execute(
            "SELECT payload FROM events WHERE conversation_id = ? AND id = ?",
            (conversation_id, event.id),
        ).fetchone()
        if existing is not None:
            return _row_to_event(existing["payload"]), False

        # Ensure the conversation exists (auto-create with default owner).
        self._conn.execute(
            "INSERT OR IGNORE INTO conversations (conversation_id, owner_id, created_at) "
            "VALUES (?, ?, ?)",
            (conversation_id, DEFAULT_OWNER_ID, datetime.now().isoformat()),
        )

        # G1: next seq = max+1 within this conversation.
        row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM events WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        next_seq = int(row["next"])

        stored = event.model_copy(update={"seq": next_seq})
        payload = event_to_json_dict(stored)
        self._conn.execute(
            "INSERT INTO events (conversation_id, seq, id, kind, source, created_at, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                conversation_id,
                next_seq,
                stored.id,
                payload["kind"],
                payload["source"],
                payload["timestamp"],
                json.dumps(payload),
            ),
        )
        return stored, True

    def _publish(self, conversation_id: str, event: Event) -> None:
        for q in self._subscribers.get(conversation_id, set()):
            q.put_nowait(event)

    def publish_ephemeral(self, conversation_id: str, frame: dict) -> None:
        """Broadcast a transient (non-persisted) frame to live subscribers. Used
        for watch-it-write file-stream deltas. Fire-and-forget; if there is no
        listener the frame is simply dropped (the final ActionEvent is the durable
        record). Sync + non-blocking so the agent loop can call it inline."""
        for q in self._eph_subscribers.get(conversation_id, set()):
            q.put_nowait(frame)

    async def subscribe_ephemeral(self, conversation_id: str) -> AsyncIterator[dict]:
        """Live-only stream of transient frames for a conversation (no history)."""
        return self._subscribe_ephemeral(conversation_id)

    async def _subscribe_ephemeral(self, conversation_id: str) -> AsyncIterator[dict]:
        queue: asyncio.Queue[dict] = asyncio.Queue()
        self._eph_subscribers[conversation_id].add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            self._eph_subscribers[conversation_id].discard(queue)

    async def append(self, conversation_id: str, event: Event) -> Event:
        async with self._write_lock:
            stored, is_new = self._store_one(conversation_id, event)
            if is_new:
                self._conn.commit()  # G2: durable before return
                self._publish(conversation_id, stored)
        return stored

    async def append_many(self, conversation_id: str, events: list[Event]) -> list[Event]:
        stored_new: list[Event] = []
        results: list[Event] = []
        async with self._write_lock:
            for event in events:
                stored, is_new = self._store_one(conversation_id, event)
                results.append(stored)
                if is_new:
                    stored_new.append(stored)
            self._conn.commit()  # batch durable atomically (G2)
            for ev in stored_new:
                self._publish(conversation_id, ev)
        return results

    # ---- reads ---------------------------------------------------------------

    def _query(
        self, conversation_id: str, filter: EventFilter | None, limit: int | None = None
    ) -> list[Event]:
        clauses = ["conversation_id = ?"]
        params: list[object] = [conversation_id]
        if filter is not None:
            if filter.kinds:
                clauses.append(f"kind IN ({','.join('?' * len(filter.kinds))})")
                params.extend(k.value for k in filter.kinds)
            if filter.sources:
                clauses.append(f"source IN ({','.join('?' * len(filter.sources))})")
                params.extend(s.value for s in filter.sources)
            if filter.after_seq is not None:
                clauses.append("seq > ?")
                params.append(filter.after_seq)
            if filter.before_seq is not None:
                clauses.append("seq < ?")
                params.append(filter.before_seq)
            if filter.since is not None:
                clauses.append("created_at >= ?")
                params.append(filter.since.isoformat())
            if filter.until is not None:
                clauses.append("created_at <= ?")
                params.append(filter.until.isoformat())
        sql = f"SELECT payload FROM events WHERE {' AND '.join(clauses)} ORDER BY seq ASC"  # G3
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_event(r["payload"]) for r in rows]

    async def get_events(
        self, conversation_id: str, filter: EventFilter | None = None
    ) -> list[Event]:
        return self._query(conversation_id, filter)

    async def paginate(
        self,
        conversation_id: str,
        *,
        after_seq: int | None = None,
        limit: int = 100,
        filter: EventFilter | None = None,
    ) -> Page:
        # Merge the cursor's after_seq into the filter (cursor wins as a floor).
        eff = (filter or EventFilter()).model_copy()
        if after_seq is not None:
            eff = eff.model_copy(update={"after_seq": after_seq})
        # Fetch one extra to know whether another page exists.
        rows = self._query(conversation_id, eff, limit=limit + 1)
        has_more = len(rows) > limit
        events = rows[:limit]
        next_cursor = events[-1].seq if (has_more and events) else None
        return Page(events=events, next_cursor=next_cursor)

    async def get_state(self, conversation_id: str) -> ConversationState:
        events = self._query(conversation_id, None)
        return ConversationState.reconstruct(conversation_id, events)

    async def conversation_exists(self, conversation_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM conversations WHERE conversation_id = ? LIMIT 1",
            (conversation_id,),
        ).fetchone()
        return row is not None

    async def list_conversations(
        self, *, owner_id: str, limit: int = 50, cursor: str | None = None
    ) -> list[str]:
        # [INTERIOR] v1 cursor = opaque integer offset string. The ownership
        # filter is the load-bearing part (§6.1): no cross-owner data, ever.
        offset = int(cursor) if cursor else 0
        rows = self._conn.execute(
            "SELECT conversation_id FROM conversations WHERE owner_id = ? "
            "ORDER BY created_at DESC, conversation_id DESC LIMIT ? OFFSET ?",
            (owner_id, limit, offset),
        ).fetchall()
        return [r["conversation_id"] for r in rows]

    async def list_conversation_summaries(
        self, *, owner_id: str, limit: int = 50, cursor: str | None = None
    ) -> list[ConversationSummary]:
        """Owner-scoped library rows (id/title/created_at), newest first — what the
        History surface lists (§6.1). Same ownership filter as `list_conversations`;
        no cross-owner data, ever."""
        offset = int(cursor) if cursor else 0
        rows = self._conn.execute(
            "SELECT conversation_id, owner_id, title, created_at, surface FROM conversations "
            "WHERE owner_id = ? ORDER BY created_at DESC, conversation_id DESC LIMIT ? OFFSET ?",
            (owner_id, limit, offset),
        ).fetchall()
        return [
            ConversationSummary(
                conversation_id=r["conversation_id"],
                owner_id=r["owner_id"],
                title=r["title"],
                created_at=r["created_at"],
                surface=r["surface"] or "research",  # null (pre-migration) → research
            )
            for r in rows
        ]

    async def delete_conversation(self, conversation_id: str, *, owner_id: str) -> bool:
        """Delete a conversation and all its events — OWNER-SCOPED (a caller can
        only delete its own, §6.1). Returns True if a row was removed. Destructive
        and irreversible; the gate is the UI confirm."""
        async with self._write_lock:
            cur = self._conn.execute(
                "DELETE FROM conversations WHERE conversation_id = ? AND owner_id = ?",
                (conversation_id, owner_id),
            )
            if cur.rowcount == 0:
                return False  # not found OR not owned — no cross-owner deletes
            self._conn.execute(
                "DELETE FROM events WHERE conversation_id = ?", (conversation_id,)
            )
            self._conn.commit()
        return True

    # ---- live subscription (§7 reconnect/replay path) ------------------------

    async def subscribe(
        self, conversation_id: str, after_seq: int | None = None
    ) -> AsyncIterator[Event]:
        """Drain history after `after_seq`, then stream live appends.

        Per the §6 contract this is awaited to obtain the async iterator:
            async for ev in await store.subscribe(cid, after_seq=k): ...
        """
        return self._subscribe(conversation_id, after_seq)

    async def _subscribe(self, conversation_id: str, after_seq: int | None) -> AsyncIterator[Event]:
        # Register the live queue FIRST so no append is missed between the
        # history snapshot and going live; the overlap is deduped by seq.
        queue: asyncio.Queue[Event] = asyncio.Queue()
        self._subscribers[conversation_id].add(queue)
        try:
            history = self._query(
                conversation_id, EventFilter(after_seq=after_seq) if after_seq else None
            )
            last_seq = after_seq or 0
            for ev in history:
                if ev.seq is not None:
                    last_seq = max(last_seq, ev.seq)
                yield ev
            while True:
                ev = await queue.get()
                if ev.seq is not None and ev.seq <= last_seq:
                    continue  # already delivered from history (overlap dedup)
                if ev.seq is not None:
                    last_seq = ev.seq
                yield ev
        finally:
            self._subscribers[conversation_id].discard(queue)
