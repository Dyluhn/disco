import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from disco.agent_server import ConversationRuntime
from disco.core import (
    ConversationStatus,
    EventSource,
    LLMMessage,
    MessageEvent,
    SqliteEventStore,
    StatusEvent,
)
from disco.core.llm import ConfigStore, ProjectStorageSettings, RouterConfig
from disco.tools import ProcessSandboxService

# ---- helpers -----------------------------------------------------------------


def _runtime(store: SqliteEventStore) -> ConversationRuntime:
    router = MagicMock()
    svc = ProcessSandboxService()
    return ConversationRuntime(store, router=router, sandbox_service=svc)


def _runtime_with_storage(store: SqliteEventStore, projects_root: str) -> ConversationRuntime:
    cfg = RouterConfig.model_validate(
        {
            "models": {"m": {"model_id": "m", "provider": "fake", "context_window": 8192}},
            "default_model": "m",
        }
    )
    cfg = cfg.model_copy(update={"projects": ProjectStorageSettings(projects_root=projects_root)})
    cfg_store = ConfigStore(path=Path("/dev/null"))
    cfg_store.load = lambda: cfg  # type: ignore[method-assign]
    return ConversationRuntime(
        store,
        router=MagicMock(),
        sandbox_service=ProcessSandboxService(),
        config=cfg,
        config_store=cfg_store,
    )


async def _make_conversation(store: SqliteEventStore, status: ConversationStatus) -> str:
    cid = f"conv_test_{id(status)}"
    await store.append(
        cid,
        MessageEvent(
            source=EventSource.USER,
            message=LLMMessage(role="user", content="hello"),
        ),
    )
    if status != ConversationStatus.IDLE:
        await store.append(cid, StatusEvent(status=status))
    return cid


# ---- tests -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_run_ends_finished_no_immediate_reap(tmp_path):
    """Build run ends FINISHED with projects_root configured -> executor STILL in _executors."""
    store = SqliteEventStore(":memory:")
    rt = _runtime_with_storage(store, str(tmp_path))

    cid = await _make_conversation(store, ConversationStatus.FINISHED)

    # fake loop that just returns the state
    fake_loop = MagicMock()
    fake_loop.run = AsyncMock(return_value=await store.get_state(cid))
    rt._loop_registry.bind(cid, fake_loop)

    # inject executor
    fake_executor = MagicMock()
    fake_executor.kill = AsyncMock()
    rt._run_resources.set_executor(cid, fake_executor)

    # mock _surface_of
    rt.settings._surface_of = MagicMock(return_value="build")  # type: ignore[method-assign]

    await rt._run_execution.run(cid, fake_loop)

    # reap is gone, executor must still be there
    assert rt._run_resources.has_executor(cid)
    assert fake_executor.kill.call_count == 0


@pytest.mark.asyncio
async def test_sweep_idle_once_suspends_past_ttl(tmp_path):
    """sweep_idle_once past TTL suspends it."""
    store = SqliteEventStore(":memory:")
    rt = _runtime_with_storage(store, str(tmp_path))
    cid = await _make_conversation(store, ConversationStatus.FINISHED)

    fake_executor = MagicMock()
    fake_executor.kill = AsyncMock()
    rt._run_resources.set_executor(cid, fake_executor)

    # configure TTL so it sweeps
    with patch.dict("os.environ", {"PMX_IDLE_SUSPEND_S": "0"}):
        count = await rt.lifecycle.sweep_idle_once()

    assert count == 1
    assert not rt._run_resources.has_executor(cid)


