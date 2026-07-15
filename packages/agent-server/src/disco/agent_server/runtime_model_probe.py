"""Live model-label + /props probe helpers — extracted from `runtime.py`.

Extracted from `runtime.py` (god-file decomposition, pure move). Two pure
helpers — `_model_label` (string cleanup of a model_id for UI display) and
`_do_live_model_probe` (the blocking llama.cpp /props probe that fills the
shared cache `_probe_live_model` reads from) — moved here so the runtime
god-file shrinks without changing any behavior. `_probe_live_model` itself
stays in runtime.py: the probe tests (`test_live_probe_nonblocking`,
`test_probe_ttl`) `monkeypatch.setattr(rt, "_do_live_model_probe", ...)` and
then call `rt._probe_live_model(...)`, which resolves the patched name in
runtime.py's module globals. The shared cache `_LIVE_MODEL_PROBE_CACHE` also
stays in runtime.py for the same reason — the tests do `rt._LIVE_MODEL_PROBE_CACHE
.clear()` / `[base_url] = ...` directly. The new module reaches it via a
function-local import (late-bound) to avoid a circular-import error at module
load time: runtime.py imports this module, so we cannot `from .runtime import
_LIVE_MODEL_PROBE_CACHE` at the top here.
"""

from __future__ import annotations

import time
from typing import Any


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
    from .runtime import _LIVE_MODEL_PROBE_CACHE  # late-bound: shared cache with _probe_live_model

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
    # Cache only a SUCCESSFUL /props probe — so a server that was down at first call
    # is picked up once it comes online (self-healing), instead of being pinned to
    # the static fallback for the agent-server's whole lifetime. Store the monotonic
    # ts alongside the value so _probe_live_model can apply a TTL and re-probe on a
    # model hot-swap (T6/E2). Cache a COPY of the SERVER-WIDE props fields only: `out`
    # may be augmented with a model-specific /models n_ctx below, which must not leak
    # into this base_url-keyed cache.
    props_n_ctx = out["n_ctx"]
    if out["model_id"] is not None or props_n_ctx is not None:
        _LIVE_MODEL_PROBE_CACHE[base_url] = (
            {"model_id": out["model_id"], "n_ctx": props_n_ctx},
            time.monotonic(),
        )
    # No server-wide n_ctx from /props → fall back to the model-specific /models
    # context_length (fills _MODELS_CTX_CACHE; the lookup is by model_id).
    if props_n_ctx is None and model_id:
        out["n_ctx"] = _models_context_length(base_url, api_key, model_id)
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
    from .runtime import _MODELS_CTX_CACHE, _PROBE_TTL_S  # late-bound: shared cache

    if not base_url or not model_id:
        return None
    cached = _MODELS_CTX_CACHE.get(base_url)
    if isinstance(cached, tuple) and len(cached) == 2:
        cmap, ts = cached
        if (time.monotonic() - ts) <= _PROBE_TTL_S:
            return cmap.get(model_id)
    cmap: dict[str, int] = {}
    fetched = False
    try:
        import httpx

        # /models is the OpenAI-compatible listing under the same base (NOT root —
        # unlike /props, do NOT strip a trailing /v1): OpenRouter serves it at
        # /api/v1/models, llama.cpp + MiniMax at /v1/models.
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
            d = r.json()
            entries = d.get("data") if isinstance(d, dict) else d
            if isinstance(entries, list):
                for e in entries:
                    if not isinstance(e, dict):
                        continue
                    mid = e.get("id")
                    cl = e.get("context_length")
                    # bool is an int subclass — exclude it (see /props above).
                    if (
                        isinstance(mid, str)
                        and isinstance(cl, int)
                        and not isinstance(cl, bool)
                        and cl > 0
                    ):
                        cmap[mid] = cl
    except Exception:  # noqa: BLE001 — best effort; the static ModelEntry is the fallback
        pass
    if fetched:
        _MODELS_CTX_CACHE[base_url] = (cmap, time.monotonic())
    return cmap.get(model_id)
