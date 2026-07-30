"""Live model identity and context-window probe ownership.

This module owns both probe caches and the nonblocking cache/probe algorithm.
``runtime.py`` re-exports the cache objects and supplies its probe-body alias to
the thin compatibility wrapper, preserving the established monkeypatch seam
without retaining probe policy in the composition facade.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

_LIVE_MODEL_PROBE_CACHE: dict[str, tuple[dict[str, Any], float] | dict[str, Any]] = {}
_PROBE_TTL_S: float = 60.0
_LIVE_MODEL_PROBE_INFLIGHT: set[str] = set()
_MODELS_CTX_CACHE: dict[str, tuple[dict[str, int], float]] = {}


def _model_label(model_id: str) -> str:
    """A short human label from a model_id (drops the gguf/quant noise + provider path)."""
    base = model_id.split("/")[-1].removesuffix(".gguf")
    for suffix in ("-UD-Q5_K_XL", "-UD-Q4_K_XL", "-Q5_K_M", "-Q4_K_M", "-Q4_K_S", "-IQ4_XS"):
        base = base.replace(suffix, "")
    return base


def _do_live_model_probe(
    base_url: str, api_key: str | None, model_id: str | None = None
) -> dict[str, Any]:
    """The BLOCKING probe body. Must run OFF the event loop (worker thread / sync
    context) — `httpx.get` here waits up to 2s. Populates the module cache on any
    partial success. Never raises.

    Two sources, two caches (CW-1):
      - llama.cpp `/props` → a SERVER-WIDE n_ctx (true for every model that backend
        serves) → cached by base_url in `_LIVE_MODEL_PROBE_CACHE`.
      - When `/props` yields no n_ctx (e.g. OpenRouter, which has no `/props`) and a
        `model_id` is known → the MODEL-SPECIFIC `context_length` from `/models`,
        served from `_MODELS_CTX_CACHE` (keyed by base_url, looked up by model_id).
        It is NEVER folded into the base_url-keyed props cache: that would pin one
        model's window onto every model at the same base_url (the codex P1 bug)."""
    out = _probe_props(base_url, api_key)
    props_n_ctx = out["n_ctx"]
    if out["model_id"] is not None or props_n_ctx is not None:
        _LIVE_MODEL_PROBE_CACHE[base_url] = (dict(out), time.monotonic())
    if props_n_ctx is None and model_id:
        out["n_ctx"] = _models_context_length(base_url, api_key, model_id)
    return out


def _probe_props(base_url: str, api_key: str | None) -> dict[str, Any]:
    out: dict[str, Any] = {"model_id": None, "n_ctx": None}
    try:
        import httpx

        root = base_url.rstrip("/")
        if root.endswith("/v1"):
            root = root[:-3]
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        r = httpx.get(
            f"{root}/props",
            headers=headers,
            timeout=2.0,
            follow_redirects=False,
            trust_env=False,
        )
        if r.status_code == 200:
            d = r.json()
            gen = d.get("default_generation_settings") or {}
            n = gen.get("n_ctx")
            # bool is a subclass of int — exclude it so a stray {"n_ctx": true}
            # can't masquerade as a context window of 1.
            if isinstance(n, int) and not isinstance(n, bool) and n > 0:
                out["n_ctx"] = n
            mp = d.get("model_path") or d.get("model")
            if isinstance(mp, str) and mp.strip():
                out["model_id"] = mp
    except Exception:  # noqa: BLE001 — best effort; the static ModelEntry is the fallback
        pass
    return out


def _models_context_length(base_url: str, api_key: str | None, model_id: str | None) -> int | None:
    """Best-effort MODEL-SPECIFIC context window from the OpenAI-compatible
    `GET {base_url}/models` listing. OpenRouter exposes `context_length` per model;
    MiniMax omits it. BLOCKING httpx — must run OFF the event loop, same constraint
    as `_do_live_model_probe`. One fetch builds the `{id: context_length}` map for
    every model at the base_url and caches it in `_MODELS_CTX_CACHE` (keyed by
    base_url, TTL `_PROBE_TTL_S`); the per-model value is then looked up by model_id,
    so a model-specific window is NEVER pinned base_url-wide. Returns the model's
    context_length or None on any miss → caller uses the static config. A successful
    fetch is cached even when the map is empty / lacks this model (negative cache:
    MiniMax stops re-probing within the TTL); only a transport failure is left
    uncached so it self-heals. Never raises."""
    if not base_url or not model_id:
        return None
    cached = _fresh_context_length(base_url, model_id)
    if isinstance(cached, int) or cached is None:
        return cached
    cmap = _fetch_model_windows(base_url, api_key)
    if cmap is not None:
        _MODELS_CTX_CACHE[base_url] = (cmap, time.monotonic())
        return cmap.get(model_id)
    return None


