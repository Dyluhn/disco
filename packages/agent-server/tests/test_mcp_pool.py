"""MCP pool integration tests — RP-05 rung A (agent-server level).

Covers: pool start, snapshot, name round-trip, approval mismatch,
init timeout, stderr capture, lifecycle
teardown, D1 production approval path, D5 tool invocation.
Uses the FakeStdioServer via McpPool + McpStdioClient.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest
from disco.core import SecurityRisk
from disco.tools.mcp.approval import (
    compute_config_hash,
    compute_description_hash,
)
from disco.tools.mcp.config import McpServerConfig, McpSettings
from disco.tools.mcp.migrations import (
    create_mcp_approval,
    create_mcp_config_approval,
    list_mcp_approvals,
    list_mcp_config_approvals,
)
from disco.tools.mcp.pool import McpPool

from packages.tools.tests.mcp_fakes import FAKE_TOOL_DESCRIPTORS

# The uv workspace root moved under current/ in the three-bucket restructure,
# so a spawned interpreter no longer finds `packages` on its path via cwd.
_WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
_PATH_BOOTSTRAP = f"import sys; sys.path.insert(0, {str(_WORKSPACE_ROOT)!r}); "


def _fake_stdio_server_config(
    name: str = "fake_srv",
    command: list[str] | None = None,
) -> McpServerConfig:
    """Build an McpServerConfig for the FakeStdioServer."""
    if command is None:
        command = [
            sys.executable,
            "-c",
            _PATH_BOOTSTRAP + "from packages.tools.tests.mcp_fakes import FakeStdioServer; "
            "import asyncio; asyncio.run(FakeStdioServer().run())",
        ]
    return McpServerConfig(
        name=name,
        transport="stdio",
        command=command,
        risk_tier=SecurityRisk.MEDIUM,
        enabled=True,
    )


def _make_pool(
    servers: dict[str, McpServerConfig] | None = None,
    approvals: dict[str, str] | None = None,
    config_approvals: dict[str, str] | None = None,
) -> McpPool:
    """Build a pool whose ordinary fixtures are explicitly approved.

    Passing an empty mapping is distinct from omitting one and is used by
    refusal tests. Production remains fail-closed when either ledger is empty.
    """
    actual_servers = servers or {}
    if approvals is None:
        approvals = {
            name: compute_description_hash(FAKE_TOOL_DESCRIPTORS)
            for name, srv in actual_servers.items()
            if srv.transport == "stdio"
        }
    if config_approvals is None:
        config_approvals = {name: compute_config_hash(srv) for name, srv in actual_servers.items()}
    settings = McpSettings(enabled=True, servers=actual_servers)
    return McpPool(
        settings,
        approvals=approvals,
        config_approvals=config_approvals,
    )


@pytest.mark.asyncio
async def test_pool_start_with_fake_server():
    """Pool.start() connects to a FakeStdioServer and builds ToolDefs."""
    srv = _fake_stdio_server_config()
    pool = _make_pool({"fake_srv": srv})
    try:
        await pool.start()
        assert pool.started is True
        snapshot = pool.snapshot()
        names = {t.name for t in snapshot}
        assert "mcp__fake_srv__echo" in names
        assert "mcp__fake_srv__add" in names
        assert "mcp__fake_srv__read_file" in names
        assert "mcp__fake_srv__list_files" in names
        assert {t.runs_in for t in snapshot} == {"in_process"}
    finally:
        await pool.aclose()


@pytest.mark.asyncio
async def test_pool_snapshot_returns_copy():
    """snapshot() returns a COPY — mutating the returned list doesn't
    affect the pool."""
    srv = _fake_stdio_server_config()
    pool = _make_pool({"fake_srv": srv})
    try:
        await pool.start()
        snap1 = pool.snapshot()
        orig_len = len(snap1)
        snap1.clear()  # mutate the copy
        snap2 = pool.snapshot()  # pool is untouched
        assert len(snap2) == orig_len
    finally:
        await pool.aclose()


@pytest.mark.asyncio
async def test_pool_server_status():
    """server_status() reflects connected/disabled states."""
    srv = _fake_stdio_server_config()
    pool = _make_pool({"fake_srv": srv})
    try:
        await pool.start()
        status = pool.server_status()
        assert status["fake_srv"] == "connected"
        # A healthy server carries no diagnostic entry at all — not an empty
        # stub — same convention the streamable_http transport uses.
        assert pool.server_diagnostics() == {}
    finally:
        await pool.aclose()


@pytest.mark.asyncio
async def test_pool_disabled_server_skipped():
    """A disabled server is not connected."""
    srv = _fake_stdio_server_config()
    srv = srv.model_copy(update={"enabled": False})
    pool = _make_pool({"fake_srv": srv})
    try:
        await pool.start()
        status = pool.server_status()
        assert status["fake_srv"] == "disabled"
        assert len(pool.snapshot()) == 0
    finally:
        await pool.aclose()


@pytest.mark.asyncio
async def test_pool_start_is_idempotent():
    """Calling start() twice does not reconnect."""
    srv = _fake_stdio_server_config()
    pool = _make_pool({"fake_srv": srv})
    try:
        await pool.start()
        snap1 = pool.snapshot()
        await pool.start()  # should be a no-op
        snap2 = pool.snapshot()
        assert len(snap1) == len(snap2)
    finally:
        await pool.aclose()


@pytest.mark.asyncio
async def test_pool_approval_mismatch_marks_server():
    """When an approval hash is stored but doesn't match, the server is
    marked 'approval_required' and the pool still starts (D1: per-server
    refusal, not pool-wide crash). Other servers are unaffected."""
    srv = _fake_stdio_server_config(name="bad_srv")
    # Pre-store a wrong approval hash
    pool = _make_pool(
        {"bad_srv": srv},
        approvals={"bad_srv": "0000000000000000000000000000000000000000000000000000000000000000"},
    )
    try:
        await pool.start()
        # Pool should still be started (other servers unaffected)
        assert pool.started is True
        # The mismatched server is marked approval_required
        status = pool.server_status()
        assert status["bad_srv"] == "approval_required"
        # The server's tools are NOT in the snapshot
        snapshot_names = {t.name for t in pool.snapshot()}
        assert len(snapshot_names) == 0
        # Approval pending info is available
        pending = pool.approval_pending()
        assert "bad_srv" in pending
        assert (
            pending["bad_srv"]["old_hash"]
            == "0000000000000000000000000000000000000000000000000000000000000000"
        )
        assert len(pending["bad_srv"]["new_hash"]) == 64
    finally:
        await pool.aclose()


@pytest.mark.asyncio
async def test_pool_approval_match_does_not_raise():
    """When the stored approval hash matches the current tools, start
    proceeds and the server is 'connected'."""
    srv = _fake_stdio_server_config()
    expected_hash = compute_description_hash(FAKE_TOOL_DESCRIPTORS)

    # Start a pool with the correct approval hash
    pool = _make_pool(
        {"fake_srv": srv},
        approvals={"fake_srv": expected_hash},
    )
    try:
        await pool.start()
        assert pool.started is True
        status = pool.server_status()
        assert status["fake_srv"] == "connected"
        assert len(pool.approval_pending()) == 0
    finally:
        await pool.aclose()


@pytest.mark.asyncio
async def test_pool_approval_from_real_db_table():
    """D1: The production path — pool started with approvals read from a
    real mcp_approvals table row. A description mismatch marks the server
    as approval_required (not a hand-constructed approvals dict)."""
    srv = _fake_stdio_server_config()
    # Create an in-memory SQLite DB with the mcp_approvals table
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS mcp_approvals ("
        "server TEXT PRIMARY KEY, "
        "description_hash TEXT NOT NULL, "
        "approved_at TEXT NOT NULL, "
        "approved_by TEXT NOT NULL)"
    )
    conn.commit()

    # Store a wrong approval hash
    create_mcp_approval(
        conn,
        "fake_srv",
        "0000000000000000000000000000000000000000000000000000000000000000",
    )
    create_mcp_config_approval(conn, "fake_srv", compute_config_hash(srv))

    # Read approvals the production way (as _start_mcp_pool does)
    approvals: dict[str, str] = {}
    for row in list_mcp_approvals(conn):
        approvals[row["server"]] = row["description_hash"]
    assert approvals == {
        "fake_srv": "0000000000000000000000000000000000000000000000000000000000000000"
    }
    config_approvals = {
        row["server"]: row["config_hash"] for row in list_mcp_config_approvals(conn)
    }

    # Start pool with approvals from the DB — same path as _start_mcp_pool
    pool = _make_pool(
        {"fake_srv": srv},
        approvals=approvals,
        config_approvals=config_approvals,
    )
    try:
        await pool.start()
        # The mismatch is detected via the real DB-loaded approval
        status = pool.server_status()
        assert status["fake_srv"] == "approval_required"
        pending = pool.approval_pending()
        assert "fake_srv" in pending
        assert (
            pending["fake_srv"]["old_hash"]
            == "0000000000000000000000000000000000000000000000000000000000000000"
        )
    finally:
        await pool.aclose()

    # Now store the CORRECT hash and verify it works
    correct_hash = compute_description_hash(FAKE_TOOL_DESCRIPTORS)
    create_mcp_approval(conn, "fake_srv", correct_hash)

    approvals2: dict[str, str] = {}
    for row in list_mcp_approvals(conn):
        approvals2[row["server"]] = row["description_hash"]

    pool2 = _make_pool(
        {"fake_srv": srv},
        approvals=approvals2,
        config_approvals=config_approvals,
    )
    try:
        await pool2.start()
        assert pool2.started is True
        status = pool2.server_status()
        assert status["fake_srv"] == "connected"
        assert len(pool2.approval_pending()) == 0
    finally:
        await pool2.aclose()
        conn.close()


@pytest.mark.asyncio
async def test_pool_approval_db_no_row_refuses_before_connect():
    """No config approval means first use refuses before discovery."""
    srv = _fake_stdio_server_config()
    pool = _make_pool({"fake_srv": srv}, approvals={}, config_approvals={})
    try:
        await pool.start()
        assert pool.started is True
        status = pool.server_status()
        assert status["fake_srv"] == "approval_required"
        assert pool.snapshot() == []
        pending = pool.approval_pending()["fake_srv"]
        assert pending["kind"] == "config"
        assert pending["old_hash"] == ""
        assert pending["new_hash"] == compute_config_hash(srv)
    finally:
        await pool.aclose()


@pytest.mark.asyncio
async def test_pool_first_tool_schema_requires_approval_after_config_approval():
    """Config approval permits discovery, but first-seen schemas still deny use."""
    srv = _fake_stdio_server_config()
    pool = _make_pool(
        {"fake_srv": srv},
        approvals={},
        config_approvals={"fake_srv": compute_config_hash(srv)},
    )
    try:
        await pool.start()
        assert pool.server_status()["fake_srv"] == "approval_required"
        assert pool.snapshot() == []
        pending = pool.approval_pending()["fake_srv"]
        assert pending["kind"] == "tools"
        assert pending["old_hash"] == ""
        assert pending["new_hash"] == compute_description_hash(FAKE_TOOL_DESCRIPTORS)
        with pytest.raises(RuntimeError, match="not connected"):
            await pool.call_tool("fake_srv", "echo", {"message": "blocked"})
    finally:
        await pool.aclose()


@pytest.mark.asyncio
async def test_pool_init_timeout_error_surfaces():
    """A server that hangs on initialize() is marked as error, and the
    pool continues (other servers unaffected)."""
    hang_cmd = [sys.executable, "-c", "import time; time.sleep(60)"]
    srv = McpServerConfig(
        name="hang_srv",
        transport="stdio",
        command=hang_cmd,
        risk_tier=SecurityRisk.MEDIUM,
        enabled=True,
    )
    pool = _make_pool({"hang_srv": srv})
    try:
        # Should NOT raise — the pool marks it as error
        await pool.start()
        status = pool.server_status()
        assert status["hang_srv"] == "error"
        assert len(pool.snapshot()) == 0
    finally:
        await pool.aclose()


@pytest.mark.asyncio
async def test_pool_stdio_connect_failure_carries_diagnostic():
    """BUG 2: before this, a stdio server that failed to even spawn (e.g. a
    misspelled/missing command — exactly what BUG 1's broken single-argv-token
    behavior produced) was marked "error" with NO further detail anywhere but
    the container's stdout. `server_diagnostics()` now carries the same
    {code, attempts, exception_type} shape the streamable_http transport's
    `McpHttpConnector.status` already provides for its own failures."""
    srv = McpServerConfig(
        name="missing_srv",
        transport="stdio",
        command=["/definitely/not/a/real/executable/on/this/host"],
        risk_tier=SecurityRisk.MEDIUM,
        enabled=True,
    )
    pool = _make_pool({"missing_srv": srv})
    try:
        await pool.start()
        assert pool.server_status()["missing_srv"] == "error"
        diagnostics = pool.server_diagnostics()
        assert diagnostics["missing_srv"] == {
            "code": "mcp_stdio_connect_failed",
            "attempts": 1,
            "exception_type": "FileNotFoundError",
        }
    finally:
        await pool.aclose()


@pytest.mark.asyncio
async def test_pool_allowed_tools_restricts_snapshot():
    """When allowed_tools is set, only those tools appear in the snapshot."""
    srv = _fake_stdio_server_config()
    srv = srv.model_copy(update={"allowed_tools": ["echo"]})
    pool = _make_pool({"fake_srv": srv})
    try:
        await pool.start()
        names = {t.name for t in pool.snapshot()}
        assert names == {"mcp__fake_srv__echo"}
    finally:
        await pool.aclose()


@pytest.mark.asyncio
async def test_pool_teardown_clears_clients():
    """After aclose(), the pool has no clients and is not started."""
    srv = _fake_stdio_server_config()
    pool = _make_pool({"fake_srv": srv})
    await pool.start()
    assert pool.started is True
    await pool.aclose()
    assert pool.started is False
    assert len(pool.snapshot()) == 0


@pytest.mark.asyncio
async def test_pool_streamable_http_deferred():
    """streamable_http servers are deferred to rung B (marked error)."""
    srv = McpServerConfig(
        name="http_srv",
        transport="streamable_http",
        url="https://example.com/mcp",
        risk_tier=SecurityRisk.MEDIUM,
        enabled=True,
    )
    pool = _make_pool({"http_srv": srv})
    try:
        await pool.start()
        status = pool.server_status()
        assert status["http_srv"] == "error"
    finally:
        await pool.aclose()


# ---- D5: tool invocation integration test -----------------------------------


@pytest.mark.asyncio
async def test_pool_call_tool_via_client():
    """D5: A registered+approved tool is invoked through the pool and
    returns the fake server's real result."""
    srv = _fake_stdio_server_config()
    pool = _make_pool({"fake_srv": srv})
    try:
        await pool.start()
        assert pool.started is True

        # Call the 'echo' tool via the pool
        result = await pool.call_tool("fake_srv", "echo", {"message": "hello world"})

        assert "content" in result
        assert not result.get("isError", True)

        # Extract text content
        texts = [
            item.text if hasattr(item, "text") else item.get("text", "")
            for item in result["content"]
        ]
        assert any("Echo: hello world" in t for t in texts)
    finally:
        await pool.aclose()


