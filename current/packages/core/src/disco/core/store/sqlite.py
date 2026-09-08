"""SQLite EventStore — event-state-contract.md §6.2 (the v1 [INTERIOR] impl).

Dependency-free: stdlib `sqlite3`; one asyncio.Lock makes the read-max-then-insert
critical section atomic across awaits, which is what gives G1 (monotonic,
gap-free seq) even under concurrent `append()` coroutines. WAL mode gives G5
(reads don't block writes pathologically).

`subscribe()` is an in-process pub/sub (per-conversation asyncio.Queue fan-out):
on connect it drains history after `after_seq`, then streams live appends,
deduping the overlap by seq. This is the §7 reconnect/replay path; the WebSocket
endpoint (deferred to the server package) wraps it.

The store is split across private static mixins (conversation identity/listing,
DoD specs, share tokens, schedule delegates) to stay within the architecture
budget. The schema DDL/version authority lives in ``store/schema.py``.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections import defaultdict
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ..events import (
    Event,
    EventAdapter,
    WorkspaceVersionEvent,
    _require_concrete_event,
    derive_final_workspace_fence,
    event_to_json_dict,
)
from ..migration import migrate_event
from ..owners import DEFAULT_OWNER_ID, install_owner_id  # noqa: F401 - public re-export
from ..state import ConversationState
from .base import ConversationSummary, EventFilter, Page
from .preview_leases import LocalPreviewLease, LocalPreviewLeaseStore
from .preview_redemptions import PreviewRedemptionStore
from .schema import apply_schema
from .sqlite_conversations import _ConversationListingMixin, _ConversationMixin
from .sqlite_dod_specs import _DodSpecMixin
from .sqlite_schedules import ScheduleStore, _ScheduleMixin
from .sqlite_share_tokens import _ShareTokenMixin

if TYPE_CHECKING:
    from ..dod import DoDSpec

MAX_EVENT_PAYLOAD_BYTES = 1024 * 1024
EVENT_SUBSCRIBER_QUEUE_MAX = 256
EPHEMERAL_SUBSCRIBER_QUEUE_MAX = 128


class EventPayloadTooLarge(ValueError):
    """An event exceeded the durable payload ceiling and was not persisted."""


class SubscriberOverflow(RuntimeError):
    """A durable subscriber fell behind and must reconnect for replay."""


class _SubscriberOverflowMarker:
    pass


_SUBSCRIBER_OVERFLOW = _SubscriberOverflowMarker()


def _estimated_json_string_bytes(value: str) -> int:
    """Upper-bound one string under ``json.dumps(..., ensure_ascii=True)``.

    Treating an entire string as six bytes per character merely because it
    contains one Unicode code point makes long reports with a single typographic
    dash look several times larger than their real JSON representation.
    Scanning code points remains allocation-light while matching JSON escaping.
    """
    total = 2  # opening and closing quotes
    for character in value:
        codepoint = ord(character)
        if character in {'"', "\\"}:
            total += 2
        elif codepoint < 0x20:
            total += 6
        elif codepoint <= 0x7F:
            total += 1
        elif codepoint <= 0xFFFF:
            total += 6
        else:
            total += 12  # JSON encodes a non-BMP code point as two surrogates
    return total


def estimated_payload_bytes(payload: object) -> int:
    """The size `append` will measure for this payload — the producer's ruler.

    The append gate rejects on a deliberately conservative UPPER BOUND, not on
    the real JSON length (it charges a flat 24 bytes per scalar so it can bail
    out early without serializing). A producer that trims to fit the real length
    therefore has no idea how close it is to the cap: on an evidence-heavy
    Deep Research report — thousands of small rows, each with several numeric
    and null fields — the estimate runs ~8% above the real bytes, which is more
    than the report builder's headroom, and a report the builder considered
    fitted was rejected at the final append (live-caught 2026-09-02).

    Anything that sizes a payload before appending it must use this function, so
    producer and gate measure the same thing.
    """
    return _estimated_json_upper_bound(payload, stop_after=MAX_EVENT_PAYLOAD_BYTES)


def _estimated_json_upper_bound(value: object, *, stop_after: int) -> int:
    """Conservative, allocation-light JSON size estimate with early exit."""
    total = 0
    stack = [value]
    while stack and total <= stop_after:
        item = stack.pop()
        if item is None or isinstance(item, (bool, int, float)):
            total += 24
        elif isinstance(item, str):
            total += _estimated_json_string_bytes(item)
        elif isinstance(item, dict):
            total += 2 + len(item) * 2
            for key, child in item.items():
                stack.append(str(key))
                stack.append(child)
        elif isinstance(item, (list, tuple)):
            total += 2 + len(item)
            stack.extend(item)
        else:
            total += len(str(item)) * 6 + 2
    return total


def _row_to_event(payload: str) -> Event:
    """Deserialize a stored payload: JSON -> migrate -> validate (§4)."""
    return EventAdapter.validate_python(migrate_event(json.loads(payload)))


def _persist_one(store: SqliteEventStore, conversation_id: str, event: Event) -> tuple[Event, bool]:
    """Persist one event under the write lock. Returns (stored_event,
    is_new). Idempotent on (conversation_id, id) — G4.

    Module-level helper extracted from SqliteEventStore._store_one so the
    persistence logic is testable without a store instance. Must be called
    inside the store's write-lock + transaction context; does NOT commit or
    acquire a new lock."""
    existing = store._conn.execute(
        "SELECT payload FROM events WHERE conversation_id = ? AND id = ?",
        (conversation_id, event.id),
    ).fetchone()
    if existing is not None:
        return _row_to_event(existing["payload"]), False

    store._conn.execute(
        "INSERT OR IGNORE INTO conversations (conversation_id, owner_id, created_at) "
        "VALUES (?, ?, ?)",
        (conversation_id, DEFAULT_OWNER_ID, datetime.now().isoformat()),
    )

    row = store._conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM events WHERE conversation_id = ?",
        (conversation_id,),
    ).fetchone()
    next_seq = int(row["next"])

    stored = EventAdapter.validate_python(event.model_dump(mode="python") | {"seq": next_seq})
    if isinstance(stored, WorkspaceVersionEvent) and stored.final_seal is not None:
        seal = stored.final_seal
        scope = seal.scope
        if scope.namespace != "workspace.tree" or scope.identifier != conversation_id:
            raise ValueError("final workspace seal scope must match the persisted conversation")
        terminal_seq, latest_effect_seq = derive_final_workspace_fence(
            store._query(conversation_id, None)
        )
        if seal.terminal_seq != terminal_seq:
            raise ValueError("final workspace seal terminal sequence does not match event log")
        if seal.latest_effect_seq != latest_effect_seq:
            raise ValueError("final workspace seal latest effect sequence does not match event log")
    payload = event_to_json_dict(stored)
    estimate = _estimated_json_upper_bound(payload, stop_after=MAX_EVENT_PAYLOAD_BYTES)
    if estimate > MAX_EVENT_PAYLOAD_BYTES:
        raise EventPayloadTooLarge(
            f"event {event.id} exceeds the {MAX_EVENT_PAYLOAD_BYTES}-byte payload cap"
        )
    payload_json = json.dumps(payload, separators=(",", ":"))
    if len(payload_json.encode("utf-8")) > MAX_EVENT_PAYLOAD_BYTES:
        raise EventPayloadTooLarge(
            f"event {event.id} exceeds the {MAX_EVENT_PAYLOAD_BYTES}-byte payload cap"
        )
    store._conn.execute(
        "INSERT INTO events (conversation_id, seq, id, kind, source, created_at, payload) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            conversation_id,
            next_seq,
            stored.id,
            payload["kind"],
            payload["source"],
            payload["timestamp"],
            payload_json,
        ),
    )

    operation = str(payload.get("operation", ""))
    semantic_status_changed = payload["kind"] in {"status", "error"} or (
        payload["kind"] == "workspace_mutation"
        and payload.get("run_protocol_version") == 1
        and (operation.startswith("agent.run-intent.") or operation == "agent.view-admitted")
    )
    if semantic_status_changed:
        reconstructed = ConversationState.reconstruct(
            conversation_id,
            store._query(conversation_id, None),
        )
        store._conn.execute(
            "UPDATE conversations SET status = ? WHERE conversation_id = ?",
            (reconstructed.execution_status.value, conversation_id),
        )

    return stored, True


class _ClosableSqliteStore(
    _ConversationMixin,
    _ConversationListingMixin,
    _DodSpecMixin,
    _ShareTokenMixin,
    _ScheduleMixin,
):
    """Idempotent connection ownership shared by the concrete event store."""

    _closed: bool
    _conn: sqlite3.Connection
    _preview_redemptions: PreviewRedemptionStore
    _local_preview_leases: LocalPreviewLeaseStore

    def register_preview_intent(self, jti: str, expires_at: int) -> None:
        self._preview_redemptions.register(jti, expires_at)

    def consume_preview_intent(self, jti: str, *, now: int) -> bool:
        return self._preview_redemptions.consume(jti, now=now)

    def acquire_local_preview_lease(
        self,
        *,
        conversation_id: str,
        owner_id: str,
        target_port: int,
        authority_id: str,
        now: int,
        expires_at: int,
        listener_ports: tuple[int, ...],
    ) -> LocalPreviewLease | None:
        return self._local_preview_leases.acquire(
            conversation_id=conversation_id,
            owner_id=owner_id,
            target_port=target_port,
            authority_id=authority_id,
            now=now,
            expires_at=expires_at,
            listener_ports=listener_ports,
        )

    def resolve_local_preview_lease(
        self, listener_port: int, *, now: int
    ) -> LocalPreviewLease | None:
        return self._local_preview_leases.resolve(listener_port, now=now)

    def release_local_preview_lease(self, *, conversation_id: str, owner_id: str) -> bool:
        return self._local_preview_leases.release(
            conversation_id=conversation_id,
            owner_id=owner_id,
        )

    def complete_local_preview_storage_reset(
        self, listener_port: int, *, authority_id: str, now: int
    ) -> bool:
        return self._local_preview_leases.complete_storage_reset(
            listener_port, authority_id=authority_id, now=now
        )

    def close(self) -> None:
        """Release the connection; safe to call more than once."""
        if self._closed:
            return
        self._conn.close()
        self._closed = True

    def __del__(self) -> None:
        """Defensively release a connection whose owner omitted ``close``."""
        if getattr(self, "_closed", True):
            return
        try:
            self.close()
        except sqlite3.Error:
            # Destructors cannot safely surface cleanup exceptions. Normal
            # ownership paths remain explicit and observable through close().
            pass


class SqliteEventStore(_ClosableSqliteStore):
    """An `EventStore` (store/base.py) backed by a single SQLite file.

    Pass a filesystem path for durability across reopens (the G2 restart path),
    or ":memory:" for ephemeral use (tests that don't reopen).

    The store is split across private static mixins (conversation identity/
    listing, DoD specs, share tokens, schedule delegates) to stay within the
    architecture budget. The schema DDL/version authority lives in
    ``store/schema.py``.
    """

    if TYPE_CHECKING:

        def create_conversation(
            self,
            conversation_id: str,
            *,
            owner_id: str = DEFAULT_OWNER_ID,
            space_id: str | None = None,
            title: str | None = None,
            surface: str | None = None,
            origin: str | None = None,
            appkit_mode: bool = False,
        ) -> None: ...

        def conversation_appkit_mode_sync(self, conversation_id: str) -> bool | None: ...

        async def update_title(self, conversation_id: str, title: str) -> None: ...

        async def get_title(self, conversation_id: str) -> str | None: ...

        async def set_dod_spec(
            self,
            conversation_id: str,
            spec: DoDSpec,
            *,
            set_by: str = "system",
        ) -> DoDSpec: ...

        async def get_dod_spec(self, conversation_id: str) -> DoDSpec | None: ...

        async def get_external_dod_spec(self, conversation_id: str) -> DoDSpec | None: ...

        async def replace_dod_spec(
            self,
            conversation_id: str,
            spec: DoDSpec,
            *,
            actor: str = "system",
        ) -> DoDSpec: ...

        async def conversation_exists(self, conversation_id: str) -> bool: ...

        async def conversation_owner_id(self, conversation_id: str) -> str | None: ...

        def conversation_owner_id_sync(self, conversation_id: str) -> str | None: ...

        async def conversation_owned_by(self, conversation_id: str, owner_id: str) -> bool: ...

        async def list_conversations(
            self,
            *,
            owner_id: str,
            limit: int = 50,
            cursor: str | None = None,
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

        async def clear_space_members(
            self, space_id: str, *, owner_id: str | None = None
        ) -> None: ...

        def conversation_origin(self, conversation_id: str) -> str | None: ...

        async def delete_conversation(self, conversation_id: str, *, owner_id: str) -> bool: ...

        def create_share_token(
            self,
            token: str,
            conversation_id: str,
            owner_id: str,
            *,
            bundle_seq: int,
            bundle_title: str | None,
            bundle_surface: str,
        ) -> None: ...

        def lookup_share_token(self, token: str) -> dict | None: ...

        def list_share_tokens(self, *, owner_id: str) -> list[dict]: ...

        def revoke_share_token(self, token: str, *, owner_id: str) -> bool: ...

        def create_schedule(self, row: dict) -> None: ...

        def list_schedules(
            self, *, owner_id: str, conversation_id: str | None = None
        ) -> list[dict]: ...

        def list_enabled_schedules(self) -> list[dict]: ...

        def delete_schedule(self, schedule_id: str, *, owner_id: str) -> bool: ...

        def update_schedule_next_run(self, schedule_id: str, next_run: str | None) -> None: ...

        def create_schedule_run(self, row: dict) -> None: ...

        def list_schedule_runs(self, schedule_id: str) -> list[dict]: ...

        def list_recent_schedule_runs(self, owner_id: str, limit: int = 50) -> list[dict]: ...

    def __init__(self, path: str | Path = ":memory:") -> None:
        # Retained so sidecar persistence (runtime B0 overrides/surfaces/autonomous)
        # can anchor next to the REAL event DB instead of re-deriving from env —
        # the two diverging silently disabled every sidecar on deployments that
        # set the DB path only at store construction (live-caught 2026-07-03).
        self.db_path: str = "" if str(path) == ":memory:" else str(path)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._closed = False
        self._conn.row_factory = sqlite3.Row
        try:
            apply_schema(self._conn)
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=FULL;")  # durable on commit (G2)
        except BaseException:
            self._conn.close()
            self._closed = True
            raise
        self._schedules = ScheduleStore(self._conn)
        self._write_lock = asyncio.Lock()
        self._preview_redemptions = PreviewRedemptionStore(self._conn)
        self._local_preview_leases = LocalPreviewLeaseStore(self._conn)
        # Reclaim capacity even when a database sits idle after leases expire.
        # Browser-origin authority rows intentionally survive as reset fences.
        self._local_preview_leases.purge_expired(now=int(time.time()))
        # conversation_id -> set of live subscriber queues.
        self._subscribers: dict[str, set[asyncio.Queue[Event | _SubscriberOverflowMarker]]] = (
            defaultdict(set)
        )
        # conversation_id -> set of EPHEMERAL subscriber queues. These carry
        # transient frames (watch-it-write file-stream deltas) that are broadcast
        # to live listeners but NEVER persisted — they're display-only and would
        # bloat the event log (hundreds per file). No history, live subscribers
        # only; a late joiner simply misses in-flight deltas and gets the final
        # persisted ActionEvent instead.
        self._eph_subscribers: dict[str, set[asyncio.Queue[dict]]] = defaultdict(set)

    # ---- writes --------------------------------------------------------------

    def _store_one(self, conversation_id: str, event: Event) -> tuple[Event, bool]:
        """Persist one event under the write lock — delegates to _persist_one."""
        return _persist_one(self, conversation_id, event)

    def _publish(self, conversation_id: str, event: Event) -> None:
        subscribers = self._subscribers.get(conversation_id, set())
        for q in list(subscribers):
            if q.full():
                subscribers.discard(q)
                while not q.empty():
                    q.get_nowait()
                q.put_nowait(_SUBSCRIBER_OVERFLOW)
                continue
            q.put_nowait(event)

    def publish_ephemeral(self, conversation_id: str, frame: dict) -> None:
        """Broadcast a transient (non-persisted) frame to live subscribers. Used
        for watch-it-write file-stream deltas. Fire-and-forget; if there is no
        listener the frame is simply dropped (the final ActionEvent is the durable
        record). Sync + non-blocking so the agent loop can call it inline."""
        for q in self._eph_subscribers.get(conversation_id, set()):
            if q.full():
                q.get_nowait()
            q.put_nowait(frame)

    async def subscribe_ephemeral(self, conversation_id: str) -> AsyncIterator[dict]:
        """Live-only stream of transient frames for a conversation (no history)."""
        return self._subscribe_ephemeral(conversation_id)

    async def _subscribe_ephemeral(self, conversation_id: str) -> AsyncIterator[dict]:
        queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=EPHEMERAL_SUBSCRIBER_QUEUE_MAX)
        self._eph_subscribers[conversation_id].add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            self._eph_subscribers[conversation_id].discard(queue)

    async def append(self, conversation_id: str, event: Event) -> Event:
        event = _require_concrete_event(event)
        async with self._write_lock:
            with self._conn:
                stored, is_new = self._store_one(conversation_id, event)
            if is_new:
                self._publish(conversation_id, stored)
        return stored

    async def append_many(self, conversation_id: str, events: list[Event]) -> list[Event]:
        events = [_require_concrete_event(event) for event in events]
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
        queue: asyncio.Queue[Event | _SubscriberOverflowMarker] = asyncio.Queue(
            maxsize=EVENT_SUBSCRIBER_QUEUE_MAX
        )
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
                item = await queue.get()
                if isinstance(item, _SubscriberOverflowMarker):
                    raise SubscriberOverflow(
                        "durable event subscriber exceeded its bounded queue; reconnect for replay"
                    )
                ev = item
                if ev.seq is not None and ev.seq <= last_seq:
                    continue  # already delivered from history (overlap dedup)
                if ev.seq is not None:
                    last_seq = ev.seq
                yield ev
        finally:
            self._subscribers[conversation_id].discard(queue)
