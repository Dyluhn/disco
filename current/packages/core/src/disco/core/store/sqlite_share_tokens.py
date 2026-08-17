"""Share token storage delegate for SqliteEventStore.

Extracted from ``SqliteEventStore`` so the store class stays within its
architecture budget. This private static mixin retains the exact public
behavior/signatures of the inherited share token methods; ``SqliteEventStore``
inherits them and exposes the same surface.

Behavior preserved exactly:

* All methods use the store's single sqlite3 connection (passed via ``self``).
* Sync methods run directly against ``self._conn`` and commit immediately,
  matching the pre-extraction shape.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3


class _ShareTokenMixin:
    """Private static mixin: share token persistence (RP-06).

    Inherited by ``SqliteEventStore``; not instantiated directly. All methods
    operate on ``self._conn`` provided by the host class.
    """

    # Host-provided attribute (declared for type-checking; assigned by SqliteEventStore).
    _conn: sqlite3.Connection

    def create_share_token(
        self,
        token: str,
        conversation_id: str,
        owner_id: str,
        *,
        bundle_seq: int,
        bundle_title: str | None,
        bundle_surface: str,
    ) -> None:
        """Persist a new share token pointing at a conversation. The row
        records the conversation + the bundle's last_seq at export time. A
        second call for the SAME (token) is a no-op (`INSERT OR IGNORE`) —
        the token is the PRIMARY KEY and is server-generated + random; the
        idempotency is the cheap defense against a double-clicked "Share"
        button issuing a write twice."""
        self._conn.execute(
            "INSERT OR IGNORE INTO share_tokens "
            "(token, conversation_id, owner_id, created_at, bundle_seq, "
            "bundle_title, bundle_surface) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                token,
                conversation_id,
                owner_id,
                datetime.now().isoformat(),
                int(bundle_seq),
                bundle_title,
                bundle_surface,
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
            "SELECT token, conversation_id, owner_id, created_at, bundle_seq, "
            "bundle_title, bundle_surface "
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
            "bundle_title": row["bundle_title"],
            "bundle_surface": row["bundle_surface"] or "research",
        }

    def list_share_tokens(self, *, owner_id: str) -> list[dict]:
        """List the active share tokens for one owner (the History
        "shared links" affordance). Revoked links are NOT returned by
        default; pass `include_revoked=True` for an audit view."""
        rows = self._conn.execute(
            "SELECT token, conversation_id, owner_id, created_at, bundle_seq, "
            "bundle_title, bundle_surface "
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
                "bundle_title": r["bundle_title"],
                "bundle_surface": r["bundle_surface"] or "research",
            }
            for r in rows
        ]

    def revoke_share_token(self, token: str, *, owner_id: str) -> bool:
        """Revoke an owner-scoped token, returning whether an active row changed."""
        async_marker = datetime.now().isoformat()
        cur = self._conn.execute(
            "UPDATE share_tokens SET revoked_at = ? "
            "WHERE token = ? AND owner_id = ? AND revoked_at IS NULL",
            (async_marker, token, owner_id),
        )
        self._conn.commit()
        return cur.rowcount > 0


__all__ = ["_ShareTokenMixin"]
