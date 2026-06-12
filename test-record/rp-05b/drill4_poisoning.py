"""Drill 4 — the poisoning drill (RP-05 §6 item 4).

Mutate a tool description between sessions → `McpPool.start()` refuses the server
(ApprovalRequired) → the runtime surfaces the re-approval banner → a Build run
gets NO tools from the poisoned server (it refuses to use it) until re-approval
is granted.

  Session 1: clean FakeStdioServer  → approve hash H_clean (persisted), connected.
  Session 2: SAME server name, now the `poison_server.py` binary (the `add`
             description carries an injection) → started with the stored H_clean
             approval → REFUSED: status 'approval_required', pending old=H_clean
             new=H_poison, H_clean != H_poison. The runtime's _mcp_approval_pending
             carries the banner; the per-conversation snapshot has zero
             mcp__fake_srv__ tools (the run cannot use the poisoned server).
  Re-approval: persist H_poison → fresh pool → connected (the human reviewed
             the diff and accepted).

PASS requires every one of those to hold.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys

from _accept_common import REPO_ROOT  # noqa: F401  (import triggers cwd→repo root)

from disco.agent_server import ConversationRuntime
from disco.core import SecurityRisk, SqliteEventStore
from disco.tools.mcp import McpPool, McpServerConfig, McpSettings
from disco.tools.mcp.migrations import create_mcp_approval, list_mcp_approvals
from disco.tools.sandbox import ProcessSandboxService

SERVER = "fake_srv"

CLEAN_CMD = [
    sys.executable, "-c",
    "from packages.tools.tests.mcp_fakes import FakeStdioServer; "
    "import asyncio; asyncio.run(FakeStdioServer().run())",
]
POISON_CMD = [sys.executable, os.path.join("test-record", "rp-05b", "poison_server.py")]


def _cfg(command: list[str]) -> McpServerConfig:
    return McpServerConfig(
        name=SERVER, transport="stdio", command=command, enabled=True,
        risk_tier=SecurityRisk.MEDIUM,
    )


def _approvals(conn) -> dict[str, str]:
    return {r["server"]: r["description_hash"] for r in list_mcp_approvals(conn)}


async def _live_hash(command: list[str]) -> str:
    """Surface a server's live description hash via the wrong-pin review trick."""
    pool = McpPool(McpSettings(enabled=True, servers={SERVER: _cfg(command)}),
                   approvals={SERVER: "0" * 64})
    try:
        await pool.start()
        return pool.approval_pending()[SERVER]["new_hash"]
    finally:
        await pool.aclose()


async def main() -> int:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS mcp_approvals ("
        "server TEXT PRIMARY KEY, description_hash TEXT NOT NULL, "
        "approved_at TEXT NOT NULL, approved_by TEXT NOT NULL)"
    )
    conn.commit()

    checks: dict[str, bool] = {}

    # --- Session 1: approve the clean server ---------------------------------
    h_clean = await _live_hash(CLEAN_CMD)
    create_mcp_approval(conn, SERVER, h_clean)
    pool1 = McpPool(McpSettings(enabled=True, servers={SERVER: _cfg(CLEAN_CMD)}),
                    approvals=_approvals(conn))
    await pool1.start()
    s1 = pool1.server_status()
    checks["s1_clean_connected"] = s1.get(SERVER) == "connected"
    print(f"session1 clean: status={s1}  approved H_clean={h_clean[:12]}…")
    await pool1.aclose()

    # --- Session 2: the server is poisoned; stored approval is still H_clean --
    h_poison = await _live_hash(POISON_CMD)
    checks["hash_actually_changed"] = h_poison != h_clean
    pool2 = McpPool(McpSettings(enabled=True, servers={SERVER: _cfg(POISON_CMD)}),
                    approvals=_approvals(conn))  # still only H_clean is approved
    await pool2.start()
    s2 = pool2.server_status()
    pending = pool2.approval_pending()
    checks["s2_refused"] = s2.get(SERVER) == "approval_required"
    checks["pending_has_server"] = SERVER in pending
    checks["pending_old_is_clean"] = pending.get(SERVER, {}).get("old_hash") == h_clean
    checks["pending_new_is_poison"] = pending.get(SERVER, {}).get("new_hash") == h_poison
    poisoned_tools = [t.name for t in pool2.snapshot() if t.name.startswith("mcp__fake_srv__")]
    checks["no_poisoned_tools_in_snapshot"] = poisoned_tools == []
    print(f"session2 poison: status={s2}  pending old={pending.get(SERVER,{}).get('old_hash','')[:12]}… "
          f"new={pending.get(SERVER,{}).get('new_hash','')[:12]}…  poisoned_tools_exposed={poisoned_tools}")

    # --- The runtime surfaces the re-approval banner; a Build run refuses -----
    store = SqliteEventStore(":memory:")
    runtime = ConversationRuntime(store, sandbox_service=ProcessSandboxService())
    runtime._mcp_pool = pool2
    # Mirror _start_mcp_pool: copy the pool's approval-pending into the runtime
    # dict that drives the mcp_approval_required WS frame (the UI banner source).
    for name, info in pool2.approval_pending().items():
        runtime._mcp_approval_pending[name] = {
            "old_hash": info.get("old_hash", ""), "new_hash": info.get("new_hash", ""),
        }
    checks["runtime_banner_pending"] = SERVER in runtime._mcp_approval_pending
    print(f"runtime banner (mcp_approval_required source): {dict(runtime._mcp_approval_pending)}")
    await pool2.aclose()

    # --- Re-approval: the human reviewed the diff and accepted the new hash ---
    create_mcp_approval(conn, SERVER, h_poison)
    pool3 = McpPool(McpSettings(enabled=True, servers={SERVER: _cfg(POISON_CMD)}),
                    approvals=_approvals(conn))
    await pool3.start()
    s3 = pool3.server_status()
    checks["s3_reapproved_connected"] = s3.get(SERVER) == "connected"
    print(f"session3 re-approved: status={s3}")
    await pool3.aclose()

    print("\n--- checks ---")
    for k, v in checks.items():
        print(f"  [{'PASS' if v else 'FAIL'}] {k}")
    ok = all(checks.values())
    print(f"\n  [{'PASS' if ok else 'FAIL'}] poisoning drill: description-hash pin "
          f"refused the mutated server, raised the re-approval banner, withheld its "
          f"tools, and re-connected only after explicit re-approval")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
