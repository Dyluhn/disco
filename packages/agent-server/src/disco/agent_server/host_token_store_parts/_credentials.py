"""Credential minting, verification, and wire-format parsing.

Extracted from ``HostTokenStore`` (PKG-10-SANDBOX) to keep the class body
under the architecture size budget. ``HostTokenStore.mint``/``verify``/``_parse``
remain public/compat methods — thin delegators that call these free functions
with the store instance, mirroring the ``host_proxy`` module's existing
self-as-parameter convention for extracted logic.
"""

from __future__ import annotations

import json
import logging
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from disco.core.host_services import valid_host_service_name

from ._crypto import _digest, _random_component
from ._model import (
    _DEFAULT_LIFETIMES,
    _DUMMY_DIGEST,
    _LEGACY_TOKEN_PREFIX,
    _LEGACY_TOKEN_VERSION,
    _SELECTOR_BYTES,
    _TABLE_NAME,
    _TOKEN_KINDS,
    _TOKEN_PREFIX,
    _TOKEN_RE,
    _TOKEN_VERSION,
    _VERIFIER_BYTES,
    HostTokenError,
    HostTokenRecord,
    TokenKind,
)
from ._origins import _canonical_origins
from ._records import _record_from_row

if TYPE_CHECKING:
    from disco.agent_server.host_token_store import HostTokenStore

_LOG = logging.getLogger("disco.agent_server.host_token_store")


def mint(
    store: HostTokenStore,
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
    verifier = _random_component(_VERIFIER_BYTES)
    digest = _digest(verifier)
    now = datetime.now(UTC)
    expires_at = (now + expires_in).isoformat() if expires_in is not None else None
    services_json = json.dumps(sorted(allowed_services), separators=(",", ":"))
    origins_json = json.dumps(sorted(canonical_origins), separators=(",", ":"))
    with store._lock:
        conn = store._check_open()
        for _attempt in range(8):
            selector = _random_component(_SELECTOR_BYTES)
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


def parse_versioned(token: str) -> tuple[int, str, str] | None:
    match = _TOKEN_RE.fullmatch(token)
    if match is None:
        return None
    version = _LEGACY_TOKEN_VERSION if match.group(1) == _LEGACY_TOKEN_PREFIX else _TOKEN_VERSION
    return version, match.group(2), match.group(3)


def parse(token: str) -> tuple[str, str] | None:
    """Compatibility parser for the existing Stripe live verifier."""
    parsed = parse_versioned(token)
    return (parsed[1], parsed[2]) if parsed is not None else None


def verify(store: HostTokenStore, token: str) -> HostTokenRecord | None:
    """Return an active record, or None for every auth failure."""
    with store._lock:
        store._check_open()
    parsed = parse_versioned(token)
    if parsed is None:
        return None
    version, selector, verifier = parsed
    with store._lock:
        conn = store._check_open()
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
    matched = secrets.compare_digest(_digest(verifier), expected)
    if not matched or row is None or not row_header_valid or stored_version != version:
        return None
    try:
        record = _record_from_row(dict(row), expected_selector=selector)
        return record if record.is_active else None
    except (HostTokenError, KeyError, TypeError, ValueError, OverflowError):
        # Persisted auth state is untrusted at this boundary. Corruption or
        # an incompatible row must fail like every other credential, never
        # escape into the bus as a 500 or disclose which field was invalid.
        _LOG.warning("malformed persisted host credential selector=%s", selector)
        return None