@pytest.mark.asyncio
async def test_pool_call_tool_add():
    """D5: The 'add' tool returns correct computation."""
    srv = _fake_stdio_server_config()
    pool = _make_pool({"fake_srv": srv})
    try:
        await pool.start()
        result = await pool.call_tool("fake_srv", "add", {"a": 3, "b": 4})
        texts = [
            item.text if hasattr(item, "text") else item.get("text", "")
            for item in result["content"]
        ]
        assert any("7" in t for t in texts)
    finally:
        await pool.aclose()


@pytest.mark.asyncio
async def test_pool_call_tool_unknown_server_raises():
    """D5: Calling a tool on an unknown server raises RuntimeError."""
    srv = _fake_stdio_server_config()
    pool = _make_pool({"fake_srv": srv})
    try:
        await pool.start()
        with pytest.raises(RuntimeError, match="not connected"):
            await pool.call_tool("nonexistent", "echo", {})
    finally:
        await pool.aclose()


# ---- P1: teardown of unapproved server ---------------------------------------


@pytest.mark.asyncio
async def test_approval_mismatch_tears_down_unapproved_server():
    """P1: After approval mismatch, pool.call_tool(server, ...) RAISES
    RuntimeError — the unapproved server's subprocess must be disconnected
    so it is NOT callable. This proves teardown, not just hiding tools."""
    srv = _fake_stdio_server_config(name="bad_srv")
    pool = _make_pool(
        {"bad_srv": srv},
        approvals={"bad_srv": "0000000000000000000000000000000000000000000000000000000000000000"},
    )
    try:
        await pool.start()
        # Approval mismatch should mark the server
        assert pool.server_status()["bad_srv"] == "approval_required"
        assert "bad_srv" in pool.approval_pending()
        # P1: the unapproved server MUST be torn down — call_tool MUST raise
        with pytest.raises(RuntimeError, match="not connected"):
            await pool.call_tool("bad_srv", "echo", {"message": "test"})
    finally:
        await pool.aclose()


