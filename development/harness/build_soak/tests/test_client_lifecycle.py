"""Resource ownership at the Build Soak client boundary."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from harness.build_soak._test_support.api_runner.helpers_transport import FakeTransport
from harness.build_soak.adapters.disco_api import DiscoApiClient


def _client(tmp_path: Path) -> DiscoApiClient:
    db_path = tmp_path / "client.db"
    return DiscoApiClient(
        FakeTransport(db_path, states=["IDLE"]),
        db_path=str(db_path),
        poll_interval_s=0.0,
    )


@pytest.mark.asyncio
async def test_client_lazily_owns_and_explicitly_removes_verified_workspace_cache(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    assert client._verified_workspace_cache is None

    cache = client._verified_workspace_cache_dir()
    (cache / "proof.txt").write_text("owned", encoding="utf-8")
    assert cache.is_dir()

    await client.aclose()
    assert not cache.exists()
    assert client._verified_workspace_cache is None

    await client.aclose()  # idempotent shutdown
    with pytest.raises(RuntimeError, match="closed"):
        client._verified_workspace_cache_dir()


@pytest.mark.asyncio
async def test_client_close_cancels_owned_inspect_tasks(tmp_path: Path) -> None:
    client = _client(tmp_path)
    started = asyncio.Event()

    async def wait_forever() -> None:
        started.set()
        await asyncio.Event().wait()

    poller = asyncio.create_task(wait_forever())
    client._inspect_poll_tasks["conversation"] = poller
    client._inspect_stop_events["conversation"] = asyncio.Event()
    await started.wait()

    await client.aclose()

    assert poller.cancelled()
    assert client._inspect_poll_tasks == {}
    assert client._inspect_stop_events == {}
