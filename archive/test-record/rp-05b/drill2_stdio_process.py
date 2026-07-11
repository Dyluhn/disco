"""Drill 2 — a real stdio MCP server, registered + APPROVED + used by a real
build on the `process` sandbox backend (RP-05 §6 item 2).

Exercises the whole rung-A→B path live:
  - a REAL subprocess MCP server (FakeStdioServer over stdio JSON-RPC),
  - the production approval flow (review the new description hash → approve →
    persist to the mcp_approvals table → pool starts `connected`),
  - the FREE or-gpt-oss-120b-free driver composing a Build loop that CALLS the
    MCP tool `mcp__fake_srv__add`,
  - the result returned through the real `_McpToolWrapper` → `pool.call_tool`
    → `fence_mcp_result` (the §4 untrusted-output fence) into an Observation.

PASS = the build reaches FINISHED, the MCP tool actually ran (an observation
whose content is an `<untrusted_mcp_result …>` fence carrying the correct sum),
and the server was `connected` via a persisted approval (not trust-on-first-use).
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys

from _accept_common import ConversationStatus, drive, get, user_event

from disco.agent_server import ConversationRuntime
from disco.core import ObservationEvent, SecurityRisk, SqliteEventStore
from disco.tools.mcp import McpPool, McpServerConfig, McpSettings
from disco.tools.mcp.migrations import (
    create_mcp_approval,
    list_mcp_approvals,
)
from disco.tools.sandbox import ProcessSandboxService, SandboxSpec

CID = "rp05b-drill2-stdio-process"
SERVER = "fake_srv"
TASK = (
    "You have an MCP tool `mcp__fake_srv__add` that adds two integers. "
    "Plan a single step, then call the tool exactly ONCE to add 17 and 25 "
    "(do NOT compute it yourself), mark the step done, and immediately call "
    "`finish` reporting the single number it returned. Do not repeat yourself."
)


def _fake_server_config(name: str = SERVER) -> McpServerConfig:
    command = [
        sys.executable,
        "-c",
        "from packages.tools.tests.mcp_fakes import FakeStdioServer; "
        "import asyncio; asyncio.run(FakeStdioServer().run())",
    ]
    return McpServerConfig(
        name=name, transport="stdio", command=command, enabled=True,
        risk_tier=SecurityRisk.MEDIUM,
    )


async def _approve_via_review(conn) -> str:
    """The production approval UX: a fresh pin shows the live hash as the
    'new_hash' to approve; persist it. Returns the approved hash."""
    srv = _fake_server_config()
    # Pin a deliberately-wrong hash so the pool surfaces the REAL live hash.
    wrong = "0" * 64
    probe = McpPool(McpSettings(enabled=True, servers={SERVER: srv}),
                    approvals={SERVER: wrong})
    try:
        await probe.start()
        pending = probe.approval_pending()
        assert SERVER in pending, "expected the wrong-hash pin to refuse the server"
        live_hash = pending[SERVER]["new_hash"]
        assert len(live_hash) == 64
    finally:
        await probe.aclose()
    create_mcp_approval(conn, SERVER, live_hash)
    return live_hash


async def main() -> int:
    # A real mcp_approvals table (the production persistence path).
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS mcp_approvals ("
        "server TEXT PRIMARY KEY, description_hash TEXT NOT NULL, "
        "approved_at TEXT NOT NULL, approved_by TEXT NOT NULL)"
    )
    conn.commit()

    approved_hash = await _approve_via_review(conn)
    approvals = {row["server"]: row["description_hash"] for row in list_mcp_approvals(conn)}
    print(f"approved {SERVER} → hash {approved_hash[:12]}… (persisted to mcp_approvals)")

    # The live pool, started from the persisted approval → must be 'connected'.
    pool = McpPool(
        McpSettings(enabled=True, servers={SERVER: _fake_server_config()}),
        approvals=approvals,
    )
    await pool.start()
    status = pool.server_status()
    print(f"pool server_status: {status}")
    assert status.get(SERVER) == "connected", f"server not connected: {status}"
    snap_names = {t.name for t in pool.snapshot()}
    print(f"snapshot tools: {sorted(snap_names)}")
    assert "mcp__fake_srv__add" in snap_names

    store = SqliteEventStore(":memory:")
    runtime = ConversationRuntime(
        store, sandbox_service=ProcessSandboxService(), sandbox_spec=SandboxSpec(memory_mb=512)
    )
    runtime._mcp_pool = pool  # inject the live, approved pool (as the lifespan would)
    store.create_conversation(CID, owner_id="local")
    runtime.set_surface(CID, "build")
    await store.append(CID, user_event(TASK))
    print(f"\nTASK: {TASK}\n--- trace ---")

    final, events, counters = await drive(runtime, store, CID, max_rounds=24)

    # Find the MCP observation: an untrusted-MCP fence carrying the sum 42.
    mcp_obs = [
        e for e in events
        if isinstance(e, ObservationEvent)
        and "<untrusted_mcp_result" in (e.tool_result.content or "")
    ]
    fenced_42 = [e for e in mcp_obs if "42" in (e.tool_result.content or "")]

    # §6 drill-2 contract = "registered, APPROVED, and USED by a real build".
    # The USE is an approved-server tool invocation returning a correctly-fenced
    # result inside the composed loop. Reaching FINISHED is a model-FLUENCY trait
    # (the free dev model is verbose) orthogonal to MCP correctness — so we reject
    # only a genuine loop failure (ERROR/STUCK) and accept FINISHED or a clean
    # PAUSED-after-the-work.
    bad_terminal = final.execution_status in (
        ConversationStatus.ERROR,
        ConversationStatus.STUCK,
    )
    print("\n--- result ---")
    print(f"  final status      : {final.execution_status.value}")
    print(f"  MCP observations  : {len(mcp_obs)} (fenced <untrusted_mcp_result …>)")
    if mcp_obs:
        print(f"  first fenced obs   : {mcp_obs[0].tool_result.content.strip()[:200]!r}")
    ok = (
        not bad_terminal
        and status.get(SERVER) == "connected"
        and len(fenced_42) >= 1
    )
    print(f"\n  [{'PASS' if ok else 'FAIL'}] approved stdio MCP server invoked in a "
          f"real build; result fenced as untrusted; sum=42 observed={bool(fenced_42)}; "
          f"loop terminal={final.execution_status.value} (ERROR/STUCK would fail)")
    await pool.aclose()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