@pytest.mark.asyncio
async def test_ttl_precedence(tmp_path):
    """TTL source precedence: env PMX_IDLE_SUSPEND_S > config sandbox.idle_ttl_s > default.

    A fresh conversation has idle_s ~= 0, so a TTL of 0 sweeps it and any large
    TTL (including the 1800 default) never does — the chosen TTL is observable
    through sweep behavior alone, no clock mocking."""
    store = SqliteEventStore(":memory:")
    rt = _runtime_with_storage(store, str(tmp_path))
    cfg = rt._config_store.load()
    seq = iter(range(100))

    async def fresh_conv() -> str:
        # _make_conversation derives its cid from id(status) — identical for every
        # FINISHED call — so mint unique cids here instead.
        cid = f"conv_ttl_{next(seq)}"
        await store.append(
            cid,
            MessageEvent(
                source=EventSource.USER,
                message=LLMMessage(role="user", content="hello"),
            ),
        )
        await store.append(cid, StatusEvent(status=ConversationStatus.FINISHED))
        executor = MagicMock()
        executor.kill = AsyncMock()
        rt._run_resources.set_executor(cid, executor)
        return cid

    # 1) env wins over config: config says "never" (huge), env says "now".
    cid = await fresh_conv()
    cfg.sandbox.idle_ttl_s = 10**9
    with patch.dict(os.environ, {"PMX_IDLE_SUSPEND_S": "0"}):
        assert await rt.lifecycle.sweep_idle_once() == 1
    assert not rt._run_resources.has_executor(cid)

    # 2) config wins over default: no env; config 0 sweeps a fresh conversation
    #    (the 1800 default never would).
    cid = await fresh_conv()
    cfg.sandbox.idle_ttl_s = 0
    with patch.dict(os.environ):
        os.environ.pop("PMX_IDLE_SUSPEND_S", None)
        assert await rt.lifecycle.sweep_idle_once() == 1
    assert not rt._run_resources.has_executor(cid)

    # 3) a large config TTL is respected: nothing swept, executor stays.
    cid = await fresh_conv()
    cfg.sandbox.idle_ttl_s = 10**9
    with patch.dict(os.environ):
        os.environ.pop("PMX_IDLE_SUSPEND_S", None)
        assert await rt.lifecycle.sweep_idle_once() == 0
    assert rt._run_resources.has_executor(cid)


@pytest.mark.asyncio
async def test_wake_for_preview(tmp_path):
    store = SqliteEventStore(str(tmp_path / "test.db"))
    store.create_conversation("conv_1234567890", surface="build")

    rt = _runtime_with_storage(store, str(tmp_path))

    # mock ensure_preview
    rt.preview.ensure_preview = AsyncMock(return_value=True)  # type: ignore[method-assign]
    rt.preview.port_upstream = MagicMock(return_value="http://upstream")  # type: ignore[method-assign]

    # 1. Unknown
    res = await rt.preview.wake_for_preview("00000000", 8000)
    assert res is None

    # 2. Known without live executor
    res = await rt.preview.wake_for_preview("12345678", 8000)
    assert res == "http://upstream"
    rt.preview.ensure_preview.assert_called_once_with("conv_1234567890")
    rt.preview.port_upstream.assert_called_once_with("conv_1234567890", 8000)


@pytest.mark.asyncio
async def test_wake_for_preview_concurrent(tmp_path):
    store = SqliteEventStore(str(tmp_path / "test.db"))
    store.create_conversation("conv_1234567890", surface="build")

    rt = _runtime_with_storage(store, str(tmp_path))

    call_count = 0

    async def fake_ensure(cid):
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.1)  # yield to allow concurrency
        rt._run_resources.set_executor(cid, MagicMock())
        return True

    rt.preview.ensure_preview = AsyncMock(side_effect=fake_ensure)  # type: ignore[method-assign]
    rt.preview.port_upstream = MagicMock(return_value="http://upstream")  # type: ignore[method-assign]

    # All three callers pass the no-live-executor fast path before any of them
    # acquires the lock; the re-check of _executors INSIDE the lock is what must
    # collapse the storm to a single ensure_preview call.
    await asyncio.gather(
        rt.preview.wake_for_preview("12345678", 8000),
        rt.preview.wake_for_preview("12345678", 8000),
        rt.preview.wake_for_preview("12345678", 8000),
    )

    assert rt.preview.ensure_preview.call_count == 1
