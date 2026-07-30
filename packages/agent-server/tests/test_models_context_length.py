"""CW-1: live context-window derivation incl. the OpenAI-compatible `/models`
`context_length` (OpenRouter) — with a SEPARATE model-keyed cache.

llama.cpp exposes a SERVER-WIDE `n_ctx` via `/props`; OpenRouter has no `/props`
but `GET /models` returns a per-model `context_length`; MiniMax `/models` omits
the field entirely. The per-model value must be cached in `_MODELS_CTX_CACHE`
(keyed by base_url, looked up by model_id) and NEVER folded into the base_url-keyed
`_LIVE_MODEL_PROBE_CACHE` — pinning one model's window onto every model at a base_url
was the codex P1 bug this guards against.

Contract locked here:
  (a) /props-only server      → n_ctx from props; /models never consulted;
  (b) OpenRouter-style /models → n_ctx via the MODEL-KEYED map lookup;
  (c) MiniMax-style /models    → no context_length + no /props → n_ctx None
                                 → caller falls back to static entry.context_window;
  (d) the /models value is NOT written into _LIVE_MODEL_PROBE_CACHE (isolation):
      two models at the same base_url resolve to DIFFERENT windows;
  (e) non-blocking on a running loop → returns the static fallback, then a worker
      thread fills the model-keyed cache and the next call is served live.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time

import httpx
import pytest
from disco.agent_server import runtime as rt
from disco.agent_server import runtime_model_probe as probe
from disco.agent_server.runtime_model_settings import (
    RuntimeSettingsCorruptionError,
    RuntimeSettingsRepository,
    RuntimeSettingsVersionError,
)


class _FakeResp:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        return self._payload


def _route(get_map: dict[str, _FakeResp]):
    """A fake httpx.get that dispatches on the URL's path suffix; anything not
    routed is a 404 (a backend that doesn't serve that endpoint, e.g. OpenRouter
    has no /props)."""

    def fake_get(url: str, headers=None, timeout=None, **kwargs):  # noqa: ANN001, ARG001
        for suffix, resp in get_map.items():
            if url.endswith(suffix):
                return resp
        return _FakeResp(404, {})

    return fake_get


# OpenRouter-shaped /models payload: per-model context_length, two DISTINCT windows.
_OR_MODELS = _FakeResp(
    200,
    {
        "data": [
            {"id": "deepseek/deepseek-v4", "context_length": 1048576},
            {"id": "minimax/minimax-m2", "context_length": 192000},
            {"id": "no-ctx/model", "name": "missing field"},  # no context_length
        ]
    },
)
# MiniMax-shaped /models payload: NO context_length field anywhere.
_MINIMAX_MODELS = _FakeResp(200, {"data": [{"id": "abab6.5-chat"}, {"id": "minimax-m2"}]})
# llama.cpp /props payload: a server-wide n_ctx.
_LLAMA_PROPS = _FakeResp(
    200, {"default_generation_settings": {"n_ctx": 131072}, "model_path": "Qwen.gguf"}
)


@pytest.fixture(autouse=True)
def _clear_caches():
    rt._LIVE_MODEL_PROBE_CACHE.clear()
    rt._MODELS_CTX_CACHE.clear()
    rt._LIVE_MODEL_PROBE_INFLIGHT.clear()
    yield
    rt._LIVE_MODEL_PROBE_CACHE.clear()
    rt._MODELS_CTX_CACHE.clear()
    rt._LIVE_MODEL_PROBE_INFLIGHT.clear()


# ---------------------------------------------------------------------------
# (a) /props-only server → n_ctx from props; /models is never consulted.
# ---------------------------------------------------------------------------
def test_props_only_server_uses_props_n_ctx(monkeypatch):
    base = "http://llama:8080/v1"
    models_hits: list[str] = []

    def fake_get(url, headers=None, timeout=None, **kwargs):  # noqa: ANN001, ARG001
        if url.endswith("/props"):
            return _LLAMA_PROPS
        if url.endswith("/models"):
            models_hits.append(url)
            return _FakeResp(200, {"data": []})
        return _FakeResp(404, {})

    monkeypatch.setattr(httpx, "get", fake_get)

    out = probe._do_live_model_probe(base, None, "Qwen")
    assert out["n_ctx"] == 131072
    # props gave a window → /models must NOT be fetched, and the model-keyed cache
    # stays empty.
    assert models_hits == []
    assert base not in rt._MODELS_CTX_CACHE


# ---------------------------------------------------------------------------
# (b) OpenRouter-style /models → n_ctx via the model-keyed map lookup.
# ---------------------------------------------------------------------------
def test_openrouter_models_context_length_by_model_id(monkeypatch):
    base = "https://openrouter.ai/api/v1"
    monkeypatch.setattr(httpx, "get", _route({"/models": _OR_MODELS}))

    # No /props (404) → fall through to /models, looked up by THIS model_id.
    out = probe._do_live_model_probe(base, "sk-key", "deepseek/deepseek-v4")
    assert out["n_ctx"] == 1048576

    # The map covers every model at the base_url; the direct helper looks up by id.
    assert probe._models_context_length(base, "sk-key", "minimax/minimax-m2") == 192000
    # A model present in the listing but missing the field → None.
    assert probe._models_context_length(base, "sk-key", "no-ctx/model") is None
    # A model absent from the listing → None.
    assert probe._models_context_length(base, "sk-key", "ghost/model") is None


# ---------------------------------------------------------------------------
# (c) MiniMax-style /models (no context_length) AND no /props → None → static.
# ---------------------------------------------------------------------------
def test_minimax_no_context_length_falls_back_to_static(monkeypatch):
    base = "https://api.minimaxi.chat/v1"
    monkeypatch.setattr(httpx, "get", _route({"/models": _MINIMAX_MODELS}))

    out = probe._do_live_model_probe(base, "sk-key", "minimax-m2")
    assert out["n_ctx"] is None

    # The caller's pattern (`live["n_ctx"] or entry.context_window`) → static config.
    static_context_window = 192000
    assert (out["n_ctx"] or static_context_window) == static_context_window

    # A successful (200) fetch is still cached as an EMPTY/negative map so we stop
    # re-probing within the TTL — but it must be a tuple(map, ts), not a window.
    cached = rt._MODELS_CTX_CACHE[base]
    assert isinstance(cached, tuple) and cached[0] == {}


# ---------------------------------------------------------------------------
# (d) the /models window is NOT pinned base_url-wide (model-specific isolation).
# ---------------------------------------------------------------------------
def test_models_ctx_isolated_from_base_url_props_cache(monkeypatch):
    base = "https://openrouter.ai/api/v1"
    monkeypatch.setattr(httpx, "get", _route({"/models": _OR_MODELS}))

    # Probe model A.
    probe._do_live_model_probe(base, "k", "deepseek/deepseek-v4")

    # The base_url-keyed props cache must NOT have been pinned to model A's window
    # (props 404 → no props entry written at all).
    props_cached = rt._LIVE_MODEL_PROBE_CACHE.get(base)
    if props_cached is not None:
        value = props_cached[0] if isinstance(props_cached, tuple) else props_cached
        assert value.get("n_ctx") is None, (
            "model-specific /models window leaked into the base_url props cache"
        )

    # Two DIFFERENT models at the SAME base_url resolve to their OWN windows.
    a = rt._probe_live_model(base, "k", "deepseek/deepseek-v4")
    b = rt._probe_live_model(base, "k", "minimax/minimax-m2")
    assert a["n_ctx"] == 1048576
    assert b["n_ctx"] == 192000


# ---------------------------------------------------------------------------
# (e) non-blocking on a running loop → fallback now, worker fills the model cache.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_models_probe_does_not_block_running_loop(monkeypatch):
    base = "https://openrouter.ai/api/v1"
    seen: dict[str, str] = {}

    def slow_get(url, headers=None, timeout=None, **kwargs):  # noqa: ANN001, ARG001
        seen["thread"] = threading.current_thread().name
        if url.endswith("/models"):
            time.sleep(1.0)  # simulate a slow listing
            return _OR_MODELS
        return _FakeResp(404, {})

    monkeypatch.setattr(httpx, "get", slow_get)

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    out = rt._probe_live_model(base, "k", "deepseek/deepseek-v4")
    elapsed = loop.time() - t0

    # Returned right away with the static fallback — the loop was NOT blocked.
    assert elapsed < 0.2, f"probe blocked the event loop for {elapsed:.2f}s"
    assert out == {"model_id": None, "n_ctx": None}

    # The blocking /models fetch ran OFF the loop and filled the MODEL-KEYED cache,
    # so the next call is served live for THIS model.
    await asyncio.sleep(1.4)
    assert seen["thread"] != threading.current_thread().name
    assert rt._probe_live_model(base, "k", "deepseek/deepseek-v4")["n_ctx"] == 1048576
    # The base_url props cache was never pinned to a model-specific window.
    assert base not in rt._LIVE_MODEL_PROBE_CACHE


# ---------------------------------------------------------------------------
# Regression: /props with n_ctx=None must NOT block /models from being
# fetched.  When the /props cache is fresh but has n_ctx=None and the
# model-specific _MODELS_CTX_CACHE is cold, _probe_live_model must schedule
# the /models probe instead of returning early.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_props_n_ctx_none_schedules_models_probe(monkeypatch):
    base = "https://openrouter.ai/api/v1"
    # Fresh /props cache with n_ctx=None (e.g. a server that returns /props
    # but doesn't expose an n_ctx field there).
    rt._LIVE_MODEL_PROBE_CACHE[base] = (
        {"model_id": None, "n_ctx": None},
        time.monotonic(),
    )
    # _MODELS_CTX_CACHE is cold (cleared by autouse fixture).

    # Deterministic synchronization: worker signals started, blocks on
    # release, then invokes the real probe against the httpx fake.
    started = threading.Event()
    release = threading.Event()
    call_count = 0
    real_probe = rt._do_live_model_probe

    def controlled_probe(u, k, mid=None):
        nonlocal call_count
        call_count += 1
        started.set()
        assert release.wait(timeout=5), "worker thread blocked on release"
        return real_probe(u, k, mid)

    monkeypatch.setattr(httpx, "get", _route({"/models": _OR_MODELS}))
    monkeypatch.setattr(rt, "_do_live_model_probe", controlled_probe)

    try:
        out = rt._probe_live_model(base, "k", "deepseek/deepseek-v4")
        # Must return the non-blocking fallback — NOT early-return the bogus
        # n_ctx=None from the props cache.
        assert out == {"model_id": None, "n_ctx": None}

        # Worker was scheduled; inflight marker observable.
        assert started.wait(timeout=5), "worker never started"
        assert base in rt._LIVE_MODEL_PROBE_INFLIGHT

        # Second call while worker is blocked → dedup (no new worker).
        out2 = rt._probe_live_model(base, "k", "deepseek/deepseek-v4")
        assert out2 == {"model_id": None, "n_ctx": None}
        assert call_count == 1
    finally:
        release.set()

    # Wait boundedly for worker completion / cache population.
    deadline = time.monotonic() + 5
    while base in rt._LIVE_MODEL_PROBE_INFLIGHT:
        await asyncio.sleep(0)
        assert time.monotonic() < deadline, "worker did not complete in time"

    # After the probe filled _MODELS_CTX_CACHE, the model-specific window
    # is served from cache.
    cached = rt._probe_live_model(base, "k", "deepseek/deepseek-v4")
    assert cached["n_ctx"] == 1048576


# ---------------------------------------------------------------------------
# Positive control: a fresh (empty) /models negative-cache entry is
# authoritative and suppresses re-probing within the TTL, even when props
# cache is cold and a running loop would otherwise offload a probe.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fresh_empty_models_map_suppresses_reprobe(monkeypatch):
    base = "https://openrouter.ai/api/v1"
    # Fresh empty negative cache (e.g. MiniMax: /models returns 200 but
    # no context_length field on any entry).
    rt._MODELS_CTX_CACHE[base] = ({}, time.monotonic())

    probe_called = False

    def never_probe(u, k, mid=None):
        nonlocal probe_called
        probe_called = True
        return {"model_id": None, "n_ctx": None}

    monkeypatch.setattr(rt, "_do_live_model_probe", never_probe)

    out = rt._probe_live_model(base, "k", "some-model")

    # Returns None from the negative cache, not from a scheduled probe.
    assert out == {"model_id": None, "n_ctx": None}

    # No probe was scheduled — the fresh negative cache short-circuits.
    assert not probe_called
    assert base not in rt._LIVE_MODEL_PROBE_INFLIGHT


def test_valid_legacy_sidecars_migrate_and_remain_rollback_readable(tmp_path):
    db_path = str(tmp_path / "events.db")
    (tmp_path / "events.db.overrides.json").write_text(json.dumps({"conv": "driver-local"}))
    (tmp_path / "events.db.autonomous.json").write_text(json.dumps({"conv": True}))
    (tmp_path / "events.db.surfaces.json").write_text(json.dumps({"conv": "build"}))
    (tmp_path / "events.db.last_model.json").write_text(json.dumps({"model": "driver-local"}))

    repository = RuntimeSettingsRepository(db_path)
    assert repository.document.model_overrides == {"conv": "driver-local"}
    assert repository.document.autonomous == {"conv": True}
    assert repository.document.surfaces == {"conv": "build"}
    assert repository.document.last_selected_model == "driver-local"

    repository.set_value("quiet", "conv", True)
    versioned = json.loads((tmp_path / "events.db.runtime-settings.json").read_text())
    assert versioned["schema_version"] == 1
    assert versioned["quiet"] == {"conv": True}
    assert json.loads((tmp_path / "events.db.quiet.json").read_text()) == {"conv": True}


def test_runtime_settings_restart_reads_one_typed_document(tmp_path):
    db_path = str(tmp_path / "events.db")
    first = RuntimeSettingsRepository(db_path)
    first.set_value("assist", "conv", True)
    first.set_value("model_overrides", "conv", "driver-overflow")
    first.set_last_selected_model("driver-overflow")

    restarted = RuntimeSettingsRepository(db_path)
    assert restarted.document.assist == {"conv": True}
    assert restarted.document.model_overrides == {"conv": "driver-overflow"}
    assert restarted.document.last_selected_model == "driver-overflow"


def test_corrupt_authoritative_runtime_document_fails_explicitly(tmp_path):
    db_path = str(tmp_path / "events.db")
    (tmp_path / "events.db.runtime-settings.json").write_text("{broken")

    with pytest.raises(RuntimeSettingsCorruptionError, match="cannot decode"):
        RuntimeSettingsRepository(db_path)


def test_future_runtime_settings_version_fails_closed(tmp_path):
    db_path = str(tmp_path / "events.db")
    (tmp_path / "events.db.runtime-settings.json").write_text(json.dumps({"schema_version": 2}))

    with pytest.raises(RuntimeSettingsVersionError, match="schema_version 2"):
        RuntimeSettingsRepository(db_path)
