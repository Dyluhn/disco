"""MCP approval migrations — RP-05 rung A.

The mcp_approvals table lives in the same SQLite DB as the events/state store
(mirroring the share_tokens pattern from RP-06). This module provides the SQL
for the table creation and the store-level methods for reading/writing
approval rows.
"""

from __future__ import annotations

from datetime import UTC, datetime

_MCP_APPROVALS_SQL = """CREATE TABLE IF NOT EXISTS mcp_approvals (
    server          TEXT PRIMARY KEY,
    description_hash TEXT NOT NULL,
    approved_at     TEXT NOT NULL,
    approved_by     TEXT NOT NULL
);
"""


def create_mcp_approval(
    conn: object,
    server: str,
    description_hash: str,
    *,
    approved_by: str = "operator",
) -> None:
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO mcp_approvals "
        "(server, description_hash, approved_at, approved_by) "
        "VALUES (?, ?, ?, ?)",
        (server, description_hash, now, approved_by),
    )
    conn.commit()


def delete_mcp_approval(conn: object, server: str) -> None:
    conn.execute("DELETE FROM mcp_approvals WHERE server = ?", (server,))
    conn.commit()


def get_mcp_approval(conn: object, server: str) -> dict | None:
    row = conn.execute(
        "SELECT server, description_hash, approved_at, approved_by "
        "FROM mcp_approvals WHERE server = ?",
        (server,),
    ).fetchone()
    if row is None:
        return None
    return {
        "server": row[0],
        "description_hash": row[1],
        "approved_at": row[2],
        "approved_by": row[3],
    }


def list_mcp_approvals(conn: object) -> list[dict]:
    rows = conn.execute(
        "SELECT server, description_hash, approved_at, approved_by "
        "FROM mcp_approvals ORDER BY server"
    ).fetchall()
    return [
        {
            "server": row[0],
            "description_hash": row[1],
            "approved_at": row[2],
            "approved_by": row[3],
        }
        for row in rows
    ]
