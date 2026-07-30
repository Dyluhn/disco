"""Durable per-app request and token quota accounting.

This module is intentionally server-agnostic.  Both server siblings can open
the same SQLite database and use :class:`SqliteQuotaStore`; admission is
serialized with ``BEGIN IMMEDIATE`` so the check and reservation are one
cross-process transaction.

Quota identity is always ``owner_id + audience``.  ``audience`` is the app
identity carried by the host-service credential.  Every request consumes the
app aggregate quota and, when configured, an additional exact-service quota.
There is no prefix or wildcard matching.
"""

from __future__ import annotations

import contextlib
import math
import sqlite3
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from ._quota_contracts import DEFAULT_QUOTA_CONFIG as DEFAULT_QUOTA_CONFIG
from ._quota_contracts import (
    DEFAULT_RESERVATION_TTL_SECONDS as DEFAULT_RESERVATION_TTL_SECONDS,
)
from ._quota_contracts import MAX_REQUEST_LIMIT as MAX_REQUEST_LIMIT
from ._quota_contracts import (
    MAX_RESERVATION_TTL_SECONDS as MAX_RESERVATION_TTL_SECONDS,
)
from ._quota_contracts import MAX_TOKEN_LIMIT as MAX_TOKEN_LIMIT
from ._quota_contracts import MAX_WINDOW_SECONDS as MAX_WINDOW_SECONDS
from ._quota_contracts import QuotaAdmission as QuotaAdmission
from ._quota_contracts import QuotaConfig as QuotaConfig
from ._quota_contracts import QuotaConfigurationError as QuotaConfigurationError
from ._quota_contracts import QuotaError as QuotaError
from ._quota_contracts import QuotaReservation as QuotaReservation
from ._quota_contracts import QuotaStore as QuotaStore
from ._quota_contracts import QuotaUsage as QuotaUsage
from ._quota_contracts import ReservationConflict as ReservationConflict
from ._quota_contracts import ReservationNotFound as ReservationNotFound
from ._quota_contracts import ReservationStateError as ReservationStateError
from ._quota_contracts import StoredQuotaConfig as StoredQuotaConfig
from ._quota_contracts import (
    _bounded_integer,
    _epoch_us,
    _identity,
    _instant,
    _service,
    _token_count,
)
from ._quota_sql import (
    config_from_row,
    exceeded_reason,
    find_limits,
    find_reservation,
    idempotent_admission,
    sweep_stale,
    usage,
)

# Preserve the historical public type identity for introspection and pickling.
for _public_contract in (
    QuotaError,
    QuotaConfigurationError,
    ReservationNotFound,
    ReservationConflict,
    ReservationStateError,
    QuotaConfig,
    StoredQuotaConfig,
    QuotaUsage,
    QuotaReservation,
    QuotaAdmission,
    QuotaStore,
):
    _public_contract.__module__ = __name__
del _public_contract

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


