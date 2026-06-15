"""E6 (#10) — surface the REAL new_hash from the MCP approval pool.

The MCP pool's `ApprovalRequired(old_hash, new_hash)` carries the
AUTHORITATIVE `new_hash` computed over the live server's tool
descriptions. For the frontend ApprovalDiff to show that REAL value (not a
stub of the stored hash), the agent-server must persist it to the shared
`mcp_approval_pending` table at startup, and the app-server must read that
table and project it onto `McpConnectionDTO.new_description_hash`. An
unchanged server (new_hash == old_hash) MUST NOT trigger a re-approval:
the pool's `approval_pending()` stays empty, the agent-server writes no
drift row, and the DTO's `new_description_hash` is `None`.

These tests drive the real end-to-end path:
  pool.approval_pending()  →  mcp_approval_pending table  →  ConfigState.mcp_connections()

through the real `ConversationRuntime._start_mcp_pool` (the production
write path) and the real `ConfigState.mcp_connections` (the production
read path). No mocks.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from disco.agent_server import ConversationRuntime
from disco.app_server.config_state import ConfigState
from disco.core import SecurityRisk, SkillStore, SqliteEventStore
from disco.core.llm import (
    ConfigStore,
    RouterConfig,
    SecretBox,
    SecretStore,
)
from disco.tools.mcp.approval import compute_description_hash
from disco.tools.mcp.migrations import (
    create_mcp_approval,
    list_mcp_approval_pending,
    set_mcp_approval_pending,
)

# ---- shared fixtures --------------------------------------------------------


_FAKE_SERVER_RAW_TOOLS = [
    {"name": "echo", "description": "Echo back the message"},
    {"name": "add", "description": "Add two numbers together"},
    {"name": "read_file", "description": "Read a file from the server temp dir"},
    {"name": "list_files", "description": "List files in the temp dir"},
]
_FAKE_SERVER_EXPECTED_HASH = compute_description_hash(_FAKE_SERVER_RAW_TOOLS)


def _fake_stdio_command() -> list[str]:
    """The command that boots the FakeStdioServer (subprocess per pool start)."""
    return [
        sys.executable,
        "-c",
        "from packages.tools.tests.mcp_fakes import FakeStdioServer; "
        "import asyncio; asyncio.run(FakeStdioServer().run())",
    ]


def _router_config_with_fake_srv(server_name: str = "fake_e6_srv") -> RouterConfig:
    """Build a RouterConfig with one enabled stdio MCP server (the fake one).

    Mirrors the production config shape that the runtime's `_start_mcp_pool`
    reads — `cfg.mcp.enabled + cfg.mcp.servers[name] = {transport, command, ...}`.
    """
    return RouterConfig.model_validate(
        {
            "models": {
                "m": {
                    "model_id": "m",
                    "provider": "fake",
                    "context_window": 8192,
                }
            },
            "default_model": "m",
            "mcp": {
                "enabled": True,
                "servers": {
                    server_name: {
                        "transport": "stdio",
                        "command": _fake_stdio_command(),
                        "risk_tier": SecurityRisk.MEDIUM.value,
                        "enabled": True,
                    }
                },
            },
        }
    )


def _runtime_with_mcp(
    store: SqliteEventStore, server_name: str, tmp_path: Path
) -> ConversationRuntime:
    """A ConversationRuntime whose config_store yields an mcp-enabled RouterConfig."""
    rt = ConversationRuntime(
        store,
        config_store=ConfigStore(
            path=tmp_path / "config.json",
            base_factory=lambda: _router_config_with_fake_srv(server_name),
        ),
        secret_store=SecretStore(
            tmp_path / "secrets.json", box=SecretBox(None)
        ),
    )
    return rt


def _config_state(
    store: SqliteEventStore, tmp_path: Path, server_name: str, server_url: str
) -> ConfigState:
    """An app-server ConfigState that shares the same DB and a config that
    lists the MCP server (so mcp_connections() actually projects it)."""
    cfg = _router_config_with_fake_srv(server_name)
    # The agent-server runtime reads `command=[...]`; the app-server projects
    # `url` for display. We synthesise a friendly URL pointing at the same
    # stdio command, so the projection has a non-empty `url` for assertions.
    cfg.mcp.servers[server_name]["url"] = server_url
    cfg_store = ConfigStore(
        path=tmp_path / "config.json",
        base_factory=lambda: cfg,
    )
    return ConfigState(
        config=None,
        store=cfg_store,
        secrets=SecretStore(
            tmp_path / "secrets.json", box=SecretBox(None)
        ),
        skills=SkillStore(tmp_path / "skills"),
        db_conn=store._conn,
    )


# ---- TEST 1 (positive): KNOWN new_hash from ApprovalRequired is surfaced ----


@pytest.mark.asyncio
async def test_e6_real_new_hash_threads_through_pool_db_and_dto(tmp_path):
    """E6 (#10) — the AUTHORITATIVE new_hash from `ApprovalRequired` is
    threaded through every step to the app-server DTO.

    Path under test:
      pool.approval_pending()              (the SOURCE OF TRUTH)
      → mcp_approval_pending table row     (the persistent bridge)
      → ConfigState.mcp_connections() DTO  (the UI contract)

    Setup: pre-insert a STALE (wrong) approval hash for a fake stdio server,
    then start the runtime's pool. The pool detects drift, computes the
    REAL new_hash over the live tool descriptions, and the runtime
    `_start_mcp_pool` MUST persist that new_hash to the shared
    `mcp_approval_pending` table. The app-server's `mcp_connections()` MUST
    then project that exact value onto `new_description_hash` on the DTO.

    Anti-gaming: the test compares the DTO's new_description_hash against
    `_FAKE_SERVER_EXPECTED_HASH` (the SHA-256 the canonical
    `compute_description_hash` produces over the FakeStdioServer's
    tool list). A stub that returned the stored/old hash, or a truncated
    or recomputed value, would fail this check.
    """
    server_name = "fake_e6_srv"
    stale_hash = "f" * 64  # anything != _FAKE_SERVER_EXPECTED_HASH triggers drift

    # Shared in-memory DB — both the agent-server runtime and the app-server
    # ConfigState read from `store._conn` so the drift row crosses the
    # package boundary via the SAME table (the production path).
    store = SqliteEventStore(":memory:")
    # Pre-insert the STALE approval so the pool's hash check raises.
    create_mcp_approval(store._conn, server_name, stale_hash)

    # The runtime detects drift and writes the AUTHORITATIVE new_hash to the
    # shared table. After start, BOTH the runtime's in-memory
    # `_mcp_approval_pending` AND the persisted row carry the new_hash.
    rt = _runtime_with_mcp(store, server_name, tmp_path)
    await rt._start_mcp_pool()
    try:
        # 1) Pool source of truth: the runtime's `mcp_approval_state()` has
        #    the AUTHORITATIVE new_hash.
        approval_state = rt.mcp_approval_state()
        assert server_name in approval_state, (
            f"expected drift entry for {server_name!r}; got {approval_state!r}"
        )
        runtime_new = approval_state[server_name]["new_hash"]
        assert runtime_new == _FAKE_SERVER_EXPECTED_HASH, (
            "runtime._mcp_approval_pending carried a STUB new_hash "
            f"(got {runtime_new!r}, expected {_FAKE_SERVER_EXPECTED_HASH!r})"
        )

        # 2) Persistence bridge: the shared mcp_approval_pending table has
        #    the EXACT same (old, new) pair. Both old and new MUST be
        #    present — the DTO uses old for the diff side and new for the
        #    approve POST body.
        rows = list_mcp_approval_pending(store._conn)
        drift_rows = [r for r in rows if r["server"] == server_name]
        assert len(drift_rows) == 1, (
            f"expected exactly one drift row for {server_name!r}; got {drift_rows!r}"
        )
        assert drift_rows[0]["old_hash"] == stale_hash
        assert drift_rows[0]["new_hash"] == _FAKE_SERVER_EXPECTED_HASH
    finally:
        await rt._close_mcp_pool()

    # 3) UI contract: the app-server's mcp_connections() projects the
    #    AUTHORITATIVE new_hash onto McpConnectionDTO.new_description_hash.
    #    No recompute, no stub, no truncation. The frontend's
    #    ApprovalDiff reads this field directly.
    cfg_state = _config_state(
        store, tmp_path, server_name, "stdio://fake"
    )
    conns = cfg_state.mcp_connections()
    assert len(conns) == 1
    conn = conns[0]
    assert conn.id == server_name
    # The OLD hash is the STORED approval (the operator hasn't accepted the
    # new tools yet) — the diff shows BOTH sides.
    assert conn.description_hash == stale_hash
    # The NEW hash is the AUTHORITATIVE value the live pool just computed
    # and the runtime persisted — NOT the stored hash, NOT a stub.
    assert conn.new_description_hash == _FAKE_SERVER_EXPECTED_HASH, (
        "DTO.new_description_hash was NOT the real new_hash from "
        f"ApprovalRequired (got {conn.new_description_hash!r}, "
        f"expected {_FAKE_SERVER_EXPECTED_HASH!r})"
    )


# ---- TEST 2 (negative): unchanged server does NOT trigger a re-approval -----


@pytest.mark.asyncio
async def test_e6_unchanged_server_writes_no_drift_row(tmp_path):
    """E6 (#10) — when the live server's description hash MATCHES the
    stored approval, the pool raises no ApprovalRequired, the runtime
    writes NO drift row, and the DTO's `new_description_hash` is `None`.

    This guards against a regression that would (a) spam the operator with
    unnecessary re-approval prompts every restart, or (b) write a drift
    row whose old_hash == new_hash (a "diff" that has no actual
    disagreement) — which would render a spurious ApprovalDiff banner and
    auto-fail the operator's `approve` POST (the re-approve body would
    equal the stored hash and be a no-op).
    """
    server_name = "fake_e6_srv"

    store = SqliteEventStore(":memory:")
    # Pre-insert the CORRECT approval hash — the pool's stored-vs-new check
    # (`stored != new_hash`) does NOT fire, so `approval_pending()` stays
    # empty and the runtime writes no drift row.
    create_mcp_approval(store._conn, server_name, _FAKE_SERVER_EXPECTED_HASH)

    rt = _runtime_with_mcp(store, server_name, tmp_path)
    await rt._start_mcp_pool()
    try:
        # 1) Pool: no drift. The pool must be connected (not
        #    `approval_required`) and `approval_pending()` is empty.
        assert rt._mcp_pool is not None
        status = rt._mcp_pool.server_status()
        assert status[server_name] == "connected", (
            f"expected connected (hashes match); got {status!r}"
        )
        assert rt._mcp_pool.approval_pending() == {}, (
            f"approval_pending() must be empty when hashes match; "
            f"got {rt._mcp_pool.approval_pending()!r}"
        )

        # 2) Runtime: `_mcp_approval_pending` is the empty bridge.
        assert rt.mcp_approval_state() == {}, (
            f"runtime.mcp_approval_state() must be empty when no drift; "
            f"got {rt.mcp_approval_state()!r}"
        )

        # 3) Persistence: the mcp_approval_pending table has NO row for
        #    this server. (We assert on the shared DB — the same one the
        #    app-server reads.)
        rows = list_mcp_approval_pending(store._conn)
        assert rows == [], (
            f"mcp_approval_pending must be empty when hashes match; "
            f"got {rows!r}"
        )
    finally:
        await rt._close_mcp_pool()

    # 4) UI contract: the DTO has `new_description_hash = None` so the
    #    frontend does NOT show an ApprovalDiff banner (the
    #    `c.new_description_hash` guard in McpSection.tsx is what hides it).
    cfg_state = _config_state(
        store, tmp_path, server_name, "stdio://fake"
    )
    conns = cfg_state.mcp_connections()
    assert len(conns) == 1
    conn = conns[0]
    assert conn.description_hash == _FAKE_SERVER_EXPECTED_HASH
    assert conn.new_description_hash is None, (
        f"new_description_hash must be None when the server is in sync; "
        f"got {conn.new_description_hash!r}"
    )


# ---- TEST 3 (negative): a brand-new server (no approval row) has no drift ---


@pytest.mark.asyncio
async def test_e6_first_time_server_writes_no_drift_row(tmp_path):
    """E6 — when there is NO prior approval row (first-time setup), the
    pool starts without a drift row and the DTO has `description_hash=None`
    and `new_description_hash=None`. The operator must explicitly approve
    to seed the first stored hash; the UI's Re-approve affordance is
    hidden in that case.
    """
    server_name = "fake_e6_srv"
    store = SqliteEventStore(":memory:")
    # No create_mcp_approval — first-time setup.

    rt = _runtime_with_mcp(store, server_name, tmp_path)
    await rt._start_mcp_pool()
    try:
        assert rt._mcp_pool is not None
        assert rt._mcp_pool.server_status()[server_name] == "connected"
        assert rt._mcp_pool.approval_pending() == {}
        assert rt.mcp_approval_state() == {}
        assert list_mcp_approval_pending(store._conn) == []
    finally:
        await rt._close_mcp_pool()

    cfg_state = _config_state(
        store, tmp_path, server_name, "stdio://fake"
    )
    conns = cfg_state.mcp_connections()
    assert len(conns) == 1
    assert conns[0].description_hash is None
    assert conns[0].new_description_hash is None


# ---- TEST 4 (positive, focused): the app-server READS the drift row --------


def test_e6_app_server_projects_authoritative_new_hash_from_drift_row(
    tmp_path,
):
    """E6 — focused test for the app-server's READ path:
    `ConfigState.mcp_connections()` must read the shared
    `mcp_approval_pending` table (the AUTHORITATIVE source the agent-server
    wrote to) and project the EXACT `new_hash` onto the DTO.

    This complements the E2E test by isolating the read path: a hand-
    inserted drift row (the exact same shape the agent-server writes)
    surfaces verbatim. The app-server NEVER recomputes; if a regression
    turned this into a recompute or a stub of the stored hash, this test
    fails.
    """
    server_name = "fake_e6_srv"
    store = SqliteEventStore(":memory:")
    # Operator previously approved the old hash.
    create_mcp_approval(store._conn, server_name, _FAKE_SERVER_EXPECTED_HASH)
    # The agent-server detected drift and wrote a pending row.
    authoratitive_new = "a" * 64  # a clearly distinct, hand-rolled value
    set_mcp_approval_pending(
        store._conn,
        server_name,
        old_hash=_FAKE_SERVER_EXPECTED_HASH,
        new_hash=authoratitive_new,
    )

    cfg_state = _config_state(
        store, tmp_path, server_name, "stdio://fake"
    )
    conns = cfg_state.mcp_connections()
    assert len(conns) == 1
    conn = conns[0]
    # The old hash (operator's last approval) is the diff's left side.
    assert conn.description_hash == _FAKE_SERVER_EXPECTED_HASH
    # The new hash is the AUTHORITATIVE value the agent-server's live pool
    # computed — passed through verbatim, NOT recomputed, NOT stubbed with
    # the stored hash.
    assert conn.new_description_hash == authoratitive_new, (
        "DTO.new_description_hash must be the exact value the agent-server "
        f"wrote (got {conn.new_description_hash!r}, "
        f"expected {authoratitive_new!r})"
    )


# ---- TEST 5 (negative, focused): a stale drift row is overwritten on restart


@pytest.mark.asyncio
async def test_e6_stale_drift_row_replaced_with_fresh_authoritative_value(
    tmp_path,
):
    """E6 — if a previous agent-server session wrote a drift row and then
    crashed, the NEXT startup's drift (which may have changed) MUST
    overwrite the stale row. `set_mcp_approval_pending` is `INSERT OR
    REPLACE` (per migrations.py), so the table is always the FRESHEST
    authoritative value, not a historical one.
    """
    server_name = "fake_e6_srv"
    stale_new = "b" * 64  # value a previous, crashed session wrote
    store = SqliteEventStore(":memory:")
    create_mcp_approval(store._conn, server_name, "f" * 64)
    # Pre-seed a stale drift row.
    set_mcp_approval_pending(
        store._conn, server_name, old_hash="f" * 64, new_hash=stale_new
    )
    # Sanity: the stale row is in the table.
    rows = list_mcp_approval_pending(store._conn)
    assert len(rows) == 1
    assert rows[0]["new_hash"] == stale_new

    # Now restart the pool. The pool computes the AUTHORITATIVE new_hash
    # (= _FAKE_SERVER_EXPECTED_HASH) and the runtime overwrites the stale
    # drift row with the fresh value.
    rt = _runtime_with_mcp(store, server_name, tmp_path)
    await rt._start_mcp_pool()
    try:
        # The pool detected drift (stored "f"*64 != real new_hash).
        approval_state = rt.mcp_approval_state()
        assert server_name in approval_state
        assert approval_state[server_name]["new_hash"] == _FAKE_SERVER_EXPECTED_HASH

        # The persisted row was replaced with the fresh authoritative value.
        rows = list_mcp_approval_pending(store._conn)
        assert len(rows) == 1
        assert rows[0]["new_hash"] == _FAKE_SERVER_EXPECTED_HASH, (
            f"stale drift row not overwritten (got {rows[0]['new_hash']!r}, "
            f"expected {_FAKE_SERVER_EXPECTED_HASH!r})"
        )
    finally:
        await rt._close_mcp_pool()
