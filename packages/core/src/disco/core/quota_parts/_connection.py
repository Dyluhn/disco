"""SQLite connection lifecycle for the quota store.

Extracted from :class:`~disco.core.quota.SqliteQuotaStore` as a cohesive
collaborator: this module owns the one real ``sqlite3.Connection``, the
``threading.RLock`` that serializes writers across threads, the schema
bootstrap, and the two lock disciplines every other quota collaborator reads
or writes through — a lock-only ``read_lock`` for plain lookups and a
``BEGIN IMMEDIATE`` ``write_transaction`` for anything that mutates state.
Both ``_config_store`` and ``_reservation_ledger`` are handed the same
instance so every writer in one process agrees on what "the connection" is.

The schema, table names, and column names below are the durable on-disk
contract described in ``quota.py``'s module docstring; nothing here may
change them without a migration.
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS quota_configs (
    owner_id          TEXT NOT NULL,
    audience          TEXT NOT NULL,
    service           TEXT NOT NULL,
    window_seconds    INTEGER NOT NULL CHECK (window_seconds BETWEEN 1 AND 86400),
    max_requests      INTEGER,
    max_input_tokens  INTEGER,
    max_output_tokens INTEGER,
    max_total_tokens  INTEGER,
    updated_at_us     INTEGER NOT NULL,
    PRIMARY KEY (owner_id, audience, service),
    CHECK (max_requests IS NOT NULL OR max_input_tokens IS NOT NULL
           OR max_output_tokens IS NOT NULL OR max_total_tokens IS NOT NULL)
);
CREATE TABLE IF NOT EXISTS quota_reservations (
    owner_id               TEXT NOT NULL,
    audience               TEXT NOT NULL,
    service                TEXT NOT NULL,
    reservation_id         TEXT NOT NULL,
    state                  TEXT NOT NULL
                           CHECK (state IN
                               ('reserved', 'dispatched', 'completed', 'abandoned', 'released')),
    estimated_input_tokens INTEGER NOT NULL CHECK (estimated_input_tokens >= 0),
    estimated_output_tokens INTEGER NOT NULL CHECK (estimated_output_tokens >= 0),
    actual_input_tokens    INTEGER,
    actual_output_tokens   INTEGER,
    created_at_us          INTEGER NOT NULL,
    completed_at_us        INTEGER,
    released_at_us         INTEGER,
    PRIMARY KEY (owner_id, audience, reservation_id)
);
CREATE INDEX IF NOT EXISTS idx_quota_reservations_app_window
    ON quota_reservations (owner_id, audience, created_at_us);
CREATE INDEX IF NOT EXISTS idx_quota_reservations_service_window
    ON quota_reservations (owner_id, audience, service, created_at_us);
"""


class _QuotaConnection:
    """Owns the quota store's single SQLite connection and its lock discipline."""

    def __init__(self, path: str | Path) -> None:
        resolved = Path(path)
        self.db_path = str(resolved)
        if self.db_path != ":memory:":
            resolved.parent.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                resolved.parent.chmod(0o700)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            timeout=5.0,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout = 5000")
        if self.db_path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
            with contextlib.suppress(OSError):
                resolved.chmod(0o600)
        with self.write_transaction() as conn:
            conn.executescript(_SCHEMA)

    def check_open(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("quota store is closed")
        return self._conn

    @contextlib.contextmanager
    def read_lock(self) -> Iterator[sqlite3.Connection]:
        """Hold the serializing lock for a plain read; no transaction is opened."""
        with self._lock:
            yield self.check_open()

    @contextlib.contextmanager
    def write_transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self.check_open()
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.rollback()
                raise
            else:
                conn.commit()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
