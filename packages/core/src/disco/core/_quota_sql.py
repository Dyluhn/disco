"""Stateless SQLite queries for the quota store's single transaction owner."""

from __future__ import annotations

import sqlite3

from ._quota_contracts import (
    MAX_WINDOW_SECONDS,
    QuotaAdmission,
    QuotaConfig,
    QuotaReservation,
    QuotaUsage,
    ReservationConflict,
    StoredQuotaConfig,
    _from_epoch_us,
)


def sweep_stale(
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


def find_limits(
    conn: sqlite3.Connection,
    owner: str,
    app: str,
    service: str,
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


def config_from_row(row: sqlite3.Row) -> StoredQuotaConfig:
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


def find_reservation(
    conn: sqlite3.Connection,
    owner: str,
    app: str,
    reservation_id: str,
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


def usage(
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


def exceeded_reason(
    current: QuotaUsage,
    limits: QuotaConfig,
    input_tokens: int,
    output_tokens: int,
) -> str | None:
    checks = (
        ("requests", current.request_count + 1, limits.max_requests),
        ("input_tokens", current.input_tokens + input_tokens, limits.max_input_tokens),
        ("output_tokens", current.output_tokens + output_tokens, limits.max_output_tokens),
        (
            "total_tokens",
            current.total_tokens + input_tokens + output_tokens,
            limits.max_total_tokens,
        ),
    )
    for label, projected, limit in checks:
        if limit is not None and projected > limit:
            return label
    return None


def idempotent_admission(
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
