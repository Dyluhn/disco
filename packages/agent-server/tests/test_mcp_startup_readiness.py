from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from disco.agent_server.app import create_app
from disco.agent_server.runtime import ConversationRuntime
from disco.core import SqliteEventStore
from disco.core.llm import ConfigStore


async def test_mcp_connection_attempt_cannot_own_agent_readiness(tmp_path) -> None:
    store = SqliteEventStore(":memory:")
    runtime = ConversationRuntime(
        store,
        config_store=ConfigStore(tmp_path / "config.json"),
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_mcp_start() -> None:
        entered.set()
        await release.wait()

    runtime._start_mcp_pool = blocked_mcp_start
    runtime.reconcile_orphaned_runs = AsyncMock(return_value=0)
    runtime.prewarm_model_probe = AsyncMock()
    runtime.prewarm_vision_probe = AsyncMock()
    runtime._idle_sweep_loop = AsyncMock()
    runtime._schedule_manager_loop = AsyncMock()
    runtime._close_mcp_pool = AsyncMock()
    app = create_app(store, runtime=runtime)

    lifespan = app.router.lifespan_context(app)
    await asyncio.wait_for(lifespan.__aenter__(), timeout=0.5)
    try:
        await asyncio.wait_for(entered.wait(), timeout=0.5)
    finally:
        release.set()
        await lifespan.__aexit__(None, None, None)

    runtime._close_mcp_pool.assert_awaited_once_with()
    store.close()
