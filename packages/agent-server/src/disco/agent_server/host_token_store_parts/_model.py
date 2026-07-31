"""Shared constants, exceptions, and the record type for host-service tokens.

Deliberately free of any dependency on ``HostTokenStore`` (the parent facade
class) so every other ``host_token_store_parts`` module — and the parent
itself — can import from here without a circular import.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

_TOKEN_PREFIX = "a4v1"
_TOKEN_VERSION = 1
_LEGACY_TOKEN_PREFIX = "a2v0"
_LEGACY_TOKEN_VERSION = 0
_SELECTOR_BYTES = 16
_VERIFIER_BYTES = 32
_TOKEN_RE = re.compile(r"^(a2v0|a4v1)\.([A-Za-z0-9_-]{22})\.([A-Za-z0-9_-]{43})$")
_SELECTOR_RE = re.compile(r"^[A-Za-z0-9_-]{22}$")
_DUMMY_DIGEST = b"\x00" * hashlib.sha256().digest_size
_TABLE_NAME = "host_service_tokens"
_TOKEN_KINDS = frozenset({"preview", "deployed", "probe"})
_DEFAULT_LIFETIMES = {
    "preview": timedelta(minutes=30),
    "probe": timedelta(minutes=2),
}
TokenKind = Literal["preview", "deployed", "probe"]


class HostTokenError(Exception):
    """Base class for token-store errors."""


class TokenStoreClosed(HostTokenError):
    """The store is closed and cannot be used."""


@dataclass(frozen=True)
class HostTokenRecord:
    """A durable credential record. It deliberately contains no verifier."""

    selector: str
    version: int
    conversation_id: str
    owner_id: str
    audience: str
    allowed_services: frozenset[str]
    allowed_origins: frozenset[str]
    kind: TokenKind
    generation: int
    created_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None and (
            self.expires_at is None or self.expires_at > datetime.now(UTC)
        )
