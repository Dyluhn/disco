"""Public quota contracts and their framework-free validation rules."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
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
        raise QuotaConfigurationError(f"{label} must be an integer from 0 to {MAX_TOKEN_LIMIT}")
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
    """Admission result. A denial never creates a reservation."""

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
