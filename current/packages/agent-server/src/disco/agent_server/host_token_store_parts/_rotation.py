"""Revocation, generation rotation, and per-conversation listing.

Extracted from ``HostTokenStore`` (PKG-10-SANDBOX) to keep the class body
under the architecture size budget. The public methods remain thin
delegators on ``HostTokenStore`` that call these free functions with the
store instance.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from ._model import _TABLE_NAME, HostTokenRecord, TokenKind
from ._records import _record_from_row

if TYPE_CHECKING:
    from disco.agent_server.host_token_store import HostTokenStore

_LOG = logging.getLogger("disco.agent_server.host_token_store")


def revoke(store: HostTokenStore, selector: str) -> bool:
    with store._lock:
        conn = store._check_open()
        cur = conn.execute(
            f"UPDATE {_TABLE_NAME} SET revoked_at = ? "
            "WHERE selector = ? AND revoked_at IS NULL",
            (datetime.now(UTC).isoformat(), selector),
        )
        conn.commit()
        if cur.rowcount > 0:
            _LOG.info("host credential revoked selector=%s", selector)
        return cur.rowcount > 0


def revoke_for_conversation(store: HostTokenStore, conversation_id: str) -> int:
    with store._lock:
        conn = store._check_open()
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
    store: HostTokenStore,
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
    with store._lock:
        conn = store._check_open()
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
            token = store.mint(
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
    store: HostTokenStore,
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
    with store._lock:
        conn = store._check_open()
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


def list_for_conversation(store: HostTokenStore, conversation_id: str) -> list[HostTokenRecord]:
    with store._lock:
        conn = store._check_open()
        rows = conn.execute(
            f"SELECT * FROM {_TABLE_NAME} WHERE conversation_id = ? ORDER BY created_at DESC",
            (conversation_id,),
        ).fetchall()
    return [_record_from_row(dict(row)) for row in rows]
