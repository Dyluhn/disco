"""Durable, one-use preview intent redemption persistence."""

from __future__ import annotations

import sqlite3
import threading
import time


class PreviewRedemptionStore:
    """Atomic replay fence for replicas sharing one SQLite database.

    The process-local lock prevents concurrent use of one connection. SQLite's
    conditional UPDATE supplies the cross-connection/process winner when every
    replica points at the same deployment database. Separate databases remain
    separate authorization failure domains and must not share signed intents.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._lock = threading.Lock()

    def register(self, jti: str, expires_at: int) -> None:
        """Register a freshly signed intent before returning it to the caller."""

        if not jti or expires_at <= 0:
            raise ValueError("preview intent requires a JTI and expiry")
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM preview_redemptions WHERE expires_at < ?",
                (int(time.time()),),
            )
            self._connection.execute(
                "INSERT INTO preview_redemptions (jti, expires_at, consumed_at) "
                "VALUES (?, ?, NULL)",
                (jti, int(expires_at)),
            )

    def consume(self, jti: str, *, now: int) -> bool:
        """Atomically win one redemption across connections sharing the DB."""

        if not jti:
            return False
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE preview_redemptions SET consumed_at = ? "
                "WHERE jti = ? AND consumed_at IS NULL AND expires_at >= ?",
                (int(now), jti, int(now)),
            )
            self._connection.execute(
                "DELETE FROM preview_redemptions WHERE expires_at < ?",
                (int(now),),
            )
        return cursor.rowcount == 1
