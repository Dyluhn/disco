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
import threading
import time

import httpx
import pytest
from disco.agent_server import runtime as rt
from disco.agent_server import runtime_model_probe as probe


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

    def fake_get(url: str, headers=None, timeout=None):  # noqa: ANN001
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
_MINIMAX_MODELS = _FakeResp(
    200, {"data": [{"id": "abab6.5-chat"}, {"id": "minimax-m2"}]}
)
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

    def fake_get(url, headers=None, timeout=None):  # noqa: ANN001
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

    def slow_get(url, headers=None, timeout=None):  # noqa: ANN001
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
