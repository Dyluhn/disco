"""Durable leases for DNS-free per-conversation Preview origins."""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class LocalPreviewLease:
    conversation_id: str
    owner_id: str
    listener_port: int
    target_port: int
    authority_id: str
    expires_at: int
    storage_reset_required: bool = False


class LocalPreviewLeaseStore:
    """Atomically allocate a bounded listener origin to one conversation.

    The row is durable so a restarted agent-server reconstructs the same origin.
    Expired rows are reclaimable; live rows are never shared between conversations.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._lock = threading.Lock()

    @staticmethod
    def _row(
        row: tuple[object, ...] | None,
        *,
        storage_authority_id: str | None = None,
    ) -> LocalPreviewLease | None:
        if row is None or len(row) < 6:
            return None
        conversation_id, owner_id, listener_port, target_port, authority_id, expires_at = row[:6]
        if (
            not isinstance(conversation_id, str)
            or not isinstance(owner_id, str)
            or type(listener_port) is not int
            or type(target_port) is not int
            or not isinstance(authority_id, str)
            or type(expires_at) is not int
        ):
            return None
        return LocalPreviewLease(
            conversation_id=conversation_id,
            owner_id=owner_id,
            listener_port=listener_port,
            target_port=target_port,
            authority_id=authority_id,
            expires_at=expires_at,
            storage_reset_required=storage_authority_id != authority_id,
        )

    def _storage_authority(self, listener_port: int) -> str | None:
        row = self._connection.execute(
            "SELECT authority_id FROM local_preview_origin_state WHERE listener_port = ?",
            (int(listener_port),),
        ).fetchone()
        return str(row[0]) if row is not None and isinstance(row[0], str) else None

    def _validate_acquire_request(
        self,
        *,
        conversation_id: str,
        owner_id: str,
        target_port: int,
        authority_id: str,
        now: int,
        expires_at: int,
        listener_ports: tuple[int, ...],
    ) -> None:
        if (
            not conversation_id
            or not owner_id
            or not 1 <= target_port <= 65535
            or not authority_id
            or len(authority_id) > 256
            or expires_at <= now
            or not listener_ports
            or len(listener_ports) != len(set(listener_ports))
        ):
            raise ValueError("invalid local preview lease request")

    def _read_existing_lease(self, conversation_id: str) -> LocalPreviewLease | None:
        return self._row(
            self._connection.execute(
                "SELECT conversation_id, owner_id, listener_port, target_port, "
                "authority_id, expires_at "
                "FROM local_preview_leases WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        )

    def _refresh_same_authority_lease(
        self,
        *,
        conversation_id: str,
        owner_id: str,
        target_port: int,
        authority_id: str,
        expires_at: int,
        existing: LocalPreviewLease,
    ) -> LocalPreviewLease:
        self._connection.execute(
            "UPDATE local_preview_leases SET target_port = ?, expires_at = ? "
            "WHERE conversation_id = ?",
            (int(target_port), int(expires_at), conversation_id),
        )
        return LocalPreviewLease(
            conversation_id=conversation_id,
            owner_id=owner_id,
            listener_port=existing.listener_port,
            target_port=target_port,
            authority_id=authority_id,
            expires_at=expires_at,
            storage_reset_required=(
                self._storage_authority(existing.listener_port) != authority_id
            ),
        )

    def _select_listener_port(
        self,
        *,
        conversation_id: str,
        now: int,
        listener_ports: tuple[int, ...],
        excluded: int | None,
    ) -> int | None:
        """Deterministic port selection over the occupied set."""
        occupied = {
            int(row[0])
            for row in self._connection.execute(
                "SELECT listener_port FROM local_preview_leases WHERE expires_at >= ?",
                (int(now),),
            ).fetchall()
        }
        digest = hashlib.sha256(conversation_id.encode()).digest()
        offset = int.from_bytes(digest[:4], "big") % len(listener_ports)
        for index in range(len(listener_ports)):
            listener_port = listener_ports[(offset + index) % len(listener_ports)]
            if listener_port in occupied or listener_port == excluded:
                continue
            return listener_port
        return None

    def _allocate_new_lease(
        self,
        *,
        conversation_id: str,
        owner_id: str,
        target_port: int,
        authority_id: str,
        expires_at: int,
        listener_port: int,
        existing: LocalPreviewLease | None,
    ) -> LocalPreviewLease:
        if existing is None:
            self._connection.execute(
                "INSERT INTO local_preview_leases "
                "(conversation_id, owner_id, listener_port, target_port, "
                "authority_id, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    conversation_id,
                    owner_id,
                    int(listener_port),
                    int(target_port),
                    authority_id,
                    int(expires_at),
                ),
            )
        else:
            self._connection.execute(
                "UPDATE local_preview_leases SET listener_port = ?, target_port = ?, "
                "authority_id = ?, expires_at = ? WHERE conversation_id = ?",
                (
                    int(listener_port),
                    int(target_port),
                    authority_id,
                    int(expires_at),
                    conversation_id,
                ),
            )
        return LocalPreviewLease(
            conversation_id=conversation_id,
            owner_id=owner_id,
            listener_port=listener_port,
            target_port=target_port,
            authority_id=authority_id,
            expires_at=expires_at,
            storage_reset_required=(self._storage_authority(listener_port) != authority_id),
        )

    def acquire(
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
        self._validate_acquire_request(
            conversation_id=conversation_id,
            owner_id=owner_id,
            target_port=target_port,
            authority_id=authority_id,
            now=now,
            expires_at=expires_at,
            listener_ports=listener_ports,
        )

        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM local_preview_leases WHERE expires_at < ?",
                (int(now),),
            )
            existing = self._read_existing_lease(conversation_id)
            if existing is not None:
                # Ownership is immutable. A mismatched caller cannot retarget an
                # origin that was leased by another owner.
                if existing.owner_id != owner_id:
                    return None
                if existing.authority_id == authority_id:
                    return self._refresh_same_authority_lease(
                        conversation_id=conversation_id,
                        owner_id=owner_id,
                        target_port=target_port,
                        authority_id=authority_id,
                        expires_at=expires_at,
                        existing=existing,
                    )

            # A new runtime/immutable authority gets a new browser origin. The
            # old listener is deliberately excluded even though its row will be
            # updated below: a capability for generation N must not become a
            # capability for generation N+1 on the same 127.0.0.1 origin.
            excluded = existing.listener_port if existing is not None else None
            listener_port = self._select_listener_port(
                conversation_id=conversation_id,
                now=now,
                listener_ports=listener_ports,
                excluded=excluded,
            )
            if listener_port is None:
                return None
            return self._allocate_new_lease(
                conversation_id=conversation_id,
                owner_id=owner_id,
                target_port=target_port,
                authority_id=authority_id,
                expires_at=expires_at,
                listener_port=listener_port,
                existing=existing,
            )

    def resolve(self, listener_port: int, *, now: int) -> LocalPreviewLease | None:
        if not 1 <= listener_port <= 65535:
            return None
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM local_preview_leases WHERE expires_at < ?",
                (int(now),),
            )
            row = self._connection.execute(
                "SELECT conversation_id, owner_id, listener_port, target_port, "
                "authority_id, expires_at "
                "FROM local_preview_leases WHERE listener_port = ? AND expires_at >= ?",
                (int(listener_port), int(now)),
            ).fetchone()
            return self._row(
                row,
                storage_authority_id=self._storage_authority(listener_port),
            )

    def purge_expired(self, *, now: int) -> int:
        """Drop idle expired capacity rows while retaining origin reset fences."""

        with self._lock, self._connection:
            deleted = self._connection.execute(
                "DELETE FROM local_preview_leases WHERE expires_at < ?",
                (int(now),),
            )
            return max(0, deleted.rowcount)

    def release(self, *, conversation_id: str, owner_id: str) -> bool:
        """Release one explicitly torn-down conversation's listener origin.

        The retained ``local_preview_origin_state`` row is intentionally not
        removed. A later authority may reuse the listener only through the
        existing browser-storage reset fence, so prompt capacity recovery does
        not weaken generated-application cookie or storage isolation.
        """

        if not conversation_id or not owner_id:
            return False
        with self._lock, self._connection:
            deleted = self._connection.execute(
                "DELETE FROM local_preview_leases WHERE conversation_id = ? AND owner_id = ?",
                (conversation_id, owner_id),
            )
            return deleted.rowcount == 1

    def complete_storage_reset(
        self,
        listener_port: int,
        *,
        authority_id: str,
        now: int,
    ) -> bool:
        """Record a browser-confirmed reset only for the current leased authority."""

        if not 1 <= listener_port <= 65535 or not authority_id:
            return False
        with self._lock, self._connection:
            current = self._connection.execute(
                "SELECT authority_id FROM local_preview_leases "
                "WHERE listener_port = ? AND expires_at >= ?",
                (int(listener_port), int(now)),
            ).fetchone()
            if current is None or current[0] != authority_id:
                return False
            self._connection.execute(
                "INSERT INTO local_preview_origin_state (listener_port, authority_id) "
                "VALUES (?, ?) ON CONFLICT(listener_port) DO UPDATE SET "
                "authority_id = excluded.authority_id",
                (int(listener_port), authority_id),
            )
            return True


__all__ = ["LocalPreviewLease", "LocalPreviewLeaseStore"]
