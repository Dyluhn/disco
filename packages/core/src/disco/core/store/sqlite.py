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
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ..events import Event, EventAdapter, event_to_json_dict
from ..migration import migrate_event
from ..state import ConversationState
from .base import ConversationSummary, EventFilter, Page

if TYPE_CHECKING:  # annotations only; runtime uses a local import (dod.py is a leaf)
    from ..dod import DoDSpec

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
CREATE TABLE IF NOT EXISTS share_tokens (
    -- RP-06: revocable share-link tokens (base62 random) for static-bundle replay.
    -- The token IS the URL slug; the row points back at the conversation whose
    -- events the bundle was built from + the bundle's metadata. Revocation =
    -- setting `revoked_at`; lookups must filter `revoked_at IS NULL`. The
    -- PRIMARY KEY is the token (random collision-free base62 over a 16-byte
    -- entropy pool = ~22 chars; the column width is generous to allow future
    -- entropy bumps without a migration).
    token          TEXT    PRIMARY KEY,
    conversation_id TEXT   NOT NULL,
    owner_id       TEXT    NOT NULL,
    created_at     TEXT    NOT NULL,
    revoked_at     TEXT,                  -- NULL = active; ISO-8601 if revoked
    bundle_seq     INTEGER NOT NULL,      -- the last_seq captured at export time
    -- The bundle is rebuilt on demand (events are append-only; the share
    -- selector walks the live log) — this column is the seq boundary the
    -- future re-export uses to detect "the conversation moved since the link
    -- was created" and rebuild from scratch (a real-time event on the
    -- conversation AFTER share creation → the viewer shows a "newer events
    -- available" hint; we never auto-include them, the user re-exports).
    FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id)
);
CREATE INDEX IF NOT EXISTS idx_share_tokens_conv
    ON share_tokens (conversation_id);
CREATE TABLE IF NOT EXISTS schedules (
    -- RP-08: cron-style recurring agent runs. One row per user-created schedule.
    -- `rrule` stores a cron expression (cronsim-parseable, 5-field standard cron).
    -- `depth` + `model_override` reproduce the user's settings on each scheduled
    -- run. `next_run` is updated after each fire (always a future time after the
    -- coalesced catch-up policy runs). `enabled = 0` pauses without deleting.
    schedule_id     TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    owner_id        TEXT NOT NULL,
    rrule           TEXT NOT NULL,
    description     TEXT NOT NULL,
    depth           TEXT,
    model_override  TEXT,
    created_at      TEXT NOT NULL,
    enabled         INTEGER NOT NULL DEFAULT 1,
    next_run        TEXT,
    FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id)
);
CREATE INDEX IF NOT EXISTS idx_schedules_owner
    ON schedules (owner_id, conversation_id);
CREATE TABLE IF NOT EXISTS schedule_runs (
    -- RP-08: audit log of every scheduled run that fired. `coalesced = 1` when
    -- N missed fires were coalesced into this single catch-up run (downtime policy).
    run_id          TEXT PRIMARY KEY,
    schedule_id     TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    fired_at        TEXT NOT NULL,
    coalesced       INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (schedule_id) REFERENCES schedules(schedule_id)
);
CREATE INDEX IF NOT EXISTS idx_schedule_runs_sched
    ON schedule_runs (schedule_id, fired_at DESC);