# ---- P3: WS frame mcp_approval_required --------------------------------------


def test_mcp_approval_required_frame_reaches_ws_client():
    """P3: an `mcp_approval_required` ephemeral frame is dispatched over the LIVE
    websocket as a typed WSServerFrame. This drives the REAL production boundary
    — app.py's `pump_ephemeral`, which converts the ephemeral dict the runtime
    publishes into `WSServerFrame(type="mcp_approval_required", mcp_approval=...)`
    on the wire — rather than self-publishing into a hand-registered queue or
    hand-constructing the frame. The runtime emits exactly this ephemeral dict
    from its approval-pending loop on conversation start (runtime.py)."""
    import time

    from disco.agent_server import create_app
    from disco.core import SqliteEventStore
    from fastapi.testclient import TestClient

    store = SqliteEventStore(":memory:")
    try:
        with TestClient(create_app(store)) as client:
            cid = client.post("/conversations", json={"owner_id": "local"}).json()[
                "conversation_id"
            ]

            with client.websocket_connect(f"/ws/conversations/{cid}") as ws:
                assert ws.receive_json()["type"] == "state"

                # The ephemeral pump subscribes LAZILY — the queue is registered only when
                # pump_ephemeral's `async for` first iterates (sqlite.py:_subscribe_
                # ephemeral). publish_ephemeral is fire-and-forget and drops frames with
                # no live subscriber, so wait until the pump is subscribed before emitting.
                deadline = time.time() + 5.0
                while not store._eph_subscribers.get(cid):
                    assert time.time() < deadline, "ephemeral pump never subscribed"
                    time.sleep(0.02)

                # Emit the exact ephemeral dict the runtime emits for a re-approval event.
                store.publish_ephemeral(
                    cid,
                    {
                        "type": "mcp_approval_required",
                        "server": "bad_ws_srv",
                        "description_hash": "a" * 64,
                        "old_description_hash": "b" * 64,
                    },
                )

                # It arrives over the real socket as the typed WSServerFrame (app.py
                # pump_ephemeral wrapped it) — proving the dispatch boundary, not a mock.
                frame = ws.receive_json()
                assert frame["type"] == "mcp_approval_required"
                assert frame["mcp_approval"]["server"] == "bad_ws_srv"
                assert frame["mcp_approval"]["description_hash"] == "a" * 64
                assert frame["mcp_approval"]["old_description_hash"] == "b" * 64
    finally:
        store.close()


