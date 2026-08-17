"""MCP approval migrations — RP-05 rung A.

The mcp_approvals table lives in the same SQLite DB as the events/state store
(mirroring the share_tokens pattern from RP-06). This module provides the SQL
for the table creation and the store-level methods for reading/writing
approval rows.

The three MCP approval CREATE statements live in one Core source location only
(``disco.core.store.schema``); this module is a thin compatibility delegate so
bare connections and store-created schemas are identical. Core must not import
Tools; Tools imports Core.

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
from typing import Any, Protocol, runtime_checkable

from disco.core.store.schema import ensure_mcp_approval_tables as _core_ensure_mcp_approval_tables


@runtime_checkable
class _ApprovalConn(Protocol):
    """Minimal SQLite-compatible connection with function registration.

    Persistent downgrade guards require each writer connection to publish its
    schema version, so wrappers and test fakes must forward ``create_function``
    in addition to ``execute`` + ``commit``. Keeps the module free of an import on
    sqlite3 (so the helpers stay trivial to call from places that already
    have a different connection type)."""

    # `params` is positional-only (`/`) so a real sqlite3.Connection — whose
    # 2nd execute() arg is named `parameters`, not `params` — structurally
    # satisfies this Protocol. Every call site below passes params positionally.
    def execute(self, sql: str, params: tuple[Any, ...] = ..., /) -> Any: ...
    def create_function(
        self,
        name: str,
        narg: int,
        func: Any,
        *,
        deterministic: bool = ...,
    ) -> None: ...
    def commit(self) -> None: ...


def ensure_mcp_approval_tables(conn: _ApprovalConn) -> None:
    """Create both approval ledgers for old databases and lightweight tests.

    Thin compatibility delegate to ``disco.core.store.schema`` so the three
    MCP CREATE statements live in one Core source location only. Bare
    connections and store-created schemas are identical.
    """
    _core_ensure_mcp_approval_tables(conn)


def create_mcp_approval(
    conn: _ApprovalConn,
    server: str,
    description_hash: str,
    *,
    approved_by: str = "operator",
) -> None:
    ensure_mcp_approval_tables(conn)
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO mcp_approvals "
        "(server, description_hash, approved_at, approved_by) "
        "VALUES (?, ?, ?, ?)",
        (server, description_hash, now, approved_by),
    )
    delete_mcp_approval_pending(conn, server)
    conn.commit()


def delete_mcp_approval(conn: _ApprovalConn, server: str) -> None:
    ensure_mcp_approval_tables(conn)
    conn.execute("DELETE FROM mcp_approvals WHERE server = ?", (server,))
    # E6: any drift row for the server is stale the moment the approval row
    # is gone — the next startup will recompute the new_hash from scratch.
    delete_mcp_approval_pending(conn, server)
    conn.commit()


def get_mcp_approval(conn: _ApprovalConn, server: str) -> dict | None:
    ensure_mcp_approval_tables(conn)
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


def list_mcp_approvals(conn: _ApprovalConn) -> list[dict]:
    ensure_mcp_approval_tables(conn)
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
    conn: _ApprovalConn,
    server: str,
    old_hash: str,
    new_hash: str,
) -> None:
    """Record a drift: the live pool saw `new_hash` for `server` but the
    stored approval is `old_hash`. Idempotent — overwrites on repeat writes
    (e.g. agent-server restart re-detects the same drift)."""
    ensure_mcp_approval_tables(conn)
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO mcp_approval_pending "
        "(server, old_hash, new_hash, detected_at) "
        "VALUES (?, ?, ?, ?)",
        (server, old_hash, new_hash, now),
    )
    conn.commit()


def delete_mcp_approval_pending(conn: _ApprovalConn, server: str) -> None:
    """Clear a drift row — called by the app-server on POST /approve (the
    operator has just accepted the new descriptions) and by
    delete_mcp_approval (the server was removed entirely)."""
    conn.execute("DELETE FROM mcp_approval_pending WHERE server = ?", (server,))


def get_mcp_approval_pending(conn: _ApprovalConn, server: str) -> dict | None:
    ensure_mcp_approval_tables(conn)
    row = conn.execute(
        "SELECT server, old_hash, new_hash, detected_at FROM mcp_approval_pending WHERE server = ?",
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


def list_mcp_approval_pending(conn: _ApprovalConn) -> list[dict]:
    ensure_mcp_approval_tables(conn)
    rows = conn.execute(
        "SELECT server, old_hash, new_hash, detected_at FROM mcp_approval_pending ORDER BY server"
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


def create_mcp_config_approval(
    conn: _ApprovalConn,
    server: str,
    config_hash: str,
    *,
    approved_by: str = "operator",
) -> None:
    ensure_mcp_approval_tables(conn)
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO mcp_config_approvals "
        "(server, config_hash, approved_at, approved_by) VALUES (?, ?, ?, ?)",
        (server, config_hash, now, approved_by),
    )
    conn.commit()


def delete_mcp_config_approval(conn: _ApprovalConn, server: str) -> None:
    ensure_mcp_approval_tables(conn)
    conn.execute("DELETE FROM mcp_config_approvals WHERE server = ?", (server,))
    conn.commit()


def get_mcp_config_approval(conn: _ApprovalConn, server: str) -> dict | None:
    ensure_mcp_approval_tables(conn)
    row = conn.execute(
        "SELECT server, config_hash, approved_at, approved_by "
        "FROM mcp_config_approvals WHERE server = ?",
        (server,),
    ).fetchone()
    if row is None:
        return None
    return {
        "server": row[0],
        "config_hash": row[1],
        "approved_at": row[2],
        "approved_by": row[3],
    }


def list_mcp_config_approvals(conn: _ApprovalConn) -> list[dict]:
    ensure_mcp_approval_tables(conn)
    rows = conn.execute(
        "SELECT server, config_hash, approved_at, approved_by "
        "FROM mcp_config_approvals ORDER BY server"
    ).fetchall()
    return [
        {
            "server": row[0],
            "config_hash": row[1],
            "approved_at": row[2],
            "approved_by": row[3],
        }
        for row in rows
    ]
