"""Durable, scoped bearer credentials for the WO-A2.2 host-service bus.

New tokens have one exact wire shape::

    a4v1.<22-char selector>.<43-char verifier>

Only a SHA-256 digest of the verifier is persisted. Records are operational
state, not conversation events, and contain the trusted principal and complete
capability scope used by the bus. Persisted A2 ``a2v0`` records remain usable
during migration; the store never mints new v0 credentials.

This module is the public compatibility/export facade over
:mod:`disco.agent_server.host_token_store_parts`, which holds the cohesive
private implementation (schema, crypto, row marshalling, credential
minting/verification, rotation) so every public symbol keeps its import path
(``disco.agent_server.host_token_store.HostTokenStore`` and friends). The
public methods below are thin delegators onto that implementation, passing
``self`` explicitly — the same convention already used by
``disco.agent_server.host_proxy`` for its own extracted helpers.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import timedelta
from pathlib import Path

from .host_token_store_parts import _credentials, _crypto, _rotation, _schema
from .host_token_store_parts._model import HostTokenError as HostTokenError
from .host_token_store_parts._model import HostTokenRecord as HostTokenRecord
from .host_token_store_parts._model import TokenKind as TokenKind
from .host_token_store_parts._model import TokenStoreClosed as TokenStoreClosed


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
        _schema._ensure_schema(self)

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
        return _credentials.mint(
            self,
            conversation_id,
            owner_id,
            audience,
            allowed_services=allowed_services,
            allowed_origins=allowed_origins,
            kind=kind,
            generation=generation,
            expires_in=expires_in,
        )

    def verify(self, token: str) -> HostTokenRecord | None:
        """Return an active record, or None for every auth failure."""
        return _credentials.verify(self, token)

    @staticmethod
    def _parse(token: str) -> tuple[str, str] | None:
        """Compatibility parser for the existing Stripe live verifier."""
        return _credentials.parse(token)

    @staticmethod
    def _digest(verifier: str) -> bytes:
        return _crypto._digest(verifier)

    def revoke(self, selector: str) -> bool:
        return _rotation.revoke(self, selector)

    def revoke_for_conversation(self, conversation_id: str) -> int:
        return _rotation.revoke_for_conversation(self, conversation_id)

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
        return _rotation.rotate(
            self,
            conversation_id,
            owner_id,
            audience,
            allowed_services=allowed_services,
            allowed_origins=allowed_origins,
            kind=kind,
            expires_in=expires_in,
        )

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
        return _rotation.finish_rotation(
            self, conversation_id, audience, keep_selector=keep_selector
        )

    def list_for_conversation(self, conversation_id: str) -> list[HostTokenRecord]:
        return _rotation.list_for_conversation(self, conversation_id)

    def _check_open(self) -> sqlite3.Connection:
        if self._conn is None:
            raise TokenStoreClosed("HostTokenStore is closed")
        return self._conn

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
