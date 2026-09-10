"""Conversation identity, listing, title, and space delegates for SqliteEventStore.

Extracted from ``SqliteEventStore`` so the store class stays within its
architecture budget. These private static mixins retain the exact public
behavior/signatures of the inherited methods; ``SqliteEventStore`` inherits
them and exposes the same surface.

Behavior preserved exactly:

* All methods use the store's single sqlite3 connection (passed via ``self``).
* Sync methods run directly against ``self._conn``.
* Async methods that mutate acquire ``self._write_lock`` + ``self._conn``
  transaction context, matching the pre-extraction shape.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING

from ..owners import DEFAULT_OWNER_ID
from ..state import ConversationState
from .base import ConversationSummary

if TYPE_CHECKING:
    import sqlite3


class _ConversationMixin:
    """Private static mixin: conversation identity, title, origin, delete.

    Inherited by ``SqliteEventStore``; not instantiated directly. All methods
    operate on ``self._conn`` and ``self._write_lock`` provided by the host
    class.
    """

    # Host-provided attributes (declared for type-checking; assigned by SqliteEventStore).
    _conn: sqlite3.Connection
    _write_lock: asyncio.Lock

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
    ) -> None:
        """Register a conversation with explicit ownership (§6.1). Idempotent.
        Auto-creation on first append uses DEFAULT_OWNER_ID; call this to set a
        real owner/space/title/surface up front (the §7.5 POST /conversations path).
        `surface` ("research"|"build"|"agent"|"deep_research") is persisted so History
        can route an item to the right surface. `origin="imported"` marks a bundle
        import (untrusted, read-only) — NULL for a normal first-party conversation.
        ``appkit_mode`` is immutable: idempotent re-registration never changes the
        value first captured for this conversation."""
        self._conn.execute(
            "INSERT OR IGNORE INTO conversations "
            "(conversation_id, owner_id, space_id, title, created_at, surface, origin, "
            "appkit_mode) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                conversation_id,
                owner_id,
                space_id,
                title,
                datetime.now().isoformat(),
                surface,
                origin,
                int(appkit_mode),
            ),
        )
        self._conn.commit()

    def conversation_appkit_mode_sync(self, conversation_id: str) -> bool | None:
        """Return immutable AppKit identity, or ``None`` for an unknown id."""

        row = self._conn.execute(
            "SELECT appkit_mode FROM conversations WHERE conversation_id = ? LIMIT 1",
            (conversation_id,),
        ).fetchone()
        return bool(row["appkit_mode"]) if row is not None else None

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

    async def update_title_unique(self, conversation_id: str, title: str) -> str:
        """Set the display title, appending ``" (2)"``, ``" (3)"``, ... when this
        owner already has a conversation with that exact title. Returns the title
        actually stored.

        The scope is the OWNER, which is the scope History/Projects list in
        (`list_conversation_summaries`) — the only place a collision is visible.
        Two builds of the same prompt produce the same summarized title (the
        summarizer runs at temperature 0), which left two History rows and two
        Projects cards distinguishable only by their file count.

        Race tolerance: the read of the taken names and the write of the winning
        one happen inside ONE `_write_lock` + transaction, so two conversations
        finishing their titling at the same moment are serialized and get
        different names. No-op returning `title` unchanged on an unknown id (as
        `update_title`, whose UPDATE would match zero rows)."""
        async with self._write_lock:
            with self._conn:
                row = self._conn.execute(
                    "SELECT owner_id FROM conversations WHERE conversation_id = ?",
                    (conversation_id,),
                ).fetchone()
                if row is None:
                    return title
                # LIKE wildcards inside a user/model-authored title are literal.
                escaped = title.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                taken = {
                    r["title"]
                    for r in self._conn.execute(
                        "SELECT title FROM conversations WHERE owner_id = ? "
                        "AND conversation_id != ? AND (title = ? OR title LIKE ? ESCAPE '\\')",
                        (row["owner_id"], conversation_id, title, f"{escaped} (%)"),
                    )
                }
                unique = title
                suffix = 1
                while unique in taken:
                    suffix += 1
                    unique = f"{title} ({suffix})"
                self._conn.execute(
                    "UPDATE conversations SET title = ? WHERE conversation_id = ?",
                    (unique, conversation_id),
                )
        return unique

    async def get_title(self, conversation_id: str) -> str | None:
        """The stored title (None if unset / unknown id). Cheap point-read used by
        the auto-title service to stay idempotent (skip already-titled conversations)."""
        cur = self._conn.execute(
            "SELECT title FROM conversations WHERE conversation_id = ?",
            (conversation_id,),
        )
        row = cur.fetchone()
        return row["title"] if row else None

    async def conversation_exists(self, conversation_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM conversations WHERE conversation_id = ? LIMIT 1",
            (conversation_id,),
        ).fetchone()
        return row is not None

    async def conversation_owner_id(self, conversation_id: str) -> str | None:
        return self.conversation_owner_id_sync(conversation_id)

    def conversation_owner_id_sync(self, conversation_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT owner_id FROM conversations WHERE conversation_id = ? LIMIT 1",
            (conversation_id,),
        ).fetchone()
        return row["owner_id"] if row is not None else None

    async def conversation_owned_by(self, conversation_id: str, owner_id: str) -> bool:
        return await self.conversation_owner_id(conversation_id) == owner_id

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
            self._conn.execute("DELETE FROM events WHERE conversation_id = ?", (conversation_id,))
            self._conn.commit()
        return True


class _ConversationListingMixin:
    """Private static mixin: conversation listing, summaries, and space.

    Inherited by ``SqliteEventStore``; not instantiated directly. All methods
    operate on ``self._conn`` and ``self._write_lock`` provided by the host
    class.
    """

    # Host-provided attributes (declared for type-checking; assigned by SqliteEventStore).
    _conn: sqlite3.Connection
    _write_lock: asyncio.Lock

    if TYPE_CHECKING:

        async def get_state(self, conversation_id: str) -> ConversationState:
            """Host-provided: reconstruct conversation state from the event log."""
            ...

    async def list_conversations(
        self, *, owner_id: str, limit: int = 50, cursor: str | None = None
    ) -> list[str]:
        # [INTERIOR] v1 cursor = opaque integer offset string. The ownership
        # filter is the essential part (§6.1): no cross-owner data, ever.
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
            "SELECT conversation_id, owner_id, space_id, title, created_at, status, "
            "surface, origin "
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

    async def set_conversation_space(self, conversation_id: str, space_id: str | None) -> None:
        """Move a conversation into a Space folder, or clear it to Unfiled."""
        clean_space_id = space_id.strip() if isinstance(space_id, str) else None
        async with self._write_lock:
            with self._conn:
                self._conn.execute(
                    "UPDATE conversations SET space_id = ? WHERE conversation_id = ?",
                    (clean_space_id or None, conversation_id),
                )

    async def clear_space_members(self, space_id: str, *, owner_id: str | None = None) -> None:
        sql = "UPDATE conversations SET space_id = NULL WHERE space_id = ?"
        params: tuple[str, ...] = (space_id,)
        if owner_id is not None:
            sql += " AND owner_id = ?"
            params = (space_id, owner_id)
        async with self._write_lock:
            with self._conn:
                self._conn.execute(sql, params)


__all__ = ["_ConversationListingMixin", "_ConversationMixin"]
