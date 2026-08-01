"""Reservation admission, completion, and usage accounting.

Extracted from :class:`~disco.core.quota.SqliteQuotaStore`: this module owns
every read/write of the ``quota_reservations`` table — admission checks,
completion/dispatch/release state transitions, stale-lease sweeping, and the
fixed-window usage aggregate the app- and exact-service limits are checked
against. The read-only helpers (``_find_reservation``, ``_usage``,
``_exceeded_reason``, ``_idempotent_admission``) are plain module-level
functions rather than methods: none of them touch instance state, and keeping
them out of the class body keeps :class:`_ReservationLedger` itself a small,
readable surface of just the six operations ``SqliteQuotaStore`` exposes.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import UTC, datetime

from ._config_store import _find_limits
from ._connection import _QuotaConnection
from ._types import (
    MAX_WINDOW_SECONDS,
    QuotaAdmission,
    QuotaConfig,
    QuotaError,
    QuotaReservation,
    QuotaUsage,
    ReservationConflict,
    ReservationNotFound,
    ReservationStateError,
    _epoch_us,
    _from_epoch_us,
    _identity,
    _instant,
    _service,
    _token_count,
    _validate_app,
)


def _sweep_stale(
    conn: sqlite3.Connection,
    owner: str,
    app: str,
    now_us: int,
    *,
    reservation_ttl_seconds: int,
) -> None:
    """Settle expired leases without erasing their admitted request count."""
    cutoff_us = now_us - reservation_ttl_seconds * 1_000_000
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
            None if row["actual_output_tokens"] is None else int(row["actual_output_tokens"])
        ),
        created_at=_from_epoch_us(int(row["created_at_us"])),
        completed_at=(
            None if row["completed_at_us"] is None else _from_epoch_us(row["completed_at_us"])
        ),
        released_at=(
            None if row["released_at_us"] is None else _from_epoch_us(row["released_at_us"])
        ),
    )


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


class _ReservationLedger:
    """Owns reservation admission/state transitions and usage accounting."""

    def __init__(
        self,
        connection: _QuotaConnection,
        *,
        default_config: QuotaConfig,
        reservation_ttl_seconds: int,
    ) -> None:
        self._connection = connection
        self.default_config = default_config
        self.reservation_ttl_seconds = reservation_ttl_seconds

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
        owner, app = _validate_app(owner_id, audience)
        exact = _service(service)
        key = _identity("reservation id", reservation_id)
        input_tokens = _token_count("estimated_input_tokens", estimated_input_tokens)
        output_tokens = _token_count("estimated_output_tokens", estimated_output_tokens)
        instant = _instant(now)
        now_us = _epoch_us(instant)

        with self._connection.write_transaction() as conn:
            _sweep_stale(
                conn, owner, app, now_us, reservation_ttl_seconds=self.reservation_ttl_seconds
            )
            existing = _find_reservation(conn, owner, app, key)
            if existing is not None:
                return _idempotent_admission(
                    existing,
                    service=exact,
                    estimated_input_tokens=input_tokens,
                    estimated_output_tokens=output_tokens,
                )

            app_limits = _find_limits(conn, owner, app, "") or self.default_config
            scopes: list[tuple[str | None, QuotaConfig]] = [(None, app_limits)]
            service_limits = _find_limits(conn, owner, app, exact)
            if service_limits is not None:
                scopes.append((exact, service_limits))

            for limiting_service, limits in scopes:
                usage = _usage(
                    conn,
                    owner,
                    app,
                    limiting_service,
                    limits.window_seconds,
                    now_us,
                )
                reason = _exceeded_reason(usage, limits, input_tokens, output_tokens)
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
            reservation = _find_reservation(conn, owner, app, key)
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
        owner, app = _validate_app(owner_id, audience)
        key = _identity("reservation id", reservation_id)
        input_tokens = _token_count("actual_input_tokens", actual_input_tokens)
        output_tokens = _token_count("actual_output_tokens", actual_output_tokens)
        completed_us = _epoch_us(_instant(now))
        with self._connection.write_transaction() as conn:
            existing = _find_reservation(conn, owner, app, key)
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
            result = _find_reservation(conn, owner, app, key)
            if result is None:  # pragma: no cover - SQLite contract guard
                raise QuotaError("completed reservation was not readable")
            return result

    def mark_dispatched(
        self, *, owner_id: str, audience: str, reservation_id: str
    ) -> QuotaReservation:
        """Record that downstream execution may now incur token spend."""
        owner, app = _validate_app(owner_id, audience)
        key = _identity("reservation id", reservation_id)
        with self._connection.write_transaction() as conn:
            existing = _find_reservation(conn, owner, app, key)
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
            result = _find_reservation(conn, owner, app, key)
            if result is None:  # pragma: no cover - SQLite contract guard
                raise QuotaError("dispatched reservation was not readable")
            return result

    def release(self, *, owner_id: str, audience: str, reservation_id: str) -> bool:
        """Release an unused reservation; repeated release is an idempotent no-op."""
        owner, app = _validate_app(owner_id, audience)
        key = _identity("reservation id", reservation_id)
        released_us = _epoch_us(datetime.now(UTC))
        with self._connection.write_transaction() as conn:
            existing = _find_reservation(conn, owner, app, key)
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
        owner, app = _validate_app(owner_id, audience)
        key = _identity("reservation id", reservation_id)
        with self._connection.read_lock() as conn:
            return _find_reservation(conn, owner, app, key)

    def get_usage(
        self,
        *,
        owner_id: str,
        audience: str,
        service: str | None = None,
        now: datetime | None = None,
    ) -> QuotaUsage:
        """Read aggregate or exact-service usage for its effective window."""
        owner, app = _validate_app(owner_id, audience)
        exact = None if service is None else _service(service)
        now_us = _epoch_us(_instant(now))
        with self._connection.write_transaction() as conn:
            _sweep_stale(
                conn, owner, app, now_us, reservation_ttl_seconds=self.reservation_ttl_seconds
            )
            if exact is None:
                limits = _find_limits(conn, owner, app, "") or self.default_config
            else:
                limits = (
                    _find_limits(conn, owner, app, exact)
                    or _find_limits(conn, owner, app, "")
                    or self.default_config
                )
            return _usage(conn, owner, app, exact, limits.window_seconds, now_us)