_CACHE_MISS = object()


def _fresh_context_length(base_url: str, model_id: str) -> int | None | object:
    cached = _MODELS_CTX_CACHE.get(base_url)
    if not (isinstance(cached, tuple) and len(cached) == 2):
        return _CACHE_MISS
    context_by_model, timestamp = cached
    if (time.monotonic() - timestamp) > _PROBE_TTL_S:
        return _CACHE_MISS
    return context_by_model.get(model_id)


def _fetch_model_windows(base_url: str, api_key: str | None) -> dict[str, int] | None:
    try:
        import httpx

        root = base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        r = httpx.get(
            f"{root}/models",
            headers=headers,
            timeout=2.0,
            follow_redirects=False,
            trust_env=False,
        )
        if r.status_code != 200:
            return None
        data = r.json()
    except Exception:  # noqa: BLE001 — best effort; the static ModelEntry is the fallback
        return None
    entries = data.get("data") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        return {}
    windows: dict[str, int] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        model_id = entry.get("id")
        context_length = entry.get("context_length")
        if (
            isinstance(model_id, str)
            and isinstance(context_length, int)
            and not isinstance(context_length, bool)
            and context_length > 0
        ):
            windows[model_id] = context_length
    return windows


def _probe_live_model(
    base_url: str | None,
    api_key: str | None = None,
    model_id: str | None = None,
    *,
    probe_body: Callable[..., dict[str, Any]] = _do_live_model_probe,
) -> dict[str, Any]:
    """Return the cached live model identity/window without blocking an event loop."""
    if not base_url:
        return {"model_id": None, "n_ctx": None}

    props = _fresh_props(base_url)
    models_map = _fresh_model_windows(base_url, model_id)
    cached_result = _cached_result(props, models_map, model_id)
    if cached_result is not None:
        return cached_result

    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is None:
        return _run_probe(probe_body, base_url, api_key, model_id)
    _schedule_probe(running, probe_body, base_url, api_key, model_id)
    return {"model_id": None, "n_ctx": None}


def _fresh_props(base_url: str) -> dict[str, Any] | None:
    cached = _LIVE_MODEL_PROBE_CACHE.get(base_url)
    if cached is None:
        return None
    if isinstance(cached, tuple) and len(cached) == 2:
        value, timestamp = cached
        return value if (time.monotonic() - timestamp) <= _PROBE_TTL_S else None
    return cached


def _fresh_model_windows(
    base_url: str,
    model_id: str | None,
) -> dict[str, int] | None:
    if model_id is None:
        return None
    cached = _MODELS_CTX_CACHE.get(base_url)
    if not (isinstance(cached, tuple) and len(cached) == 2):
        return None
    windows, timestamp = cached
    return windows if (time.monotonic() - timestamp) <= _PROBE_TTL_S else None


def _cached_result(
    props: dict[str, Any] | None,
    model_windows: dict[str, int] | None,
    model_id: str | None,
) -> dict[str, Any] | None:
    if model_id is None:
        return props
    if (props is None or props.get("n_ctx") is None) and model_windows is None:
        return None
    n_ctx = props["n_ctx"] if props is not None else None
    if n_ctx is None and model_windows is not None:
        n_ctx = model_windows.get(model_id)
    return {
        "model_id": props["model_id"] if props is not None else None,
        "n_ctx": n_ctx,
    }


def _run_probe(
    probe_body: Callable[..., dict[str, Any]],
    base_url: str,
    api_key: str | None,
    model_id: str | None,
) -> dict[str, Any]:
    if model_id is None:
        return probe_body(base_url, api_key)
    return probe_body(base_url, api_key, model_id)


def _schedule_probe(
    loop: asyncio.AbstractEventLoop,
    probe_body: Callable[..., dict[str, Any]],
    base_url: str,
    api_key: str | None,
    model_id: str | None,
) -> None:
    if base_url in _LIVE_MODEL_PROBE_INFLIGHT:
        return
    _LIVE_MODEL_PROBE_INFLIGHT.add(base_url)

    def probe_then_clear() -> None:
        try:
            _run_probe(probe_body, base_url, api_key, model_id)
        finally:
            _LIVE_MODEL_PROBE_INFLIGHT.discard(base_url)

    loop.run_in_executor(None, probe_then_clear)