class SqliteQuotaStore:
    """SQLite quota configuration, reservations, and usage accounting."""

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        default_config: QuotaConfig = DEFAULT_QUOTA_CONFIG,
        reservation_ttl_seconds: int = DEFAULT_RESERVATION_TTL_SECONDS,
    ) -> None:
        if not isinstance(default_config, QuotaConfig):
            raise QuotaConfigurationError("default_config must be a QuotaConfig")
        resolved = Path(path)
        self.db_path = str(resolved)
        self.default_config = default_config
        _bounded_integer(
            "reservation_ttl_seconds",
            reservation_ttl_seconds,
            MAX_RESERVATION_TTL_SECONDS,
        )
        self.reservation_ttl_seconds = reservation_ttl_seconds
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
        with self._write_transaction() as conn:
            conn.executescript(_SCHEMA)

    def configure(
        self,
        *,
        owner_id: str,
        audience: str,
        limits: QuotaConfig,
        service: str | None = None,
        now: datetime | None = None,
    ) -> StoredQuotaConfig:
        """Atomically upsert an app aggregate or exact-service configuration."""
        owner, app = self._validate_app(owner_id, audience)
        exact = "" if service is None else _service(service)
        if not isinstance(limits, QuotaConfig):
            raise QuotaConfigurationError("limits must be a QuotaConfig")
        updated = _instant(now)
        with self._write_transaction() as conn:
            conn.execute(
                """
                INSERT INTO quota_configs
                    (owner_id, audience, service, window_seconds, max_requests,
                     max_input_tokens, max_output_tokens, max_total_tokens, updated_at_us)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(owner_id, audience, service) DO UPDATE SET
                    window_seconds = excluded.window_seconds,
                    max_requests = excluded.max_requests,
                    max_input_tokens = excluded.max_input_tokens,
                    max_output_tokens = excluded.max_output_tokens,
                    max_total_tokens = excluded.max_total_tokens,
                    updated_at_us = excluded.updated_at_us
                """,
                (
                    owner,
                    app,
                    exact,
                    limits.window_seconds,
                    limits.max_requests,
                    limits.max_input_tokens,
                    limits.max_output_tokens,
                    limits.max_total_tokens,
                    _epoch_us(updated),
                ),
            )
        return StoredQuotaConfig(owner, app, service, limits, updated)

    def get_config(
        self, owner_id: str, audience: str, *, service: str | None = None
    ) -> StoredQuotaConfig | None:
        """Return an explicitly stored config; app defaults are not synthesized."""
        owner, app = self._validate_app(owner_id, audience)
        exact = "" if service is None else _service(service)
        with self._lock:
            row = (
                self._check_open()
                .execute(
                    "SELECT * FROM quota_configs WHERE owner_id = ? AND audience = ? "
                    "AND service = ?",
                    (owner, app, exact),
                )
                .fetchone()
            )
        return None if row is None else config_from_row(row)

    def effective_app_config(self, owner_id: str, audience: str) -> QuotaConfig:
        """Return the explicit aggregate config or the bounded safe default."""
        stored = self.get_config(owner_id, audience)
        return self.default_config if stored is None else stored.limits

    def delete_config(self, owner_id: str, audience: str, *, service: str | None = None) -> bool:
        owner, app = self._validate_app(owner_id, audience)
        exact = "" if service is None else _service(service)
        with self._write_transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM quota_configs WHERE owner_id = ? AND audience = ? AND service = ?",
                (owner, app, exact),
            )
        return cursor.rowcount > 0

    def reserve(
        self,
        *,
        owner_id: str,
        audience: str,
        service: str,
        reservation_id: str,
        estimated_input_tokens: int = 0,
        estimated_output_tokens: int = 0,
        now: datetime | None = None,
    ) -> QuotaAdmission:
        """Atomically check all applicable limits and reserve projected usage."""
        owner, app = self._validate_app(owner_id, audience)
        exact = _service(service)
        key = _identity("reservation id", reservation_id)
        input_tokens = _token_count("estimated_input_tokens", estimated_input_tokens)
        output_tokens = _token_count("estimated_output_tokens", estimated_output_tokens)
        instant = _instant(now)
        now_us = _epoch_us(instant)

        with self._write_transaction() as conn:
            sweep_stale(
                conn,
                owner,
                app,
                now_us,
                reservation_ttl_seconds=self.reservation_ttl_seconds,
            )
            existing = find_reservation(conn, owner, app, key)
            if existing is not None:
                return idempotent_admission(
                    existing,
                    service=exact,
                    estimated_input_tokens=input_tokens,
                    estimated_output_tokens=output_tokens,
                )

            app_limits = find_limits(conn, owner, app, "") or self.default_config
            scopes: list[tuple[str | None, QuotaConfig]] = [(None, app_limits)]
            service_limits = find_limits(conn, owner, app, exact)
            if service_limits is not None:
                scopes.append((exact, service_limits))

            for limiting_service, limits in scopes:
                current_usage = usage(
                    conn,
                    owner,
                    app,
                    limiting_service,
                    limits.window_seconds,
                    now_us,
                )
                reason = exceeded_reason(current_usage, limits, input_tokens, output_tokens)
                if reason is not None:
                    remaining_us = _epoch_us(current_usage.window_end) - now_us
                    return QuotaAdmission(
                        allowed=False,
                        reason=reason,
                        retry_after_seconds=max(1, math.ceil(remaining_us / 1_000_000)),
                        limiting_service=limiting_service,
                        usage=current_usage,
                        limits=limits,
                    )

            conn.execute(
                """
                INSERT INTO quota_reservations
                    (owner_id, audience, service, reservation_id, state,
                     estimated_input_tokens, estimated_output_tokens, created_at_us)
                VALUES (?, ?, ?, ?, 'reserved', ?, ?, ?)
                """,
                (owner, app, exact, key, input_tokens, output_tokens, now_us),
            )
            reservation = find_reservation(conn, owner, app, key)
            if reservation is None:  # pragma: no cover - SQLite contract guard
                raise QuotaError("reservation insert was not readable")
            return QuotaAdmission(allowed=True, reservation=reservation)

    def complete(
        self,
        *,
        owner_id: str,
        audience: str,
        reservation_id: str,
        actual_input_tokens: int,
        actual_output_tokens: int,
        now: datetime | None = None,
    ) -> QuotaReservation:
        """Reconcile a reservation to actual usage without rewriting completion."""
        owner, app = self._validate_app(owner_id, audience)
        key = _identity("reservation id", reservation_id)
        input_tokens = _token_count("actual_input_tokens", actual_input_tokens)
        output_tokens = _token_count("actual_output_tokens", actual_output_tokens)
        completed_us = _epoch_us(_instant(now))
        with self._write_transaction() as conn:
            existing = find_reservation(conn, owner, app, key)
            if existing is None:
                raise ReservationNotFound("quota reservation not found")
            if existing.state == "released":
                raise ReservationStateError("a released reservation cannot be completed")
            if existing.state == "abandoned":
                raise ReservationStateError("an abandoned reservation cannot be completed")
            if existing.state == "completed":
                if (
                    existing.actual_input_tokens != input_tokens
                    or existing.actual_output_tokens != output_tokens
                ):
                    raise ReservationConflict("completion idempotency key changed actual usage")
                return existing
            conn.execute(
                """
                UPDATE quota_reservations
                SET state = 'completed', actual_input_tokens = ?, actual_output_tokens = ?,
                    completed_at_us = ?
                WHERE owner_id = ? AND audience = ? AND reservation_id = ?
                """,
                (input_tokens, output_tokens, completed_us, owner, app, key),
            )
            result = find_reservation(conn, owner, app, key)
            if result is None:  # pragma: no cover - SQLite contract guard
                raise QuotaError("completed reservation was not readable")
            return result

    def mark_dispatched(
        self, *, owner_id: str, audience: str, reservation_id: str
    ) -> QuotaReservation:
        """Record that downstream execution may now incur token spend."""
        owner, app = self._validate_app(owner_id, audience)
        key = _identity("reservation id", reservation_id)
        with self._write_transaction() as conn:
            existing = find_reservation(conn, owner, app, key)
            if existing is None:
                raise ReservationNotFound("quota reservation not found")
            if existing.state == "dispatched":
                return existing
            if existing.state != "reserved":
                raise ReservationStateError("only a reserved request can be dispatched")
            conn.execute(
                """
                UPDATE quota_reservations SET state = 'dispatched'
                WHERE owner_id = ? AND audience = ? AND reservation_id = ?
                  AND state = 'reserved'
                """,
                (owner, app, key),
            )
            result = find_reservation(conn, owner, app, key)
            if result is None:  # pragma: no cover - SQLite contract guard
                raise QuotaError("dispatched reservation was not readable")
            return result

    def release(self, *, owner_id: str, audience: str, reservation_id: str) -> bool:
        """Release an unused reservation; repeated release is an idempotent no-op."""
        owner, app = self._validate_app(owner_id, audience)
        key = _identity("reservation id", reservation_id)
        released_us = _epoch_us(datetime.now(UTC))
        with self._write_transaction() as conn:
            existing = find_reservation(conn, owner, app, key)
            if existing is None:
                raise ReservationNotFound("quota reservation not found")
            if existing.state in {"dispatched", "completed", "abandoned"}:
                raise ReservationStateError("settled usage cannot be released")
            if existing.state == "released":
                return False
            cursor = conn.execute(
                """
                UPDATE quota_reservations SET state = 'released', released_at_us = ?
                WHERE owner_id = ? AND audience = ? AND reservation_id = ?
                  AND state = 'reserved'
                """,
                (released_us, owner, app, key),
            )
        return cursor.rowcount == 1

    def get_reservation(
        self, owner_id: str, audience: str, reservation_id: str
    ) -> QuotaReservation | None:
        owner, app = self._validate_app(owner_id, audience)
        key = _identity("reservation id", reservation_id)
        with self._lock:
            return find_reservation(self._check_open(), owner, app, key)

    def get_usage(
        self,
        *,
        owner_id: str,
        audience: str,
        service: str | None = None,
        now: datetime | None = None,
    ) -> QuotaUsage:
        """Read aggregate or exact-service usage for its effective window."""
        owner, app = self._validate_app(owner_id, audience)
        exact = None if service is None else _service(service)
        now_us = _epoch_us(_instant(now))
        with self._write_transaction() as conn:
            sweep_stale(
                conn,
                owner,
                app,
                now_us,
                reservation_ttl_seconds=self.reservation_ttl_seconds,
            )
            if exact is None:
                limits = find_limits(conn, owner, app, "") or self.default_config
            else:
                limits = (
                    find_limits(conn, owner, app, exact)
                    or find_limits(conn, owner, app, "")
                    or self.default_config
                )
            return usage(conn, owner, app, exact, limits.window_seconds, now_us)

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __del__(self) -> None:
        """Last-resort cleanup when an embedding never enters app lifespan."""
        try:
            self.close()
        except (AttributeError, sqlite3.Error):
            pass

    def _validate_app(self, owner_id: str, audience: str) -> tuple[str, str]:
        return _identity("owner id", owner_id), _identity("audience", audience)

    def _check_open(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("quota store is closed")
        return self._conn

    @contextlib.contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self._check_open()
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.rollback()
                raise
            else:
                conn.commit()
