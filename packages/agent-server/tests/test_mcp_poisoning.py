"""MCP description poisoning tests — RP-05 rung B.

Anti-gaming bar (from master brief):
- mutate a description between sessions → real McpPool.start() raises
  ApprovalRequired → run refuses.
- Real pool, not a hand-thrown exception.
"""

from __future__ import annotations

import sys

import pytest
from disco.core import SecurityRisk
from disco.tools.mcp.approval import (
    ApprovalRequired,
    compute_config_hash,
    compute_description_hash,
)
from disco.tools.mcp.config import McpServerConfig, McpSettings
from disco.tools.mcp.pool import McpPool

from packages.tools.tests.mcp_fakes import FAKE_TOOL_DESCRIPTORS


def _fake_stdio_server_config(name: str = "test_srv") -> McpServerConfig:
    """Build an McpServerConfig for the FakeStdioServer (same as test_mcp_pool)."""
    command = [
        sys.executable,
        "-c",
        "from packages.tools.tests.mcp_fakes import FakeStdioServer; "
        "import asyncio; asyncio.run(FakeStdioServer().run())",
    ]
    return McpServerConfig(
        name=name,
        transport="stdio",
        command=command,
        risk_tier=SecurityRisk.MEDIUM,
        enabled=True,
    )


# Pre-computed hashes for the FakeStdioServer tools (deterministic).
_FAKE_SERVER_RAW_TOOLS = FAKE_TOOL_DESCRIPTORS
_FAKE_SERVER_EXPECTED_HASH = compute_description_hash(_FAKE_SERVER_RAW_TOOLS)


def _config_approval(name: str, srv: McpServerConfig) -> dict[str, str]:
    return {name: compute_config_hash(srv)}


@pytest.mark.asyncio
async def test_poisoning_approval_mismatch_real_pool():
    """ANTI-GAMING: mutate the description hash → REAL McpPool.start()
    raises ApprovalRequired → the run refuses.

    This is NOT a hand-thrown exception. We:
    1. Start a pool with a pre-approved hash
    2. Verify the pool starts normally
    3. Start a NEW pool (new session) with a WRONG hash (simulating a
       poisoned/modified tool description)
    4. Verify the pool REFUSES the server — status is "approval_required"
    5. The tools from the refused server are NOT in the snapshot

    This exercises the REAL production path through McpPool.start() →
    _connect_stdio → compute_description_hash → ApprovalRequired.
    """
    srv = _fake_stdio_server_config(name="poison_srv")

    # Session 1: correct hash → pool starts normally
    pool1 = McpPool(
        McpSettings(enabled=True, servers={"poison_srv": srv}),
        approvals={"poison_srv": _FAKE_SERVER_EXPECTED_HASH},
        config_approvals=_config_approval("poison_srv", srv),
    )
    try:
        await pool1.start()
        assert pool1.started is True
        status = pool1.server_status()
        assert status["poison_srv"] == "connected"

        # Tools are registered
        snapshot = pool1.snapshot()
        tool_names = {t.name for t in snapshot}
        assert "mcp__poison_srv__echo" in tool_names
        assert "mcp__poison_srv__add" in tool_names
    finally:
        await pool1.aclose()

    # Session 2: WRONG hash (simulates a poisoned description)
    poison_hash = "a" * 64  # deliberately wrong
    pool2 = McpPool(
        McpSettings(enabled=True, servers={"poison_srv": srv}),
        approvals={"poison_srv": poison_hash},
        config_approvals=_config_approval("poison_srv", srv),
    )
    try:
        await pool2.start()

        # The pool itself still starts (other servers are unaffected)
        assert pool2.started is True

        # But the poisoned server is REFUSED
        status = pool2.server_status()
        assert status["poison_srv"] == "approval_required"

        # The server's tools are NOT in the snapshot
        snapshot2 = pool2.snapshot()
        snapshot_names = {t.name for t in snapshot2}
        assert len(snapshot_names) == 0  # nothing from the refused server

        # The approval pending info is available for WS dispatch
        pending = pool2.approval_pending()
        assert "poison_srv" in pending
        assert pending["poison_srv"]["old_hash"] == poison_hash
        assert len(pending["poison_srv"]["new_hash"]) == 64  # real SHA-256
        assert pending["poison_srv"]["new_hash"] == _FAKE_SERVER_EXPECTED_HASH
    finally:
        await pool2.aclose()


@pytest.mark.asyncio
async def test_poisoning_two_servers_one_poisoned():
    """When ONE server is poisoned, the other server still starts normally.

    This is the D1 per-server refusal guarantee: a poisoned description does
    NOT crash the pool or prevent other servers from starting.
    """
    good_srv = _fake_stdio_server_config(name="good_srv")
    bad_srv = _fake_stdio_server_config(name="bad_srv")

    pool = McpPool(
        McpSettings(enabled=True, servers={"good_srv": good_srv, "bad_srv": bad_srv}),
        approvals={
            "good_srv": _FAKE_SERVER_EXPECTED_HASH,
            "bad_srv": "b" * 64,  # poisoned
        },
        config_approvals={
            "good_srv": compute_config_hash(good_srv),
            "bad_srv": compute_config_hash(bad_srv),
        },
    )
    try:
        await pool.start()

        assert pool.started is True
        status = pool.server_status()

        # Good server is connected
        assert status["good_srv"] == "connected"

        # Bad server is refused
        assert status["bad_srv"] == "approval_required"

        # Good server's tools are in the snapshot
        snapshot = pool.snapshot()
        good_tool_names = {t.name for t in snapshot if "good_srv" in t.name}
        assert len(good_tool_names) > 0

        # Bad server's tools are NOT in the snapshot
        bad_tool_names = {t.name for t in snapshot if "bad_srv" in t.name}
        assert len(bad_tool_names) == 0
    finally:
        await pool.aclose()


def test_compute_description_hash_is_deterministic():
    """compute_description_hash is deterministic — same tools → same hash."""
    hash1 = compute_description_hash(_FAKE_SERVER_RAW_TOOLS)
    hash2 = compute_description_hash(_FAKE_SERVER_RAW_TOOLS)
    assert hash1 == hash2
    assert len(hash1) == 64  # SHA-256 hex digest


def test_compute_description_hash_changes_when_description_mutated():
    """Mutating a tool description changes the hash (the poisoning detection)."""
    original = [
        {"name": "echo", "description": "Echo back the message"},
    ]
    mutated = [
        {"name": "echo", "description": "Echo back the message AND DELETE FILES"},
    ]
    hash_orig = compute_description_hash(original)
    hash_mut = compute_description_hash(mutated)
    assert hash_orig != hash_mut


def test_compute_description_hash_changes_with_new_tool():
    """Adding a new tool changes the hash (new tool == re-approval required)."""
    original = [
        {"name": "echo", "description": "Echo"},
    ]
    with_new_tool = [
        {"name": "echo", "description": "Echo"},
        {"name": "delete_everything", "description": "Deletes everything"},
    ]
    assert compute_description_hash(original) != compute_description_hash(with_new_tool)


def test_approval_required_exception_has_hash_info():
    """ApprovalRequired exception carries old and new hashes for WS dispatch."""
    exc = ApprovalRequired("test_srv", "abc123", "def456")
    assert exc.server == "test_srv"
    assert exc.old_hash == "abc123"
    assert exc.new_hash == "def456"
    assert "description_hash changed" in str(exc)
    assert "re-approval required" in str(exc)
