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


def _do_live_model_probe(base_url: str, api_key: str | None) -> dict[str, Any]:
    """The BLOCKING probe body. Must run OFF the event loop (worker thread / sync
    context) — `httpx.get` here waits up to 2s. Populates the module cache on any
    partial success. Never raises."""
    from .runtime import _LIVE_MODEL_PROBE_CACHE  # late-bound: shared cache with _probe_live_model
    out: dict[str, Any] = {"model_id": None, "n_ctx": None}
    try:
        import httpx

        root = base_url.rstrip("/")
        if root.endswith("/v1"):
            root = root[:-3]
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        r = httpx.get(f"{root}/props", headers=headers, timeout=2.0)
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
    # Cache only a SUCCESSFUL probe — so a server that was down at first call is
    # picked up once it comes online (self-healing), instead of being pinned to
    # the static fallback for the agent-server's whole lifetime. Store the
    # monotonic ts alongside the value so _probe_live_model can apply a TTL
    # and re-probe on a model hot-swap (T6/E2).
    if out["model_id"] is not None or out["n_ctx"] is not None:
        _LIVE_MODEL_PROBE_CACHE[base_url] = (out, time.monotonic())
    return out