CREATE TABLE IF NOT EXISTS mcp_approvals (
    -- RP-05: per-server MCP tool-description approvals. One row per server;
    -- the description_hash is the SHA-256 fingerprint of the canonicalized
    -- tool descriptions the operator approved. A hash mismatch on pool
    -- start raises ApprovalRequired (the server refuses to start until
    -- re-approval). The PK is the server name (matching McpServerConfig.name).
    server          TEXT PRIMARY KEY,
    description_hash TEXT NOT NULL,
    approved_at     TEXT NOT NULL,  -- ISO-8601
    approved_by     TEXT NOT NULL   -- operator username
);
CREATE TABLE IF NOT EXISTS mcp_approval_pending (
    -- E6 (#10): per-server DRIFT row, written by the agent-server when its
    -- live pool detects a description_hash mismatch against mcp_approvals at
    -- startup. Carries the AUTHORITATIVE new_hash the live server advertised
    -- (NOT a recompute). The app-server reads this on GET /api/mcp to
    -- surface the REAL new_hash on the ApprovalDiff. The agent-server clears
    -- the row when the operator accepts the new tool descriptions via
    -- POST /api/mcp/servers/{name}/approve (create_mcp_approval deletes it
    -- in the same transaction). Without this table the UI sees only the
    -- STORED (old) hash from mcp_approvals and the diff is useless (or
    -- worse, shows the same value for both old and new).
    server          TEXT PRIMARY KEY,
    old_hash        TEXT NOT NULL,  -- the LAST APPROVED hash (mcp_approvals.description_hash)
    new_hash        TEXT NOT NULL,  -- the AUTHORITATIVE live pool's hash
    detected_at     TEXT NOT NULL   -- ISO-8601
);
CREATE TABLE IF NOT EXISTS dod_specs (
    -- C1a: external Definition-of-Done spec, one row per conversation.
    --
    -- Sibling to `conversations` and `events`, OUTSIDE the agent-editable
    -- event stream. The agent has no tool that mutates this table: the only
    -- writer is the store's `set_dod_spec` (server-side, NOT exposed as a
    -- tool) and that writer is WRITE-ONCE — a second call with the same
    -- `conversation_id` raises `DoDSpecAlreadySet`. The `replace_dod_spec`
    -- method is the named, always-raise hook for any future "weaken"
    -- affordance to fail loudly rather than silently mutate.
    --
    -- The `spec` column carries the serialized DoDSpec (predicates + meta);
    -- we keep the predicates as JSON text rather than relational rows so
    -- the spec is a single atomic read/write — no half-written predicates,
    -- no "spec exists but its predicates are gone" half-states.
    --
    -- `set_at` + `set_by` are audit fields, never consulted by the
    -- evaluator. The PK is `conversation_id` (one spec per conversation).
    conversation_id TEXT PRIMARY KEY,
    spec            TEXT NOT NULL,  -- JSON-encoded DoDSpec
    set_at          TEXT NOT NULL,  -- ISO-8601
    set_by          TEXT NOT NULL,  -- "system" | "user" | "plan:<step-id>" — audit only
    FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id)
);
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
        # Retained so sidecar persistence (runtime B0 overrides/surfaces/autonomous)
        # can anchor next to the REAL event DB instead of re-deriving from env —
        # the two diverging silently disabled every sidecar on deployments that
        # set the DB path only at store construction (live-caught 2026-07-03).
        self.db_path: str = "" if str(path) == ":memory:" else str(path)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=FULL;")  # durable on commit (G2)
        self._conn.executescript(_SCHEMA)
        # Migration: add `surface` to pre-existing conversations tables (CREATE TABLE
        # IF NOT EXISTS won't add a new column). Idempotent — ignore "duplicate".
        try:
            self._conn.execute("ALTER TABLE conversations ADD COLUMN space_id TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists
        try:
            self._conn.execute("ALTER TABLE conversations ADD COLUMN surface TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists
        try:
            self._conn.execute("ALTER TABLE conversations ADD COLUMN status TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists
        try:
            # `origin` marks a conversation that was IMPORTED from a share bundle
            # (untrusted third-party data) — used to enforce read-only at the server
            # edge and badge it in the UI. NULL = a normal first-party conversation.
            self._conn.execute("ALTER TABLE conversations ADD COLUMN origin TEXT")
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
        origin: str | None = None,
    ) -> None:
        """Register a conversation with explicit ownership (§6.1). Idempotent.
        Auto-creation on first append uses DEFAULT_OWNER_ID; call this to set a
        real owner/space/title/surface up front (the §7.5 POST /conversations path).
        `surface` ("research"|"build"|"agent"|"deep_research") is persisted so History
        can route an item to the right surface. `origin="imported"` marks a bundle
        import (untrusted, read-only) — NULL for a normal first-party conversation."""
        self._conn.execute(
            "INSERT OR IGNORE INTO conversations "
            "(conversation_id, owner_id, space_id, title, created_at, surface, origin) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                conversation_id,
                owner_id,
                space_id,
                title,
                datetime.now().isoformat(),
                surface,
                origin,
            ),
        )
        self._conn.commit()

    async def update_title(self, conversation_id: str, title: str) -> None:
        """Set a conversation's display title (the History/Projects label).

        Used by the auto-title service to replace the `'(untitled)'` default with a
        model-summarized title derived from the first user message. Overwrites the
        stored title unconditionally — the 'only when unset' gate lives in the caller
        (so a future user-supplied rename can also use this path). No-op on an unknown
        id (UPDATE of zero rows)."""
        async with self._write_lock:
            with self._conn:
                self._conn.execute(
                    "UPDATE conversations SET title = ? WHERE conversation_id = ?",
                    (title, conversation_id),
                )

    async def get_title(self, conversation_id: str) -> str | None:
        """The stored title (None if unset / unknown id). Cheap point-read used by
        the auto-title service to stay idempotent (skip already-titled conversations)."""
        cur = self._conn.execute(
            "SELECT title FROM conversations WHERE conversation_id = ?",
            (conversation_id,),
        )
        row = cur.fetchone()
        return row["title"] if row else None

    # ---- DoD spec (C1a: storage + accessor + immutability) -------------------
    # The DoD spec lives in a sibling table to `conversations` and `events`,
    # OUTSIDE the agent-editable event stream. There is NO agent tool that
    # calls these methods. `set_dod_spec` is WRITE-ONCE: a second call with
    # the same conversation_id raises DoDSpecAlreadySet and leaves the
    # original intact. `replace_dod_spec` is the named, always-raise hook for
    # any future "weaken" affordance to fail loudly. See `core/dod.py` for
    # the full immutability argument.

    async def set_dod_spec(
        self, conversation_id: str, spec: DoDSpec, *, set_by: str = "system"
    ) -> DoDSpec:
        """Persist the DoD spec for a conversation. WRITE-ONCE: a second call
        raises `DoDSpecAlreadySet` and the original is preserved.

        `set_by` is an audit label (never consulted by the evaluator). We use
        a transaction (write-lock + sqlite txn) so a concurrent race between
        two `set_dod_spec` calls cannot interleave a half-written spec —
        the second caller sees the row and raises.

        The spec is serialized as a single JSON blob: predicates + meta
        round-trip atomically. Validation is the caller's job (build a
        `DoDSpec` via the Pydantic model); we don't re-validate on write
        beyond the JSON round-trip, because the spec is frozen upstream."""
        from ..dod import DoDSpec, DoDSpecAlreadySet  # local import: dod.py is a leaf
        if not isinstance(spec, DoDSpec):
            # Don't accept free-form dicts here — the storage shape is the
            # Pydantic model. Callers go through DoDSpec(predicates=...).
            raise TypeError(
                f"set_dod_spec expects a DoDSpec, got {type(spec).__name__}"
            )
        payload = spec.to_json_dict()
        now = datetime.now(UTC).isoformat()
        async with self._write_lock:
            with self._conn:
                # Idempotency check inside the txn: if a row already exists,
                # raise without touching it. The PRIMARY KEY constraint would
                # also catch a naive double-insert, but checking first lets us
                # raise the precise exception (and not the sqlite IntegrityError).
                existing = self._conn.execute(
                    "SELECT 1 FROM dod_specs WHERE conversation_id = ?",
                    (conversation_id,),
                ).fetchone()
                if existing is not None:
                    raise DoDSpecAlreadySet(
                        f"DoD spec for {conversation_id!r} is already set; "
                        "the spec is write-once. Capture a new conversation "
                        "if the acceptance criteria changed."
                    )
                # Make sure the parent conversation row exists — the FK
                # constraint would otherwise reject the insert. (Auto-create
                # mirrors `create_conversation`'s "idempotent register" pattern
                # so a spec can be set at conversation creation time, before
                # the first event is appended.)
                self._conn.execute(
                    "INSERT OR IGNORE INTO conversations "
                    "(conversation_id, owner_id, created_at) "
                    "VALUES (?, ?, ?)",
                    (conversation_id, DEFAULT_OWNER_ID, datetime.now().isoformat()),
                )
                self._conn.execute(
                    "INSERT INTO dod_specs "
                    "(conversation_id, spec, set_at, set_by) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        conversation_id,
                        json.dumps(payload),
                        now,
                        set_by,
                    ),
                )
        return spec

    async def get_dod_spec(self, conversation_id: str) -> DoDSpec | None:
        """Accessor. Returns the stored `DoDSpec` or `None` when no spec has
        been captured yet. A pure read; no copy, no wrapping, no mutation.

        The returned spec is the live Pydantic model — frozen, so even an
        in-process attempt to mutate the result is a `ValidationError`."""
        from ..dod import DoDSpec
        row = self._conn.execute(
            "SELECT spec FROM dod_specs WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        if row is None:
            return None
        return DoDSpec.from_json_dict(json.loads(row["spec"]))

    async def replace_dod_spec(
        self, conversation_id: str, spec: DoDSpec, *, actor: str = "system"
    ) -> DoDSpec:
        """Write-once BOOTSTRAP + MONOTONIC replacement (v2). The spec can be
        EXTENDED (a mid-build steer that adds scope) but never WEAKENED — the
        monotonic guard (`is_monotonic_extension`) is enforced HERE, in the
        store, so no caller can route around it. The contract:

          * No spec exists → bootstrap (same as `set_dod_spec`).
          * Spec exists AND `new` is a monotonic extension (only adds / renames
            within, never drops a committed deliverable) → UPDATE in place.
          * Spec exists AND `new` would WEAKEN it → raise `DoDSpecAlreadySet`
            (original preserved). A revision can tighten its own acceptance bar,
            never relax it — that is the security property write-once protected,
            now preserved as monotonicity instead of pure immutability.

        `actor` is recorded as `set_by` on an accepted update (audit), but does
        NOT buy a weakening: even a human operator cannot drop a committed bar
        via this method (the user-facing relax path is a new conversation)."""
        from ..dod import DoDSpec, DoDSpecAlreadySet, is_monotonic_extension
        if not isinstance(spec, DoDSpec):
            raise TypeError(
                f"replace_dod_spec expects a DoDSpec, got {type(spec).__name__}"
            )
        payload = spec.to_json_dict()
        now = datetime.now(UTC).isoformat()
        async with self._write_lock:
            with self._conn:
                row = self._conn.execute(
                    "SELECT spec FROM dod_specs WHERE conversation_id = ?",
                    (conversation_id,),
                ).fetchone()
                if row is None:
                    # Bootstrap inside the same txn (mirrors set_dod_spec).
                    self._conn.execute(
                        "INSERT OR IGNORE INTO conversations "
                        "(conversation_id, owner_id, created_at) VALUES (?, ?, ?)",
                        (conversation_id, DEFAULT_OWNER_ID, datetime.now().isoformat()),
                    )
                    self._conn.execute(
                        "INSERT INTO dod_specs "
                        "(conversation_id, spec, set_at, set_by) VALUES (?, ?, ?, ?)",
                        (conversation_id, json.dumps(payload), now, actor),
                    )
                    return spec
                existing = DoDSpec.from_json_dict(json.loads(row[0]))
                if not is_monotonic_extension(existing, spec):
                    raise DoDSpecAlreadySet(
                        f"DoD spec for {conversation_id!r} cannot be replaced: the new "
                        "spec would WEAKEN it (a committed deliverable was dropped without "
                        "an explicit rename). The acceptance bar can only be EXTENDED."
                    )
                self._conn.execute(
                    "UPDATE dod_specs SET spec = ?, set_at = ?, set_by = ? "
                    "WHERE conversation_id = ?",
                    (json.dumps(payload), now, actor, conversation_id),
                )
        return spec

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

        # RP-01: write-through status update.
        if payload["kind"] == "status":
            self._conn.execute(
                "UPDATE conversations SET status = ? WHERE conversation_id = ?",
                (payload["status"], conversation_id),
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
            with self._conn:
                stored, is_new = self._store_one(conversation_id, event)
            if is_new:
                self._publish(conversation_id, stored)
        return stored

    async def append_many(self, conversation_id: str, events: list[Event]) -> list[Event]:
        stored_new: list[Event] = []
        results: list[Event] = []
        async with self._write_lock:
            with self._conn:
                for event in events:
                    stored, is_new = self._store_one(conversation_id, event)
                    results.append(stored)
                    if is_new:
                        stored_new.append(stored)
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
        self,
        *,
        owner_id: str,
        limit: int = 50,
        cursor: str | None = None,
        nonempty_only: bool = False,
        space_id: str | None = None,
    ) -> list[ConversationSummary]:
        """Owner-scoped library rows (id/title/created_at), newest first — what the
        History surface lists (§6.1). Same ownership filter as `list_conversations`;
        no cross-owner data, ever.

        BW-08: ``nonempty_only`` hides 0-event "ghost" conversations — rows that
        were pre-created but never actually used (no first user message, no title,
        nothing to show). The user-facing History/Projects listings pass it True
        (belt-and-suspenders to the lazy client pre-create); the activity feed and
        other internal readers leave it False so a freshly-kicked, mid-first-append
        running task is never dropped."""
        offset = int(cursor) if cursor else 0
        clauses = ["owner_id = ?"]
        params: list[str | int] = [owner_id]
        if space_id == "":
            clauses.append("space_id IS NULL")
        elif space_id is not None:
            clauses.append("space_id = ?")
            params.append(space_id)
        if nonempty_only:
            clauses.append(
                "EXISTS (SELECT 1 FROM events e WHERE e.conversation_id = c.conversation_id)"
            )
        where_clause = " AND ".join(clauses)
        params.extend([limit, offset])
        rows = self._conn.execute(
            "SELECT conversation_id, owner_id, space_id, title, created_at, status, surface, origin "
            "FROM conversations c "
            f"WHERE {where_clause} "
            "ORDER BY created_at DESC, conversation_id DESC LIMIT ? OFFSET ?",
            tuple(params),
        ).fetchall()
        summaries: list[ConversationSummary] = []
        repairs: list[tuple[str, str]] = []
        for r in rows:
            cid = r["conversation_id"]
            status = r["status"]
            if status is None:
                # RP-01: Read repair. Backfill the status column from the event log.
                state = await self.get_state(cid)
                status = state.execution_status.value
                repairs.append((status, cid))

            summaries.append(
                ConversationSummary(
                    conversation_id=cid,
                    owner_id=r["owner_id"],
                    space_id=r["space_id"],
                    title=r["title"],
                    created_at=r["created_at"],
                    status=status,
                    surface=r["surface"] or "research",  # null (pre-migration) → research
                    origin=r["origin"],  # None for first-party; "imported" for a bundle import
                )
            )

        if repairs:
            self._conn.executemany(
                "UPDATE conversations SET status = ? WHERE conversation_id = ?",
                repairs,
            )
            self._conn.commit()

        return summaries

    async def set_conversation_space(
        self, conversation_id: str, space_id: str | None
    ) -> None:
        """Move a conversation into a Space folder, or clear it to Unfiled."""
        clean_space_id = space_id.strip() if isinstance(space_id, str) else None
        async with self._write_lock:
            with self._conn:
                self._conn.execute(
                    "UPDATE conversations SET space_id = ? WHERE conversation_id = ?",
                    (clean_space_id or None, conversation_id),
                )

    async def clear_space_members(self, space_id: str) -> None:
        """Clear all conversations currently filed in a deleted Space."""
        async with self._write_lock:
            with self._conn:
                self._conn.execute(
                    "UPDATE conversations SET space_id = NULL WHERE space_id = ?",
                    (space_id,),
                )

    def conversation_origin(self, conversation_id: str) -> str | None:
        """The `origin` marker ("imported" or None) — the server-edge gate for
        read-only enforcement on imported conversations. Cheap sync lookup."""
        row = self._conn.execute(
            "SELECT origin FROM conversations WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        return row["origin"] if row is not None else None

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

    # ---- share tokens (RP-06) ----------------------------------------------

    def create_share_token(
        self,
        token: str,
        conversation_id: str,
        owner_id: str,
        *,
        bundle_seq: int,
    ) -> None:
        """Persist a new share token pointing at a conversation. The row
        records the conversation + the bundle's last_seq at export time. A
        second call for the SAME (token) is a no-op (`INSERT OR IGNORE`) —
        the token is the PRIMARY KEY and is server-generated + random; the
        idempotency is the cheap defense against a double-clicked "Share"
        button issuing a write twice."""
        self._conn.execute(
            "INSERT OR IGNORE INTO share_tokens "
            "(token, conversation_id, owner_id, created_at, bundle_seq) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                token,
                conversation_id,
                owner_id,
                datetime.now().isoformat(),
                int(bundle_seq),
            ),
        )
        self._conn.commit()

    def lookup_share_token(self, token: str) -> dict | None:
        """Fetch a share token row by its public id. Returns None when missing
        or revoked — the two cases are deliberately conflated in the public
        API: a revoked link looks IDENTICAL to a non-existent one to the
        viewer (404, not 410), so revocation cannot be probed to confirm a
        conversation exists."""
        row = self._conn.execute(
            "SELECT token, conversation_id, owner_id, created_at, bundle_seq "
            "FROM share_tokens WHERE token = ? AND revoked_at IS NULL",
            (token,),
        ).fetchone()
        if row is None:
            return None
        return {
            "token": row["token"],
            "conversation_id": row["conversation_id"],
            "owner_id": row["owner_id"],
            "created_at": row["created_at"],
            "bundle_seq": int(row["bundle_seq"]),
        }

    def list_share_tokens(self, *, owner_id: str) -> list[dict]:
        """List the active share tokens for one owner (the History
        "shared links" affordance). Revoked links are NOT returned by
        default; pass `include_revoked=True` for an audit view."""
        rows = self._conn.execute(
            "SELECT token, conversation_id, owner_id, created_at, bundle_seq "
            "FROM share_tokens WHERE owner_id = ? AND revoked_at IS NULL "
            "ORDER BY created_at DESC",
            (owner_id,),
        ).fetchall()
        return [
            {
                "token": r["token"],
                "conversation_id": r["conversation_id"],
                "owner_id": r["owner_id"],
                "created_at": r["created_at"],
                "bundle_seq": int(r["bundle_seq"]),
            }
            for r in rows
        ]

    def revoke_share_token(self, token: str, *, owner_id: str) -> bool:
        """Revoke a share token. OWNER-SCOPED — a caller can only revoke a
        token that was issued to it. Returns True if a row was marked
        revoked, False otherwise (not found, already revoked, or not owned)."""
        async_marker = datetime.now().isoformat()
        cur = self._conn.execute(
            "UPDATE share_tokens SET revoked_at = ? "
            "WHERE token = ? AND owner_id = ? AND revoked_at IS NULL",
            (async_marker, token, owner_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    # ---- schedules (RP-08) --------------------------------------------------

    def create_schedule(self, row: dict) -> None:
        """Persist a new schedule row. `row` must contain all required fields.
        Uses INSERT OR IGNORE so a double-create is a no-op."""
        self._conn.execute(
            "INSERT OR IGNORE INTO schedules "
            "(schedule_id, conversation_id, owner_id, rrule, description, "
            "depth, model_override, created_at, enabled, next_run) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["schedule_id"],
                row["conversation_id"],
                row["owner_id"],
                row["rrule"],
                row["description"],
                row.get("depth"),
                row.get("model_override"),
                row["created_at"],
                1 if row.get("enabled", True) else 0,
                row.get("next_run"),
            ),
        )
        self._conn.commit()

    def list_schedules(self, *, owner_id: str, conversation_id: str | None = None) -> list[dict]:
        """List schedules for an owner, optionally filtered by conversation."""
        if conversation_id is not None:
            rows = self._conn.execute(
                "SELECT * FROM schedules WHERE owner_id = ? AND conversation_id = ? "
                "ORDER BY created_at DESC",
                (owner_id, conversation_id),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM schedules WHERE owner_id = ? ORDER BY created_at DESC",
                (owner_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_enabled_schedules(self) -> list[dict]:
        """All enabled schedules across all owners — used by the background loop."""
        rows = self._conn.execute(
            "SELECT * FROM schedules WHERE enabled = 1"
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_schedule(self, schedule_id: str, *, owner_id: str) -> bool:
        """Delete a schedule. OWNER-SCOPED. Returns True if a row was removed."""
        cur = self._conn.execute(
            "DELETE FROM schedules WHERE schedule_id = ? AND owner_id = ?",
            (schedule_id, owner_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def update_schedule_next_run(self, schedule_id: str, next_run: str | None) -> None:
        """Update the next_run timestamp after a schedule fires."""
        self._conn.execute(
            "UPDATE schedules SET next_run = ? WHERE schedule_id = ?",
            (next_run, schedule_id),
        )
        self._conn.commit()

    def create_schedule_run(self, row: dict) -> None:
        """Record a completed schedule run in the audit log."""
        self._conn.execute(
            "INSERT OR IGNORE INTO schedule_runs "
            "(run_id, schedule_id, conversation_id, fired_at, coalesced) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                row["run_id"],
                row["schedule_id"],
                row["conversation_id"],
                row["fired_at"],
                1 if row.get("coalesced", False) else 0,
            ),
        )
        self._conn.commit()

    def list_schedule_runs(self, schedule_id: str) -> list[dict]:
        """List run history for a schedule."""
        rows = self._conn.execute(
            "SELECT * FROM schedule_runs WHERE schedule_id = ? ORDER BY fired_at DESC",
            (schedule_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_recent_schedule_runs(self, owner_id: str, limit: int = 50) -> list[dict]:
        """Owner-scoped recent scheduled-run history for the activity dashboard, newest
        first, joined with the schedule description + conversation title for display.
        Scoped by JOINing schedule_runs → schedules (which carries owner_id); a run
        whose schedule was deleted drops out (its history is gone with it, by design).
        Returns rows: run_id, schedule_id, conversation_id, fired_at, coalesced,
        description, title."""
        rows = self._conn.execute(
            "SELECT sr.run_id, sr.schedule_id, sr.conversation_id, sr.fired_at, "
            "       sr.coalesced, s.description, c.title "
            "FROM schedule_runs sr "
            "JOIN schedules s ON s.schedule_id = sr.schedule_id "
            "LEFT JOIN conversations c ON c.conversation_id = sr.conversation_id "
            "WHERE s.owner_id = ? "
            "ORDER BY sr.fired_at DESC LIMIT ?",
            (owner_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

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
