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


def _fetch_props(base_url: str, api_key: str | None) -> dict[str, Any] | None:
    """BLOCKING GET {base_url}/props. Returns the parsed JSON body on a 200
    response, else None (any network error or non-200 status). Never raises."""
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
            return r.json()
    except Exception:  # noqa: BLE001 — best effort; the static ModelEntry is the fallback
        pass
    return None


def _parse_props_payload(d: dict[str, Any]) -> tuple[int | None, str | None]:
    """Extract (n_ctx, model_id) from a parsed `/props` JSON body."""
    gen = d.get("default_generation_settings") or {}
    n = gen.get("n_ctx")
    # bool is a subclass of int — exclude it so a stray {"n_ctx": true} can't
    # masquerade as a context window of 1.
    n_ctx = n if isinstance(n, int) and not isinstance(n, bool) and n > 0 else None
    mp = d.get("model_path") or d.get("model")
    model_id = mp if isinstance(mp, str) and mp.strip() else None
    return n_ctx, model_id


def _cache_props_result(base_url: str, model_id: str | None, n_ctx: int | None) -> None:
    """Cache only a SUCCESSFUL /props probe — so a server that was down at first
    call is picked up once it comes online (self-healing), instead of being pinned
    to the static fallback for the agent-server's whole lifetime. Store the
    monotonic ts alongside the value so `_probe_live_model` can apply a TTL and
    re-probe on a model hot-swap (T6/E2). Caches the SERVER-WIDE props fields
    only — a model-specific /models n_ctx must never leak into this
    base_url-keyed cache."""
    if model_id is not None or n_ctx is not None:
        _LIVE_MODEL_PROBE_CACHE[base_url] = (
            {"model_id": model_id, "n_ctx": n_ctx},
            time.monotonic(),
        )


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
    out: dict[str, Any] = {"model_id": None, "n_ctx": None}
    payload = _fetch_props(base_url, api_key)
    if payload is not None:
        out["n_ctx"], out["model_id"] = _parse_props_payload(payload)
    props_n_ctx = out["n_ctx"]
    _cache_props_result(base_url, out["model_id"], props_n_ctx)
    # No server-wide n_ctx from /props → fall back to the model-specific /models
    # context_length (fills _MODELS_CTX_CACHE; the lookup is by model_id).
    if props_n_ctx is None and model_id:
        out["n_ctx"] = _models_context_length(base_url, api_key, model_id)
    return out


def _cached_context_map(base_url: str) -> dict[str, int] | None:
    """The `{id: context_length}` map for base_url, if cached and still fresh
    (within `_PROBE_TTL_S`); else None."""
    cached = _MODELS_CTX_CACHE.get(base_url)
    if not (isinstance(cached, tuple) and len(cached) == 2):
        return None
    cmap, ts = cached
    if (time.monotonic() - ts) <= _PROBE_TTL_S:
        return cmap
    return None


def _fetch_models_response(base_url: str, api_key: str | None) -> tuple[bool, Any]:
    """BLOCKING GET {base_url}/models — the OpenAI-compatible listing under the
    same base (NOT root — unlike /props, do NOT strip a trailing /v1): OpenRouter
    serves it at /api/v1/models, llama.cpp + MiniMax at /v1/models.

    Returns (fetched, body): `fetched` is True as soon as an HTTP 200 comes back
    — even if the body then fails to decode as JSON — matching the original
    probe's "answered but empty" semantics so a malformed body is negative-cached
    rather than retried every call. `body` is the parsed JSON, or None when
    unavailable. Never raises."""
    fetched = False
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
        if r.status_code == 200:
            fetched = True
            return fetched, r.json()
    except Exception:  # noqa: BLE001 — best effort; the static ModelEntry is the fallback
        pass
    return fetched, None


def _parse_models_context_map(d: Any) -> dict[str, int]:
    """Build the `{id: context_length}` map from a parsed `/models` listing body."""
    cmap: dict[str, int] = {}
    entries = d.get("data") if isinstance(d, dict) else d
    if not isinstance(entries, list):
        return cmap
    for e in entries:
        if not isinstance(e, dict):
            continue
        mid = e.get("id")
        cl = e.get("context_length")
        # bool is an int subclass — exclude it (see /props above).
        if isinstance(mid, str) and isinstance(cl, int) and not isinstance(cl, bool) and cl > 0:
            cmap[mid] = cl
    return cmap


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
    cached_map = _cached_context_map(base_url)
    if cached_map is not None:
        return cached_map.get(model_id)
    fetched, payload = _fetch_models_response(base_url, api_key)
    cmap = _parse_models_context_map(payload)
    if fetched:
        _MODELS_CTX_CACHE[base_url] = (cmap, time.monotonic())
    return cmap.get(model_id)


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
