"""T6/E2: the live-model /props probe cache must have a TTL.
Hot-swaps re-probe after the TTL; within it, cached results avoid re-probing.

Contract locked here:
  - cache entry WITHIN _PROBE_TTL_S (60s) -> served from cache, the
    blocking probe body `_do_live_model_probe` is NOT called again;
  - cache entry PAST _PROBE_TTL_S -> treated as a miss: the entry is
    overwritten by a fresh probe (and the new value is returned);
  - the in-flight guard + non-blocking on-event-loop behavior are
    preserved (this file only exercises the OFF-loop path so the
    read/cache-TTL decision is the only thing under test).

Time is controlled via monkeypatch of `time.monotonic` so the test is
deterministic and runs in milliseconds, not real seconds.
"""

from __future__ import annotations

import pytest
from disco.agent_server import runtime as rt
from disco.agent_server import runtime_model_probe as probe_mod


@pytest.fixture(autouse=True)
def _clear_cache():
    rt._LIVE_MODEL_PROBE_CACHE.clear()
    yield
    rt._LIVE_MODEL_PROBE_CACHE.clear()


def test_fresh_entry_is_served_from_cache(monkeypatch):
    """A cache entry with a timestamp inside _PROBE_TTL_S is served as-is
    and the blocking probe body is NOT invoked a second time."""
    base_url = "http://fresh:9999/v1"
    cached = {"model_id": "old-model", "n_ctx": 4096}
    seed_ts = 1000.0
    rt._LIVE_MODEL_PROBE_CACHE[base_url] = (cached, seed_ts)

    monkeypatch.setattr(probe_mod.time, "monotonic", lambda: seed_ts + 30.0)

    calls = []

    def fake_probe(u, k):
        calls.append((u, k))
        return {"model_id": "new-model", "n_ctx": 8192}

    monkeypatch.setattr(rt, "_do_live_model_probe", fake_probe)

    out = rt._probe_live_model(base_url, None)

    assert out == cached, f"expected cached value, got {out!r}"
    assert calls == [], f"probe should NOT be called on a fresh cache hit, got {calls!r}"


def test_stale_entry_triggers_reprobe(monkeypatch):
    """A cache entry with a timestamp older than _PROBE_TTL_S is treated
    as a miss: the blocking probe body is invoked, and the new value
    replaces the stale entry in the cache."""
    base_url = "http://stale:9999/v1"
    stale_value = {"model_id": "old-model", "n_ctx": 4096}
    seed_ts = 1000.0
    rt._LIVE_MODEL_PROBE_CACHE[base_url] = (stale_value, seed_ts)

    now = seed_ts + 90.0
    monkeypatch.setattr(probe_mod.time, "monotonic", lambda: now)

    new_value = {"model_id": "hot-swapped", "n_ctx": 16384}
    calls = []

    def fake_probe(u, k):
        calls.append((u, k))
        rt._LIVE_MODEL_PROBE_CACHE[u] = (new_value, probe_mod.time.monotonic())
        return new_value

    monkeypatch.setattr(rt, "_do_live_model_probe", fake_probe)

    out = rt._probe_live_model(base_url, None)

    assert calls == [(base_url, None)], (
        f"probe should be called exactly once on a stale entry, got {calls!r}"
    )
    assert out == new_value, f"expected freshly-probed value, got {out!r}"
    cached_after = rt._LIVE_MODEL_PROBE_CACHE[base_url]
    assert isinstance(cached_after, tuple) and len(cached_after) == 2
    assert cached_after[0] == new_value


def test_probe_ttl_s_is_60_seconds():
    """The TTL constant is pinned at 60s -- that's the spec for T6."""
    assert rt._PROBE_TTL_S == 60.0


def test_ttl_boundary_exactly_at_limit_is_still_fresh(monkeypatch):
    """An entry whose age is exactly _PROBE_TTL_S is still served from
    cache (the contract is `<=`, not `<`). The next call past the
    boundary must re-probe."""
    base_url = "http://boundary:9999/v1"
    cached = {"model_id": "boundary", "n_ctx": 2048}
    seed_ts = 5000.0
    rt._LIVE_MODEL_PROBE_CACHE[base_url] = (cached, seed_ts)

    monkeypatch.setattr(probe_mod.time, "monotonic", lambda: seed_ts + 60.0)
    monkeypatch.setattr(rt, "_do_live_model_probe", lambda u, k: pytest.fail("should not probe"))
    assert rt._probe_live_model(base_url, None) == cached

    seen = []

    def fake_probe(u, k):
        seen.append((u, k))
        fresh = {"model_id": "boundary-fresh", "n_ctx": 2048}
        rt._LIVE_MODEL_PROBE_CACHE[u] = (fresh, probe_mod.time.monotonic())
        return fresh

    monkeypatch.setattr(rt, "_do_live_model_probe", fake_probe)
    monkeypatch.setattr(probe_mod.time, "monotonic", lambda: seed_ts + 60.001)

    out = rt._probe_live_model(base_url, None)
    assert seen == [(base_url, None)]
    assert out == {"model_id": "boundary-fresh", "n_ctx": 2048}
