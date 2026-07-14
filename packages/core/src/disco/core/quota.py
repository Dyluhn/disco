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
import re
import sqlite3
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

MAX_WINDOW_SECONDS = 86_400
MAX_REQUEST_LIMIT = 1_000_000
MAX_TOKEN_LIMIT = 1_000_000_000_000
MAX_RESERVATION_TTL_SECONDS = 3_600
DEFAULT_RESERVATION_TTL_SECONDS = 300

_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SERVICE_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")


class QuotaError(Exception):
    """Base class for quota configuration and accounting errors."""


class QuotaConfigurationError(QuotaError, ValueError):
    """A quota or identity is unsafe or cannot be represented."""


class ReservationNotFound(QuotaError, LookupError):
    """Completion or release referenced no reservation in this app."""


class ReservationConflict(QuotaError):
    """An idempotency key was reused for different reservation data."""


class ReservationStateError(QuotaError):
    """The requested transition would erase or rewrite accounted usage."""


def _bounded_integer(label: str, value: int | None, maximum: int) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= maximum:
        raise QuotaConfigurationError(f"{label} must be an integer from 1 to {maximum}")


def _token_count(label: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_TOKEN_LIMIT:
        raise QuotaConfigurationError(
            f"{label} must be an integer from 0 to {MAX_TOKEN_LIMIT}"
        )
    return value


def _identity(label: str, value: str) -> str:
    if not isinstance(value, str) or _IDENTITY_RE.fullmatch(value) is None:
        raise QuotaConfigurationError(f"invalid {label}")
    return value


def _service(value: str) -> str:
    if not isinstance(value, str) or len(value.encode("utf-8")) > 128:
        raise QuotaConfigurationError("invalid exact service name")
    if _SERVICE_RE.fullmatch(value) is None:
        raise QuotaConfigurationError("invalid exact service name")
    return value


def _instant(value: datetime | None) -> datetime:
    result = datetime.now(UTC) if value is None else value
    if result.tzinfo is None or result.utcoffset() is None:
        raise QuotaConfigurationError("quota timestamps must be timezone-aware")
    return result.astimezone(UTC)


def _epoch_us(value: datetime) -> int:
    return int(value.timestamp() * 1_000_000)


def _from_epoch_us(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1_000_000, UTC)


@dataclass(frozen=True)
class QuotaConfig:
    """Limits for one fixed window; ``None`` disables only that dimension."""

    window_seconds: int = 60
    max_requests: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_total_tokens: int | None = None

    def __post_init__(self) -> None:
        _bounded_integer("window_seconds", self.window_seconds, MAX_WINDOW_SECONDS)
        _bounded_integer("max_requests", self.max_requests, MAX_REQUEST_LIMIT)
        _bounded_integer("max_input_tokens", self.max_input_tokens, MAX_TOKEN_LIMIT)
        _bounded_integer("max_output_tokens", self.max_output_tokens, MAX_TOKEN_LIMIT)
        _bounded_integer("max_total_tokens", self.max_total_tokens, MAX_TOKEN_LIMIT)
        if all(
            value is None
            for value in (
                self.max_requests,
                self.max_input_tokens,
                self.max_output_tokens,
                self.max_total_tokens,
            )
        ):
            raise QuotaConfigurationError("at least one quota limit is required")


DEFAULT_QUOTA_CONFIG = QuotaConfig(
    window_seconds=60,
    max_requests=100,
    max_input_tokens=1_000_000,
    max_output_tokens=250_000,
    max_total_tokens=1_250_000,
)


@dataclass(frozen=True)
class StoredQuotaConfig:
    owner_id: str
    audience: str
    service: str | None
    limits: QuotaConfig
    updated_at: datetime


@dataclass(frozen=True)
class QuotaUsage:
    request_count: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    window_start: datetime
    window_end: datetime


@dataclass(frozen=True)
class QuotaReservation:
    owner_id: str
    audience: str
    service: str
    reservation_id: str
    state: str
    estimated_input_tokens: int
    estimated_output_tokens: int
    actual_input_tokens: int | None
    actual_output_tokens: int | None
    created_at: datetime
    completed_at: datetime | None
    released_at: datetime | None

    @property
    def accounted_input_tokens(self) -> int:
        if self.state in {"completed", "abandoned"} and self.actual_input_tokens is not None:
            return self.actual_input_tokens
        return self.estimated_input_tokens

    @property
    def accounted_output_tokens(self) -> int:
        if self.state in {"completed", "abandoned"} and self.actual_output_tokens is not None:
            return self.actual_output_tokens
        return self.estimated_output_tokens


@dataclass(frozen=True)
class QuotaAdmission:
    """Admission result.  A denial never creates a reservation."""

    allowed: bool
    reservation: QuotaReservation | None = None
    reason: str | None = None
    retry_after_seconds: int | None = None
    limiting_service: str | None = None
    usage: QuotaUsage | None = None
    limits: QuotaConfig | None = None


class QuotaStore(Protocol):
    """Backend seam shared by app-server and agent-server callers."""

    def configure(
        self,
        *,
        owner_id: str,
        audience: str,
        limits: QuotaConfig,
        service: str | None = None,
        now: datetime | None = None,
    ) -> StoredQuotaConfig: ...

    def get_config(
        self, owner_id: str, audience: str, *, service: str | None = None
    ) -> StoredQuotaConfig | None: ...

    def delete_config(
        self, owner_id: str, audience: str, *, service: str | None = None
    ) -> bool: ...

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
    ) -> QuotaAdmission: ...

    def complete(
        self,
        *,
        owner_id: str,
        audience: str,
        reservation_id: str,
        actual_input_tokens: int,
        actual_output_tokens: int,
        now: datetime | None = None,
    ) -> QuotaReservation: ...

    def mark_dispatched(
        self, *, owner_id: str, audience: str, reservation_id: str
    ) -> QuotaReservation: ...

    def release(self, *, owner_id: str, audience: str, reservation_id: str) -> bool: ...

    def get_usage(
        self,
        *,
        owner_id: str,
        audience: str,
        service: str | None = None,
        now: datetime | None = None,
    ) -> QuotaUsage: ...


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
            row = self._check_open().execute(
                "SELECT * FROM quota_configs WHERE owner_id = ? AND audience = ? AND service = ?",
                (owner, app, exact),
            ).fetchone()
        return None if row is None else self._config_from_row(row)

    def effective_app_config(self, owner_id: str, audience: str) -> QuotaConfig:
        """Return the explicit aggregate config or the bounded safe default."""
        stored = self.get_config(owner_id, audience)
        return self.default_config if stored is None else stored.limits

    def delete_config(
        self, owner_id: str, audience: str, *, service: str | None = None
    ) -> bool:
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
            self._sweep_stale(conn, owner, app, now_us)
            existing = self._find_reservation(conn, owner, app, key)
            if existing is not None:
                return self._idempotent_admission(
                    existing,
                    service=exact,
                    estimated_input_tokens=input_tokens,
                    estimated_output_tokens=output_tokens,
                )

            app_limits = self._find_limits(conn, owner, app, "") or self.default_config
            scopes: list[tuple[str | None, QuotaConfig]] = [(None, app_limits)]
            service_limits = self._find_limits(conn, owner, app, exact)
            if service_limits is not None:
                scopes.append((exact, service_limits))

            for limiting_service, limits in scopes:
                usage = self._usage(
                    conn,
                    owner,
                    app,
                    limiting_service,
                    limits.window_seconds,
                    now_us,
                )
                reason = self._exceeded_reason(usage, limits, input_tokens, output_tokens)
                if reason is not None:
                    remaining_us = _epoch_us(usage.window_end) - now_us
                    return QuotaAdmission(
                        allowed=False,
                        reason=reason,
                        retry_after_seconds=max(1, math.ceil(remaining_us / 1_000_000)),
                        limiting_service=limiting_service,
                        usage=usage,
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
            reservation = self._find_reservation(conn, owner, app, key)
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
            existing = self._find_reservation(conn, owner, app, key)
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
            result = self._find_reservation(conn, owner, app, key)
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
            existing = self._find_reservation(conn, owner, app, key)
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
            result = self._find_reservation(conn, owner, app, key)
            if result is None:  # pragma: no cover - SQLite contract guard
                raise QuotaError("dispatched reservation was not readable")
            return result

    def release(self, *, owner_id: str, audience: str, reservation_id: str) -> bool:
        """Release an unused reservation; repeated release is an idempotent no-op."""
        owner, app = self._validate_app(owner_id, audience)
        key = _identity("reservation id", reservation_id)
        released_us = _epoch_us(datetime.now(UTC))
        with self._write_transaction() as conn:
            existing = self._find_reservation(conn, owner, app, key)
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
            return self._find_reservation(self._check_open(), owner, app, key)

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
            self._sweep_stale(conn, owner, app, now_us)
            if exact is None:
                limits = self._find_limits(conn, owner, app, "") or self.default_config
            else:
                limits = (
                    self._find_limits(conn, owner, app, exact)
                    or self._find_limits(conn, owner, app, "")
                    or self.default_config
                )
            return self._usage(conn, owner, app, exact, limits.window_seconds, now_us)

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

    def _sweep_stale(
        self, conn: sqlite3.Connection, owner: str, app: str, now_us: int
    ) -> None:
        """Settle expired leases without erasing their admitted request count."""
        cutoff_us = now_us - self.reservation_ttl_seconds * 1_000_000
        conn.execute(
            """
            UPDATE quota_reservations
            SET state = 'abandoned', actual_input_tokens = 0, actual_output_tokens = 0,
                completed_at_us = ?
            WHERE owner_id = ? AND audience = ? AND state = 'reserved'
              AND created_at_us <= ?
            """,
            (now_us, owner, app, cutoff_us),
        )
        conn.execute(
            """
            UPDATE quota_reservations
            SET state = 'abandoned',
                actual_input_tokens = estimated_input_tokens,
                actual_output_tokens = estimated_output_tokens,
                completed_at_us = ?
            WHERE owner_id = ? AND audience = ? AND state = 'dispatched'
              AND created_at_us <= ?
            """,
            (now_us, owner, app, cutoff_us),
        )
        retention_cutoff = now_us - MAX_WINDOW_SECONDS * 1_000_000
        conn.execute(
            """
            DELETE FROM quota_reservations WHERE rowid IN (
                SELECT rowid FROM quota_reservations
                WHERE owner_id = ? AND audience = ?
                  AND state IN ('completed', 'abandoned', 'released')
                  AND created_at_us < ?
                ORDER BY created_at_us LIMIT 1000
            )
            """,
            (owner, app, retention_cutoff),
        )

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

    @staticmethod
    def _find_limits(
        conn: sqlite3.Connection, owner: str, app: str, service: str
    ) -> QuotaConfig | None:
        row = conn.execute(
            """
            SELECT window_seconds, max_requests, max_input_tokens,
                   max_output_tokens, max_total_tokens
            FROM quota_configs WHERE owner_id = ? AND audience = ? AND service = ?
            """,
            (owner, app, service),
        ).fetchone()
        if row is None:
            return None
        return QuotaConfig(
            window_seconds=int(row["window_seconds"]),
            max_requests=row["max_requests"],
            max_input_tokens=row["max_input_tokens"],
            max_output_tokens=row["max_output_tokens"],
            max_total_tokens=row["max_total_tokens"],
        )

    @classmethod
    def _config_from_row(cls, row: sqlite3.Row) -> StoredQuotaConfig:
        limits = QuotaConfig(
            window_seconds=int(row["window_seconds"]),
            max_requests=row["max_requests"],
            max_input_tokens=row["max_input_tokens"],
            max_output_tokens=row["max_output_tokens"],
            max_total_tokens=row["max_total_tokens"],
        )
        service = str(row["service"])
        return StoredQuotaConfig(
            owner_id=str(row["owner_id"]),
            audience=str(row["audience"]),
            service=service or None,
            limits=limits,
            updated_at=_from_epoch_us(int(row["updated_at_us"])),
        )

    @staticmethod
    def _find_reservation(
        conn: sqlite3.Connection, owner: str, app: str, reservation_id: str
    ) -> QuotaReservation | None:
        row = conn.execute(
            """
            SELECT * FROM quota_reservations
            WHERE owner_id = ? AND audience = ? AND reservation_id = ?
            """,
            (owner, app, reservation_id),
        ).fetchone()
        if row is None:
            return None
        return QuotaReservation(
            owner_id=str(row["owner_id"]),
            audience=str(row["audience"]),
            service=str(row["service"]),
            reservation_id=str(row["reservation_id"]),
            state=str(row["state"]),
            estimated_input_tokens=int(row["estimated_input_tokens"]),
            estimated_output_tokens=int(row["estimated_output_tokens"]),
            actual_input_tokens=(
                None if row["actual_input_tokens"] is None else int(row["actual_input_tokens"])
            ),
            actual_output_tokens=(
                None
                if row["actual_output_tokens"] is None
                else int(row["actual_output_tokens"])
            ),
            created_at=_from_epoch_us(int(row["created_at_us"])),
            completed_at=(
                None if row["completed_at_us"] is None else _from_epoch_us(row["completed_at_us"])
            ),
            released_at=(
                None if row["released_at_us"] is None else _from_epoch_us(row["released_at_us"])
            ),
        )

    @staticmethod
    def _usage(
        conn: sqlite3.Connection,
        owner: str,
        app: str,
        service: str | None,
        window_seconds: int,
        now_us: int,
    ) -> QuotaUsage:
        window_us = window_seconds * 1_000_000
        start_us = (now_us // window_us) * window_us
        end_us = start_us + window_us
        service_sql = "" if service is None else " AND service = ?"
        params: tuple[object, ...] = (owner, app, start_us, end_us)
        if service is not None:
            params += (service,)
        row = conn.execute(
            """
            SELECT COUNT(*) AS request_count,
                   COALESCE(SUM(CASE WHEN state NOT IN ('reserved', 'dispatched')
                       THEN actual_input_tokens ELSE estimated_input_tokens END), 0)
                       AS input_tokens,
                   COALESCE(SUM(CASE WHEN state NOT IN ('reserved', 'dispatched')
                       THEN actual_output_tokens ELSE estimated_output_tokens END), 0)
                       AS output_tokens
            FROM quota_reservations
            WHERE owner_id = ? AND audience = ?
              AND created_at_us >= ? AND created_at_us < ?
              AND state != 'released'
            """
            + service_sql,
            params,
        ).fetchone()
        requests = int(row["request_count"])
        input_tokens = int(row["input_tokens"])
        output_tokens = int(row["output_tokens"])
        return QuotaUsage(
            request_count=requests,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            window_start=_from_epoch_us(start_us),
            window_end=_from_epoch_us(end_us),
        )

    @staticmethod
    def _exceeded_reason(
        usage: QuotaUsage, limits: QuotaConfig, input_tokens: int, output_tokens: int
    ) -> str | None:
        checks = (
            ("requests", usage.request_count + 1, limits.max_requests),
            ("input_tokens", usage.input_tokens + input_tokens, limits.max_input_tokens),
            ("output_tokens", usage.output_tokens + output_tokens, limits.max_output_tokens),
            (
                "total_tokens",
                usage.total_tokens + input_tokens + output_tokens,
                limits.max_total_tokens,
            ),
        )
        for label, projected, limit in checks:
            if limit is not None and projected > limit:
                return label
        return None

    @staticmethod
    def _idempotent_admission(
        existing: QuotaReservation,
        *,
        service: str,
        estimated_input_tokens: int,
        estimated_output_tokens: int,
    ) -> QuotaAdmission:
        if (
            existing.service != service
            or existing.estimated_input_tokens != estimated_input_tokens
            or existing.estimated_output_tokens != estimated_output_tokens
        ):
            raise ReservationConflict("reservation idempotency key changed request data")
        if existing.state == "released":
            return QuotaAdmission(
                allowed=False,
                reservation=existing,
                reason="reservation_released",
            )
        if existing.state == "abandoned":
            return QuotaAdmission(
                allowed=False,
                reservation=existing,
                reason="reservation_abandoned",
            )
        return QuotaAdmission(allowed=True, reservation=existing)
