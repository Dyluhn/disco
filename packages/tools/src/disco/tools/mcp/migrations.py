"""MCP approval migrations — RP-05 rung A.

The mcp_approvals table lives in the same SQLite DB as the events/state store
(mirroring the share_tokens pattern from RP-06). This module provides the SQL
for the table creation and the store-level methods for reading/writing
approval rows.

The companion mcp_approval_pending table records, per-server, the *new*
description_hash the agent-server's live pool computed at startup when it
detected drift against the stored mcp_approvals row. The app-server reads
this on GET /api/mcp to surface the REAL new_hash on the approval-diff UI;
without it the UI sees only the stored (old) hash from mcp_approvals. The
agent-server writes the row at startup when ApprovalRequired fires; the
app-server clears it on POST /api/mcp/servers/{name}/approve (the operator
has just accepted the new tool descriptions).
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

# E6 (#10): the drift table the agent-server writes + the app-server reads.
# Distinct from mcp_approvals: mcp_approvals holds the LAST APPROVED hash
# (operator intent); mcp_approval_pending holds the CURRENT hash the live
# pool saw (the one waiting for re-approval). They are intentionally
# independent rows — the pending row can exist only while the two hashes
# disagree.
_MCP_APPROVAL_PENDING_SQL = """CREATE TABLE IF NOT EXISTS mcp_approval_pending (
    server          TEXT PRIMARY KEY,
    old_hash        TEXT NOT NULL,
    new_hash        TEXT NOT NULL,
    detected_at     TEXT NOT NULL
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
    # E6: any drift row for the server is stale the moment the approval row
    # is gone — the next startup will recompute the new_hash from scratch.
    delete_mcp_approval_pending(conn, server)
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


# ---- E6: drift rows (mcp_approval_pending) ---------------------------------


def set_mcp_approval_pending(
    conn: object,
    server: str,
    old_hash: str,
    new_hash: str,
) -> None:
    """Record a drift: the live pool saw `new_hash` for `server` but the
    stored approval is `old_hash`. Idempotent — overwrites on repeat writes
    (e.g. agent-server restart re-detects the same drift)."""
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO mcp_approval_pending "
        "(server, old_hash, new_hash, detected_at) "
        "VALUES (?, ?, ?, ?)",
        (server, old_hash, new_hash, now),
    )
    conn.commit()


def delete_mcp_approval_pending(conn: object, server: str) -> None:
    """Clear a drift row — called by the app-server on POST /approve (the
    operator has just accepted the new descriptions) and by
    delete_mcp_approval (the server was removed entirely)."""
    conn.execute(
        "DELETE FROM mcp_approval_pending WHERE server = ?", (server,)
    )


def get_mcp_approval_pending(conn: object, server: str) -> dict | None:
    row = conn.execute(
        "SELECT server, old_hash, new_hash, detected_at "
        "FROM mcp_approval_pending WHERE server = ?",
        (server,),
    ).fetchone()
    if row is None:
        return None
    return {
        "server": row[0],
        "old_hash": row[1],
        "new_hash": row[2],
        "detected_at": row[3],
    }


def list_mcp_approval_pending(conn: object) -> list[dict]:
    rows = conn.execute(
        "SELECT server, old_hash, new_hash, detected_at "
        "FROM mcp_approval_pending ORDER BY server"
    ).fetchall()
    return [
        {
            "server": row[0],
            "old_hash": row[1],
            "new_hash": row[2],
            "detected_at": row[3],
        }
        for row in rows
    ]
