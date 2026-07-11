"""Durable, scoped bearer credentials for the WO-A2.2 host-service bus.

Tokens have one exact wire shape::

    a2v0.<22-char selector>.<43-char verifier>

Only a SHA-256 digest of the verifier is persisted. Records are operational
state, not conversation events, and contain the trusted principal and complete
capability scope used by the bus.
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

_TOKEN_PREFIX = "a2v0"
_SELECTOR_BYTES = 16
_VERIFIER_BYTES = 32
_TOKEN_RE = re.compile(r"^a2v0\.([A-Za-z0-9_-]{22})\.([A-Za-z0-9_-]{43})$")
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
                        (selector, verifier_digest, conversation_id, owner_id, audience,
                         allowed_services, allowed_origins, kind, generation,
                         created_at, expires_at, revoked_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                        """,
                        (
                            selector,
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
        parsed = self._parse(token)
        if parsed is None:
            return None
        selector, verifier = parsed
        with self._lock:
            conn = self._check_open()
            row = conn.execute(
                f"SELECT * FROM {_TABLE_NAME} WHERE selector = ?",
                (selector,),
            ).fetchone()
        expected = bytes(row["verifier_digest"]) if row is not None else _DUMMY_DIGEST
        matched = secrets.compare_digest(self._digest(verifier), expected)
        if not matched or row is None:
            return None
        record = self._record_from_row(dict(row))
        return record if record.is_active else None

    @staticmethod
    def _parse(token: str) -> tuple[str, str] | None:
        match = _TOKEN_RE.fullmatch(token)
        if match is None:
            return None
        return match.group(1), match.group(2)

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
        generation: int = 0,
        expires_in: timedelta | None = None,
    ) -> str:
        """Mint a candidate while every working credential remains valid.

        After delivery is verified, the caller invokes finish_rotation. A
        failed delivery revokes only the candidate and preserves the old token.
        """
        return self.mint(
            conversation_id,
            owner_id,
            audience,
            allowed_services=allowed_services,
            allowed_origins=allowed_origins,
            kind=kind,
            generation=generation,
            expires_in=expires_in,
        )

    def finish_rotation(
        self,
        conversation_id: str,
        audience: str,
        *,
        keep_selector: str,
    ) -> int:
        """Revoke older credentials only after the candidate is proven live."""
        with self._lock:
            conn = self._check_open()
            now = datetime.now(UTC).isoformat()
            conn.execute("BEGIN IMMEDIATE")
            try:
                active = conn.execute(
                    f"SELECT 1 FROM {_TABLE_NAME} WHERE selector = ? "
                    "AND conversation_id = ? AND audience = ? "
                    "AND revoked_at IS NULL "
                    "AND (expires_at IS NULL OR expires_at > ?)",
                    (keep_selector, conversation_id, audience, now),
                ).fetchone()
                if active is None:
                    raise ValueError("rotation candidate is not active for this app")
                cur = conn.execute(
                    f"UPDATE {_TABLE_NAME} SET revoked_at = ? "
                    "WHERE conversation_id = ? AND audience = ? "
                    "AND selector <> ? AND revoked_at IS NULL",
                    (now, conversation_id, audience, keep_selector),
                )
                conn.commit()
                _LOG.info(
                    "host credential rotation completed conversation=%s app=%s "
                    "selector=%s revoked=%d",
                    conversation_id,
                    audience,
                    keep_selector,
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
    def _record_from_row(row: dict[str, Any]) -> HostTokenRecord:
        kind = str(row["kind"])
        if kind not in _TOKEN_KINDS:
            raise HostTokenError("invalid credential kind in host token store")
        return HostTokenRecord(
            selector=str(row["selector"]),
            conversation_id=str(row["conversation_id"]),
            owner_id=str(row["owner_id"]),
            audience=str(row["audience"]),
            allowed_services=frozenset(json.loads(row["allowed_services"])),
            allowed_origins=frozenset(json.loads(row["allowed_origins"])),
            kind=cast(TokenKind, kind),
            generation=int(row["generation"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            expires_at=(datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None),
            revoked_at=(datetime.fromisoformat(row["revoked_at"]) if row["revoked_at"] else None),
        )

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
