"""Durable per-app/exact-service quota configuration rows.

Extracted from :class:`~disco.core.quota.SqliteQuotaStore`: this module owns
every read/write of the ``quota_configs`` table. ``_find_limits`` is also used
by ``_reservation_ledger``, which reads the same table from inside its own
already-open write transaction rather than through a second lock acquisition;
it is kept a plain module-level function (not a method) for exactly that
reuse.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from ._connection import _QuotaConnection
from ._types import (
    QuotaConfig,
    QuotaConfigurationError,
    StoredQuotaConfig,
    _epoch_us,
    _from_epoch_us,
    _instant,
    _service,
    _validate_app,
)


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


def _config_from_row(row: sqlite3.Row) -> StoredQuotaConfig:
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


class _QuotaConfigStore:
    """Owns ``configure``/``get_config``/``delete_config`` for one connection."""

    def __init__(self, connection: _QuotaConnection, *, default_config: QuotaConfig) -> None:
        self._connection = connection
        self.default_config = default_config

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
        owner, app = _validate_app(owner_id, audience)
        exact = "" if service is None else _service(service)
        if not isinstance(limits, QuotaConfig):
            raise QuotaConfigurationError("limits must be a QuotaConfig")
        updated = _instant(now)
        with self._connection.write_transaction() as conn:
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
        owner, app = _validate_app(owner_id, audience)
        exact = "" if service is None else _service(service)
        with self._connection.read_lock() as conn:
            row = conn.execute(
                "SELECT * FROM quota_configs WHERE owner_id = ? AND audience = ? "
                "AND service = ?",
                (owner, app, exact),
            ).fetchone()
        return None if row is None else _config_from_row(row)

    def effective_app_config(self, owner_id: str, audience: str) -> QuotaConfig:
        """Return the explicit aggregate config or the bounded safe default."""
        stored = self.get_config(owner_id, audience)
        return self.default_config if stored is None else stored.limits

    def delete_config(self, owner_id: str, audience: str, *, service: str | None = None) -> bool:
        owner, app = _validate_app(owner_id, audience)
        exact = "" if service is None else _service(service)
        with self._connection.write_transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM quota_configs WHERE owner_id = ? AND audience = ? AND service = ?",
                (owner, app, exact),
            )
        return cursor.rowcount > 0
