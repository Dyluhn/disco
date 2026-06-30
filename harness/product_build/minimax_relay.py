"""MiniMax relay (P1B-LIVE-3a) — the disclaude permanent home of the proven local relay.

Disco's build driver is OpenAI-compatible; the MiniMax coding-plan token works on MiniMax's
OpenAI-compatible endpoint (live-verified: api.minimaxi.chat/v1 + api.minimax.io/v1 → 200,
NOT api.minimaxi.com). So this is a thin OpenAI passthrough that holds MINIMAX_API_KEY (Disco's
gateway stays key-free), maps the config model id → MiniMax's real id, clamps an oversized
max_tokens, and streams SSE back. Direct MiniMax plan key ONLY — never OpenRouter.

The pure request transform + config + host parse are fastapi/httpx/secret-free so they unit-test
in isolation; the FastAPI proxy (and the env key read) are built lazily by create_app().
"""

import json
import os
import time
from typing import Any, AsyncIterator
from urllib.parse import urlparse

# NOTE: no `from __future__ import annotations` here — FastAPI introspects the proxy handler's
# annotations at runtime, and Request is imported lazily inside create_app(); a stringized
# annotation would make FastAPI treat `request` as a query param (HTTP 422). Annotations stay
# eager; fastapi/httpx are still imported lazily, so the pure import path remains dep-light.

UPSTREAM = os.environ.get("MINIMAX_UPSTREAM", "https://api.minimaxi.chat/v1")
DEFAULT_MODEL = os.environ.get("MINIMAX_MODEL", "MiniMax-M3")  # P17: M3 (source relay defaulted M2)
MAX_TOKENS_CAP = 131072  # MiniMax-M3 400s on an oversized max_tokens; the gateway can emit ~4M
MODEL_MAP = {
    "minimax/minimax-m3": "MiniMax-M3",
    "minimax-m3": "MiniMax-M3",
    "minimax/minimax-m2": "MiniMax-M2",
    "minimax-m2": "MiniMax-M2",
}


def map_model(model: Any, *, default: str = DEFAULT_MODEL, model_map: dict[str, str] = MODEL_MAP) -> str:
    return model_map.get(model, default)


def _clamp_max_tokens(mt: Any) -> int:
    # keep ONLY a positive non-bool int within the cap; everything else (absent/bool/<=0/>cap) → cap.
    return mt if (type(mt) is int and 0 < mt <= MAX_TOKENS_CAP) else MAX_TOKENS_CAP


def transform_request(
    body: Any, *, default_model: str = DEFAULT_MODEL, model_map: dict[str, str] = MODEL_MAP
) -> Any:
    """Map the model id + clamp max_tokens. Returns a NEW dict (no input mutation); every other
    field passes through unchanged."""
    if not isinstance(body, dict):
        return body
    out = dict(body)
    if "model" in out:
        out["model"] = model_map.get(out["model"], default_model)
    out["max_tokens"] = _clamp_max_tokens(out.get("max_tokens"))
    return out


def upstream_host(url: str) -> str:
    """The host of an upstream URL — for the P17 provider-call-ledger (must show the REAL
    minimaxi.chat host, not the localhost relay)."""
    return urlparse(url).hostname or ""


def relay_log_record(url: str, model: Any, *, has_tools: bool = False) -> dict[str, Any]:
    # [REL-5b] `has_tools` distinguishes a BUILD-driver request (always carries the tool catalog)
    # from a tool-less SUMMARIZER request (the async auto-title fires off kick() with NO tools).
    # The provider-after-terminal oracle counts only build-driver (tool-bearing) calls, so a benign
    # post-terminal auto-title call is not miscounted as a build runaway — while a REAL post-terminal
    # driver runaway (which carries tools) is still flagged.
    return {
        "host": upstream_host(url),
        "url": url,
        "model": model,
        "ts": time.time(),
        "has_tools": bool(has_tools),
    }


def create_app() -> Any:
    """Build the FastAPI proxy. Reads MINIMAX_API_KEY from the env (fail-fast if unset) — done
    HERE, not at import, so the pure transform stays importable + testable without the secret."""
    key = os.environ.get("MINIMAX_API_KEY")
    if not key:  # check the secret FIRST so fail-fast can't become a ModuleNotFoundError
        raise RuntimeError("MINIMAX_API_KEY is required (env only — never hardcode the MiniMax token)")
    import httpx
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, StreamingResponse
    log_path = os.environ.get("MINIMAX_RELAY_LOG")  # optional JSONL relay log (for the ledger)

    def _log(rec: dict[str, Any]) -> None:
        line = json.dumps(rec, sort_keys=True)
        print(f"RELAY {line}", flush=True)
        if log_path:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    app = FastAPI()
    auth = {"Authorization": f"Bearer {key}", "Content-Type": "application/json", "Accept-Encoding": "identity"}

    @app.get("/v1/models")
    async def models() -> Any:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(f"{UPSTREAM}/models", headers={"Authorization": f"Bearer {key}"})
        return JSONResponse(status_code=r.status_code, content=r.json())

    @app.post("/v1/{path:path}")
    async def proxy(path: str, request: Request) -> Any:
        body = transform_request(await request.json())
        url = f"{UPSTREAM}/{path}"
        _log(
            relay_log_record(
                url,
                body.get("model") if isinstance(body, dict) else None,
                has_tools=bool(isinstance(body, dict) and body.get("tools")),
            )
        )
        if isinstance(body, dict) and body.get("stream"):
            # send first so we know the REAL upstream status (a 401/400 must NOT surface as 200),
            # then stream the body and close the client when done.
            client = httpx.AsyncClient(timeout=600)
            req = client.build_request("POST", url, json=body, headers=auth)
            r = await client.send(req, stream=True)

            async def gen() -> AsyncIterator[bytes]:
                try:
                    async for chunk in r.aiter_bytes():
                        yield chunk
                finally:
                    await r.aclose()
                    await client.aclose()

            return StreamingResponse(
                gen(),
                status_code=r.status_code,
                media_type=r.headers.get("content-type", "text/event-stream"),
            )
        async with httpx.AsyncClient(timeout=600) as c:
            r = await c.post(url, json=body, headers=auth)
        return JSONResponse(status_code=r.status_code, content=r.json())

    return app


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=int(os.environ.get("MINIMAX_RELAY_PORT", "8080")))
