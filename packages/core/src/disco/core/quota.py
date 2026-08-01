"""Durable per-app request and token quota accounting.

This module is intentionally server-agnostic.  Both server siblings can open
the same SQLite database and use :class:`SqliteQuotaStore`; admission is
serialized with ``BEGIN IMMEDIATE`` so the check and reservation are one
cross-process transaction.

Quota identity is always ``owner_id + audience``.  ``audience`` is the app
identity carried by the host-service credential.  Every request consumes the
app aggregate quota and, when configured, an additional exact-service quota.
There is no prefix or wildcard matching.

This module is the state-free public compatibility facade for identity,
validation, and value types.  ``SqliteQuotaStore`` is the sole concrete
persistence implementation; its cohesive private collaborators (connection
lifecycle, ``quota_configs`` CRUD, ``quota_reservations`` admission/usage)
live under :mod:`disco.core.quota_parts` and are re-imported here so every
name this module has ever exported keeps this module as its import path.

The identity/validation helpers and value types below live in
``quota_parts._types`` — a dependency-free leaf every collaborator here
(including this module) imports from, so the import graph has one direction
and no cycle back through this module.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Protocol

from .quota_parts._config_store import _QuotaConfigStore
from .quota_parts._connection import _QuotaConnection
from .quota_parts._reservation_ledger import _ReservationLedger
from .quota_parts._types import (
    _IDENTITY_RE,
    _SERVICE_RE,
    DEFAULT_QUOTA_CONFIG,
    DEFAULT_RESERVATION_TTL_SECONDS,
    MAX_REQUEST_LIMIT,
    MAX_RESERVATION_TTL_SECONDS,
    MAX_TOKEN_LIMIT,
    MAX_WINDOW_SECONDS,
    UTC,
    QuotaAdmission,
    QuotaConfig,
    QuotaConfigurationError,
    QuotaError,
    QuotaReservation,
    QuotaUsage,
    ReservationConflict,
    ReservationNotFound,
    ReservationStateError,
    StoredQuotaConfig,
    _bounded_integer,
    _epoch_us,
    _from_epoch_us,
    _identity,
    _instant,
    _service,
    _token_count,
    _validate_app,
    dataclass,
    datetime,
    re,
)

# ``quota_parts._types`` is the sole owner of these definitions; this module
# only re-exports them so every name it has ever exported keeps this module as
# its import path (proven mechanically by diffing ``dir(disco.core.quota)``).
# Several are not referenced again below — that is expected for a facade, not
# an unused import — so they are declared here rather than left for ruff to
# flag. ``_SCHEMA`` (the raw DDL) is the one exception: it had zero reachers
# outside this file (verified via a repo-wide grep) and is not restored.
__all__ = [
    "DEFAULT_QUOTA_CONFIG",
    "DEFAULT_RESERVATION_TTL_SECONDS",
    "MAX_REQUEST_LIMIT",
    "MAX_RESERVATION_TTL_SECONDS",
    "MAX_TOKEN_LIMIT",
    "MAX_WINDOW_SECONDS",
    "Path",
    "Protocol",
    "QuotaAdmission",
    "QuotaConfig",
    "QuotaConfigurationError",
    "QuotaError",
    "QuotaReservation",
    "QuotaStore",
    "QuotaUsage",
    "ReservationConflict",
    "ReservationNotFound",
    "ReservationStateError",
    "SqliteQuotaStore",
    "StoredQuotaConfig",
    "UTC",
    "_IDENTITY_RE",
    "_SERVICE_RE",
    "_bounded_integer",
    "_epoch_us",
    "_from_epoch_us",
    "_identity",
    "_instant",
    "_service",
    "_token_count",
    "_validate_app",
    "dataclass",
    "datetime",
    "re",
    "sqlite3",
]


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


class SqliteQuotaStore:
    """SQLite quota configuration, reservations, and usage accounting.

    Persistence detail is split across cohesive collaborators under
    :mod:`disco.core.quota_parts`: :class:`~disco.core.quota_parts._connection._QuotaConnection`
    owns the one real ``sqlite3.Connection``, the write-transaction/read-lock
    discipline, and the schema bootstrap;
    :class:`~disco.core.quota_parts._config_store._QuotaConfigStore` owns every
    read/write of ``quota_configs``; and
    :class:`~disco.core.quota_parts._reservation_ledger._ReservationLedger` owns
    admission, completion/dispatch/release, sweeping, and usage accounting over
    ``quota_reservations``. This class remains the sole public entry point and
    ``QuotaStore`` implementation — it validates constructor inputs once at the
    boundary and otherwise delegates.
    """

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        default_config: QuotaConfig = DEFAULT_QUOTA_CONFIG,
        reservation_ttl_seconds: int = DEFAULT_RESERVATION_TTL_SECONDS,
    ) -> None:
        if not isinstance(default_config, QuotaConfig):
            raise QuotaConfigurationError("default_config must be a QuotaConfig")
        _bounded_integer(
            "reservation_ttl_seconds",
            reservation_ttl_seconds,
            MAX_RESERVATION_TTL_SECONDS,
        )
        self.default_config = default_config
        self.reservation_ttl_seconds = reservation_ttl_seconds
        self._connection = _QuotaConnection(path)
        self.db_path = self._connection.db_path
        self._configs = _QuotaConfigStore(self._connection, default_config=default_config)
        self._ledger = _ReservationLedger(
            self._connection,
            default_config=default_config,
            reservation_ttl_seconds=reservation_ttl_seconds,
        )

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
        return self._configs.configure(
            owner_id=owner_id,
            audience=audience,
            limits=limits,
            service=service,
            now=now,
        )

    def get_config(
        self, owner_id: str, audience: str, *, service: str | None = None
    ) -> StoredQuotaConfig | None:
        """Return an explicitly stored config; app defaults are not synthesized."""
        return self._configs.get_config(owner_id, audience, service=service)

    def effective_app_config(self, owner_id: str, audience: str) -> QuotaConfig:
        """Return the explicit aggregate config or the bounded safe default."""
        return self._configs.effective_app_config(owner_id, audience)

    def delete_config(self, owner_id: str, audience: str, *, service: str | None = None) -> bool:
        return self._configs.delete_config(owner_id, audience, service=service)

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
        return self._ledger.reserve(
            owner_id=owner_id,
            audience=audience,
            service=service,
            reservation_id=reservation_id,
            estimated_input_tokens=estimated_input_tokens,
            estimated_output_tokens=estimated_output_tokens,
            now=now,
        )

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
        return self._ledger.complete(
            owner_id=owner_id,
            audience=audience,
            reservation_id=reservation_id,
            actual_input_tokens=actual_input_tokens,
            actual_output_tokens=actual_output_tokens,
            now=now,
        )

    def mark_dispatched(
        self, *, owner_id: str, audience: str, reservation_id: str
    ) -> QuotaReservation:
        """Record that downstream execution may now incur token spend."""
        return self._ledger.mark_dispatched(
            owner_id=owner_id, audience=audience, reservation_id=reservation_id
        )

    def release(self, *, owner_id: str, audience: str, reservation_id: str) -> bool:
        """Release an unused reservation; repeated release is an idempotent no-op."""
        return self._ledger.release(
            owner_id=owner_id, audience=audience, reservation_id=reservation_id
        )

    def get_reservation(
        self, owner_id: str, audience: str, reservation_id: str
    ) -> QuotaReservation | None:
        return self._ledger.get_reservation(owner_id, audience, reservation_id)

    def get_usage(
        self,
        *,
        owner_id: str,
        audience: str,
        service: str | None = None,
        now: datetime | None = None,
    ) -> QuotaUsage:
        """Read aggregate or exact-service usage for its effective window."""
        return self._ledger.get_usage(
            owner_id=owner_id,
            audience=audience,
            service=service,
            now=now,
        )

    def close(self) -> None:
        self._connection.close()

    def __del__(self) -> None:
        """Last-resort cleanup when an embedding never enters app lifespan."""
        try:
            self.close()
        except (AttributeError, sqlite3.Error):
            pass