# ---- P5: wrapper-level invocation test ---------------------------------------


@pytest.mark.asyncio
async def test_mcp_tool_wrapper_run_returns_real_tool_outcome():
    """P5: _MCPToolWrapper.run() is invoked through the production wrapper
    (not pool.call_tool directly) and returns a real ToolOutcome with the
    fake server's result. This proves the wrapper path end-to-end."""
    from disco.agent_server.runtime import _MCPToolWrapper
    from disco.tools.anatomy import ToolOutcome

    srv = _fake_stdio_server_config()
    pool = _make_pool({"fake_srv": srv})
    try:
        await pool.start()

        # Get the echo tool's ToolDef from the snapshot
        snapshot = pool.snapshot()
        echo_tdef = next(t for t in snapshot if t.name == "mcp__fake_srv__echo")

        # Build the wrapper (production path)
        wrapper = _MCPToolWrapper(echo_tdef, pool)

        # Create args matching the ToolDef's args_model (pydantic model)
        args = echo_tdef.args_model(message="hello via wrapper")

        # Invoke through the wrapper's run() method (production wrapper path)
        result = await wrapper.run(args, ctx=None)

        assert isinstance(result, ToolOutcome)
        assert result.success is True
        assert "Echo: hello via wrapper" in result.content
        assert result.error is None or result.error == ""
    finally:
        await pool.aclose()
