"""Live-model probe must NEVER block a running event loop (the North Star #25
wedge). `_probe_live_model` is reached from async routes (/models, /health) and
from kick()'s synchronous loop composition — all on the loop. A slow/hanging
llama-server /props would otherwise stall the whole agent-server for up to 2s.

Contract locked here:
  - called WITH a running loop → returns immediately with the static fallback,
    and the blocking probe runs in a worker thread that fills the cache;
  - called WITHOUT a loop (CLI / worker) → blocks and returns the live value.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest
from disco.agent_server import runtime as rt


@pytest.mark.asyncio
async def test_probe_does_not_block_running_loop(monkeypatch):
    rt._LIVE_MODEL_PROBE_CACHE.clear()
    seen: dict[str, str] = {}

    def slow_probe(base_url: str, api_key):
        seen["thread"] = threading.current_thread().name
        time.sleep(1.0)  # simulate a slow / hanging /props
        result = {"model_id": "live-model", "n_ctx": 8192}
        rt._LIVE_MODEL_PROBE_CACHE[base_url] = result
        return result

    monkeypatch.setattr(rt, "_do_live_model_probe", slow_probe)

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    out = rt._probe_live_model("http://fake:9999/v1", None)
    elapsed = loop.time() - t0

    # Returned right away with the static fallback — the loop was NOT blocked.
    assert elapsed < 0.2, f"probe blocked the event loop for {elapsed:.2f}s"
    assert out == {"model_id": None, "n_ctx": None}

    # The blocking probe ran OFF the loop (a worker thread) and filled the cache,
    # so the NEXT call is served live.
    await asyncio.sleep(1.4)
    assert seen["thread"] != threading.current_thread().name
    assert rt._probe_live_model("http://fake:9999/v1", None) == {
        "model_id": "live-model",
        "n_ctx": 8192,
    }


def test_probe_off_loop_blocks_and_returns(monkeypatch):
    rt._LIVE_MODEL_PROBE_CACHE.clear()
    monkeypatch.setattr(rt, "_do_live_model_probe", lambda b, k: {"model_id": "x", "n_ctx": 4096})
    # No running loop in this plain sync test → the probe blocks directly and
    # returns the live value (the CLI / `disco verify` path).
    assert rt._probe_live_model("http://fake:1234/v1", None) == {
        "model_id": "x",
        "n_ctx": 4096,
    }


def test_probe_empty_base_url_is_noop():
    rt._LIVE_MODEL_PROBE_CACHE.clear()
    assert rt._probe_live_model(None, None) == {"model_id": None, "n_ctx": None}
    assert rt._probe_live_model("", None) == {"model_id": None, "n_ctx": None}
