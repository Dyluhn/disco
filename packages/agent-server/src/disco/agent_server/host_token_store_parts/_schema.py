"""SQLite schema creation/migration for the host-service token table."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._model import _LEGACY_TOKEN_VERSION, _TABLE_NAME

if TYPE_CHECKING:
    from disco.agent_server.host_token_store import HostTokenStore


def _ensure_schema(store: HostTokenStore) -> None:
    with store._lock:
        conn = store._check_open()
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
