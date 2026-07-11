"""Durable, scoped bearer credentials for the WO-A2.2 host-service bus.

New tokens have one exact wire shape::

    a4v1.<22-char selector>.<43-char verifier>

Only a SHA-256 digest of the verifier is persisted. Records are operational
state, not conversation events, and contain the trusted principal and complete
capability scope used by the bus. Persisted A2 ``a2v0`` records remain usable
during migration; the store never mints new v0 credentials.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import logging
import re
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from disco.core.host_egress import origin_for_url
from disco.core.host_services import valid_host_service_name

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
_LOG = logging.getLogger(__name__)


def _canonical_origins(origins: frozenset[str]) -> frozenset[str]:
    canonical: set[str] = set()
    for value in origins:
        try:
            parsed = urlsplit(value)
            origin = origin_for_url(value)
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("invalid host-service return origin") from exc
        if (
            origin is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("invalid host-service return origin")
        if parsed.scheme == "http":
            host = parsed.hostname or ""
            try:
                loopback = host.lower() == "localhost" or ipaddress.ip_address(host).is_loopback
            except ValueError:
                loopback = host.lower() == "localhost"
            if not loopback:
                raise ValueError("plaintext host-service return origin must be loopback")
        canonical.add(origin)
    return frozenset(canonical)


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


class HostTokenStore:
    """Thread-safe SQLite store for scoped host-service credentials."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.db_path = str(path)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            timeout=5.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self._lock:
            conn = self._check_open()
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {_TABLE_NAME} (
                    selector          TEXT PRIMARY KEY,
                    version           INTEGER NOT NULL DEFAULT 0,
                    verifier_digest   BLOB NOT NULL,
                    conversation_id   TEXT NOT NULL,
                    owner_id          TEXT NOT NULL,
                    audience          TEXT NOT NULL,
                    allowed_services  TEXT NOT NULL,
                    allowed_origins   TEXT NOT NULL,
                    kind              TEXT NOT NULL,
                    generation        INTEGER NOT NULL,
                    created_at        TEXT NOT NULL,
                    expires_at        TEXT,
                    revoked_at        TEXT
                )
                """
            )
            columns = {
                str(row["name"])
                for row in conn.execute(f"PRAGMA table_info({_TABLE_NAME})").fetchall()
            }
            if "version" not in columns:
                # Every record predating this column was minted by A2 as a2v0.
                conn.execute(
                    f"ALTER TABLE {_TABLE_NAME} "
                    f"ADD COLUMN version INTEGER NOT NULL DEFAULT {_LEGACY_TOKEN_VERSION}"
                )
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_host_tokens_conv "
                f"ON {_TABLE_NAME} (conversation_id)"
            )
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_host_tokens_audience "
                f"ON {_TABLE_NAME} (conversation_id, audience)"
            )
            conn.commit()

    @staticmethod
    def _random_component(byte_count: int) -> str:
        return (
            base64.urlsafe_b64encode(secrets.token_bytes(byte_count)).rstrip(b"=").decode("ascii")
        )

    @staticmethod
    def _digest(verifier: str) -> bytes:
        return hashlib.sha256(verifier.encode("ascii")).digest()

    def mint(
        self,
        conversation_id: str,
        owner_id: str,
        audience: str,
        *,
        allowed_services: frozenset[str] = frozenset({"svc.ping"}),
        allowed_origins: frozenset[str] | None = None,
        kind: TokenKind = "preview",
        generation: int = 0,
        expires_in: timedelta | None = None,
    ) -> str:
        """Mint one credential and return its plaintext exactly once."""
        if not conversation_id or not owner_id or not audience:
            raise ValueError("host-service credential identity fields must be non-empty")
        if not allowed_services:
            raise ValueError("host-service credential must authorize at least one service")
        if any(not valid_host_service_name(service) for service in allowed_services):
            raise ValueError("host-service credential contains an invalid service name")
        if kind not in _TOKEN_KINDS:
            raise ValueError("invalid host-service credential kind")
        if generation < 0:
            raise ValueError("host-service credential generation must be non-negative")
        if expires_in is None:
            expires_in = _DEFAULT_LIFETIMES.get(kind)
        canonical_origins = _canonical_origins(allowed_origins or frozenset())
        verifier = self._random_component(_VERIFIER_BYTES)
        digest = self._digest(verifier)
        now = datetime.now(UTC)
        expires_at = (now + expires_in).isoformat() if expires_in is not None else None
        services_json = json.dumps(sorted(allowed_services), separators=(",", ":"))
        origins_json = json.dumps(sorted(canonical_origins), separators=(",", ":"))
        with self._lock:
            conn = self._check_open()
            for _attempt in range(8):
                selector = self._random_component(_SELECTOR_BYTES)
                try:
                    conn.execute(
                        f"""
                        INSERT INTO {_TABLE_NAME}
                        (selector, version, verifier_digest, conversation_id, owner_id, audience,
                         allowed_services, allowed_origins, kind, generation,
                         created_at, expires_at, revoked_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                        """,
                        (
                            selector,
                            _TOKEN_VERSION,
                            digest,
                            conversation_id,
                            owner_id,
                            audience,
                            services_json,
                            origins_json,
                            kind,
                            generation,
                            now.isoformat(),
                            expires_at,
                        ),
                    )
                except sqlite3.IntegrityError:
                    continue
                conn.commit()
                _LOG.info(
                    "host credential minted selector=%s conversation=%s app=%s "
                    "kind=%s generation=%d",
                    selector,
                    conversation_id,
                    audience,
                    kind,
                    generation,
                )
                return f"{_TOKEN_PREFIX}.{selector}.{verifier}"
        raise HostTokenError("could not allocate a unique host-service credential")

    def verify(self, token: str) -> HostTokenRecord | None:
        """Return an active record, or None for every auth failure."""
        with self._lock:
            self._check_open()
        parsed = self._parse_versioned(token)
        if parsed is None:
            return None
        version, selector, verifier = parsed
        with self._lock:
            conn = self._check_open()
            row = conn.execute(
                f"SELECT * FROM {_TABLE_NAME} WHERE selector = ?",
                (selector,),
            ).fetchone()
        expected = _DUMMY_DIGEST
        stored_version: int | None = None
        row_header_valid = row is not None
        if row is not None:
            try:
                raw_digest = row["verifier_digest"]
                raw_version = row["version"]
                if not isinstance(raw_digest, bytes) or len(raw_digest) != len(_DUMMY_DIGEST):
                    raise HostTokenError("invalid verifier digest in host token store")
                if not isinstance(raw_version, int) or isinstance(raw_version, bool):
                    raise HostTokenError("invalid credential version in host token store")
                expected = raw_digest
                stored_version = raw_version
            except (HostTokenError, IndexError, KeyError, TypeError, ValueError, OverflowError):
                row_header_valid = False
        matched = secrets.compare_digest(self._digest(verifier), expected)
        if not matched or row is None or not row_header_valid or stored_version != version:
            return None
        try:
            record = self._record_from_row(dict(row), expected_selector=selector)
            return record if record.is_active else None
        except (HostTokenError, KeyError, TypeError, ValueError, OverflowError):
            # Persisted auth state is untrusted at this boundary. Corruption or
            # an incompatible row must fail like every other credential, never
            # escape into the bus as a 500 or disclose which field was invalid.
            _LOG.warning("malformed persisted host credential selector=%s", selector)
            return None

    @staticmethod
    def _parse_versioned(token: str) -> tuple[int, str, str] | None:
        match = _TOKEN_RE.fullmatch(token)
        if match is None:
            return None
        version = (
            _LEGACY_TOKEN_VERSION if match.group(1) == _LEGACY_TOKEN_PREFIX else _TOKEN_VERSION
        )
        return version, match.group(2), match.group(3)

    @staticmethod
    def _parse(token: str) -> tuple[str, str] | None:
        """Compatibility parser for the existing Stripe live verifier."""
        parsed = HostTokenStore._parse_versioned(token)
        return (parsed[1], parsed[2]) if parsed is not None else None

    def revoke(self, selector: str) -> bool:
        with self._lock:
            conn = self._check_open()
            cur = conn.execute(
                f"UPDATE {_TABLE_NAME} SET revoked_at = ? "
                "WHERE selector = ? AND revoked_at IS NULL",
                (datetime.now(UTC).isoformat(), selector),
            )
            conn.commit()
            if cur.rowcount > 0:
                _LOG.info("host credential revoked selector=%s", selector)
            return cur.rowcount > 0

    def revoke_for_conversation(self, conversation_id: str) -> int:
        with self._lock:
            conn = self._check_open()
            cur = conn.execute(
                f"UPDATE {_TABLE_NAME} SET revoked_at = ? "
                "WHERE conversation_id = ? AND revoked_at IS NULL",
                (datetime.now(UTC).isoformat(), conversation_id),
            )
            conn.commit()
            if cur.rowcount:
                _LOG.info(
                    "host credentials revoked conversation=%s count=%d",
                    conversation_id,
                    cur.rowcount,
                )
            return cur.rowcount

    def rotate(
        self,
        conversation_id: str,
        owner_id: str,
        audience: str,
        *,
        allowed_services: frozenset[str] = frozenset({"svc.ping"}),
        allowed_origins: frozenset[str] | None = None,
        kind: TokenKind = "preview",
        expires_in: timedelta | None = None,
    ) -> str:
        """Mint a candidate while every working credential remains valid.

        After delivery is verified, the caller invokes finish_rotation. A
        failed delivery revokes only the candidate and preserves the old token.
        """
        with self._lock:
            conn = self._check_open()
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    f"SELECT MAX(generation) AS generation FROM {_TABLE_NAME} "
                    "WHERE conversation_id = ? AND owner_id = ? AND audience = ?",
                    (conversation_id, owner_id, audience),
                ).fetchone()
                generation = int(row["generation"]) + 1 if row["generation"] is not None else 0
                # mint uses this same connection and commits only after its
                # INSERT, completing this allocation+insert transaction.
                token = self.mint(
                    conversation_id,
                    owner_id,
                    audience,
                    allowed_services=allowed_services,
                    allowed_origins=allowed_origins,
                    kind=kind,
                    generation=generation,
                    expires_in=expires_in,
                )
                return token
            except Exception:
                conn.rollback()
                raise

    def finish_rotation(
        self,
        conversation_id: str,
        audience: str,
        *,
        keep_selector: str,
    ) -> int:
        """Revoke strictly older generations after the candidate is proven live.

        A lower-generation finisher can race a newer candidate, so selection by
        identity alone is insufficient: it must never revoke the same or a
        newer generation. Concurrent finishes therefore converge on the newest
        candidate that successfully finishes, regardless of transaction order.
        """
        with self._lock:
            conn = self._check_open()
            now = datetime.now(UTC).isoformat()
            conn.execute("BEGIN IMMEDIATE")
            try:
                active = conn.execute(
                    f"SELECT owner_id, conversation_id, audience, generation "
                    f"FROM {_TABLE_NAME} WHERE selector = ? AND revoked_at IS NULL "
                    "AND (expires_at IS NULL OR expires_at > ?)",
                    (keep_selector, now),
                ).fetchone()
                if active is None:
                    raise ValueError("rotation candidate is not active for this app")
                owner_value = active["owner_id"]
                conversation_value = active["conversation_id"]
                audience_value = active["audience"]
                generation_value = active["generation"]
                if (
                    not isinstance(owner_value, str)
                    or not isinstance(conversation_value, str)
                    or not isinstance(audience_value, str)
                    or not owner_value
                    or not conversation_value
                    or not audience_value
                    or not isinstance(generation_value, int)
                    or isinstance(generation_value, bool)
                    or generation_value < 0
                    or conversation_value != conversation_id
                    or audience_value != audience
                ):
                    raise ValueError("rotation candidate is not active for this app")
                cur = conn.execute(
                    f"UPDATE {_TABLE_NAME} SET revoked_at = ? "
                    "WHERE conversation_id = ? AND owner_id = ? AND audience = ? "
                    "AND generation < ? AND revoked_at IS NULL",
                    (
                        now,
                        conversation_value,
                        owner_value,
                        audience_value,
                        generation_value,
                    ),
                )
                conn.commit()
                _LOG.info(
                    "host credential rotation completed conversation=%s app=%s "
                    "selector=%s generation=%d revoked=%d",
                    conversation_id,
                    audience,
                    keep_selector,
                    generation_value,
                    cur.rowcount,
                )
                return cur.rowcount
            except Exception:
                conn.rollback()
                raise

    def list_for_conversation(self, conversation_id: str) -> list[HostTokenRecord]:
        with self._lock:
            conn = self._check_open()
            rows = conn.execute(
                f"SELECT * FROM {_TABLE_NAME} WHERE conversation_id = ? ORDER BY created_at DESC",
                (conversation_id,),
            ).fetchall()
        return [self._record_from_row(dict(row)) for row in rows]

    @staticmethod
    def _record_from_row(
        row: dict[str, Any], *, expected_selector: str | None = None
    ) -> HostTokenRecord:
        selector = row["selector"]
        if (
            not isinstance(selector, str)
            or _SELECTOR_RE.fullmatch(selector) is None
            or (expected_selector is not None and selector != expected_selector)
        ):
            raise HostTokenError("invalid selector in host token store")
        identity: dict[str, str] = {}
        for field in ("conversation_id", "owner_id", "audience"):
            value = row[field]
            if not isinstance(value, str) or not value:
                raise HostTokenError("invalid credential identity in host token store")
            identity[field] = value
        kind = str(row["kind"])
        if kind not in _TOKEN_KINDS:
            raise HostTokenError("invalid credential kind in host token store")
        version_value = row["version"]
        if not isinstance(version_value, int) or isinstance(version_value, bool):
            raise HostTokenError("invalid credential version in host token store")
        version = version_value
        if version not in {_LEGACY_TOKEN_VERSION, _TOKEN_VERSION}:
            raise HostTokenError("invalid credential version in host token store")
        generation_value = row["generation"]
        if (
            not isinstance(generation_value, int)
            or isinstance(generation_value, bool)
            or generation_value < 0
        ):
            raise HostTokenError("invalid credential generation in host token store")
        services_value = json.loads(row["allowed_services"])
        if (
            not isinstance(services_value, list)
            or not services_value
            or any(not isinstance(value, str) for value in services_value)
        ):
            raise HostTokenError("invalid service scope in host token store")
        services = frozenset(services_value)
        if len(services) != len(services_value) or any(
            not valid_host_service_name(service) for service in services
        ):
            raise HostTokenError("invalid service scope in host token store")
        origins_value = json.loads(row["allowed_origins"])
        if not isinstance(origins_value, list) or any(
            not isinstance(value, str) for value in origins_value
        ):
            raise HostTokenError("invalid origin scope in host token store")
        origins = frozenset(origins_value)
        try:
            canonical_origins = _canonical_origins(origins)
        except ValueError as exc:
            raise HostTokenError("invalid origin scope in host token store") from exc
        if len(origins) != len(origins_value) or canonical_origins != origins:
            raise HostTokenError("invalid origin scope in host token store")
        created_at = HostTokenStore._stored_datetime(row["created_at"], required=True)
        expires_at = HostTokenStore._stored_datetime(row["expires_at"], required=False)
        revoked_at = HostTokenStore._stored_datetime(row["revoked_at"], required=False)
        return HostTokenRecord(
            selector=selector,
            version=version,
            conversation_id=identity["conversation_id"],
            owner_id=identity["owner_id"],
            audience=identity["audience"],
            allowed_services=services,
            allowed_origins=origins,
            kind=cast(TokenKind, kind),
            generation=generation_value,
            created_at=cast(datetime, created_at),
            expires_at=expires_at,
            revoked_at=revoked_at,
        )

    @staticmethod
    def _stored_datetime(value: object, *, required: bool) -> datetime | None:
        if value is None and not required:
            return None
        if not isinstance(value, str) or not value:
            raise HostTokenError("invalid credential timestamp in host token store")
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise HostTokenError("credential timestamp must be timezone-aware")
        return parsed

    def _check_open(self) -> sqlite3.Connection:
        if self._conn is None:
            raise TokenStoreClosed("HostTokenStore is closed")
        return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __enter__(self) -> HostTokenStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


__all__ = [
    "HostTokenError",
    "HostTokenRecord",
    "HostTokenStore",
    "TokenKind",
    "TokenStoreClosed",
]
