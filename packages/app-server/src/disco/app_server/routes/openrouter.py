"""OpenRouter routes — live catalogue proxy + the encrypted API key.

`GET /api/models/openrouter` proxies the public OpenRouter catalogue (so the
browser dodges CORS and gets a normalized shape); the `/api/openrouter/key`
trio manages the encrypted key stored in `ConfigState`.
"""

from __future__ import annotations

import asyncio
import time

import httpx
from fastapi import APIRouter, HTTPException, Query

from ..config.dtos import (
    OpenRouterKeyBody,
    OpenRouterKeyStatus,
    OpenRouterModelDTO,
)
from ..config.mappers import normalize_openrouter
from ..config_state import ConfigState

_OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
_OPENROUTER_ENDPOINTS_URL = "https://openrouter.ai/api/v1/models/{slug}/endpoints"
# The image catalogue (modalities=all) fans out to ~34 /endpoints sub-requests for
# pricing; cache the enriched result so a direct/uncached caller can't re-trigger that.
_IMG_CACHE_TTL_S = 600.0
# Bounds so a slow OpenRouter can't make the route hang: each sub-request times out
# fast, and the whole enrichment is wall-clock-budgeted (partial → models fall back).
_ENDPOINT_TIMEOUT_S = 8.0
_ENRICH_BUDGET_S = 15.0


async def _enrich_image_pricing(
    client: httpx.AsyncClient, models: list[OpenRouterModelDTO]
) -> None:
    """Fill `image_price_per_m` for image-output models from each model's /endpoints
    `image_output` rate (USD/token → USD per M tokens). The /models catalogue reports 0
    token price for the dedicated generators (FLUX/Recraft/Seedream), so their real cost
    only lives in /endpoints. Bounded-concurrency + a wall-clock budget; best-effort: a
    failed/slow lookup leaves 0 (the UI then shows the token price or 'pricing on
    openrouter.ai'), and never hangs or fails the whole route. The rate is the MAX across
    a model's endpoints — a conservative ceiling, never an understatement of cost."""
    targets = [m for m in models if m.image_output]
    sem = asyncio.Semaphore(8)

    async def one(m: OpenRouterModelDTO) -> None:
        async with sem:
            try:
                r = await client.get(
                    _OPENROUTER_ENDPOINTS_URL.format(slug=m.id), timeout=_ENDPOINT_TIMEOUT_S
                )
                r.raise_for_status()
                eps = (r.json().get("data") or {}).get("endpoints") or []
            except (httpx.HTTPError, ValueError):
                return
            rates: list[float] = []
            for e in eps:
                try:
                    rates.append(float((e.get("pricing") or {}).get("image_output") or 0))
                except (TypeError, ValueError):
                    continue
            if rates:
                m.image_price_per_m = max(rates) * 1_000_000  # USD per M image tokens

    try:
        await asyncio.wait_for(asyncio.gather(*(one(m) for m in targets)), timeout=_ENRICH_BUDGET_S)
    except TimeoutError:
        pass  # partial enrichment is fine — unenriched models fall back in the UI


def make_openrouter_router(state: ConfigState) -> APIRouter:
    router = APIRouter()
    # Per-app TTL cache + singleflight lock for the ENRICHED image catalogue (the only
    # expensive path). Closure-scoped (not a module global) so tests get a fresh cache.
    _img_cache: dict[str, tuple[float, list[OpenRouterModelDTO]]] = {}
    _img_lock = asyncio.Lock()

    async def _fetch(all_modalities: bool) -> list[OpenRouterModelDTO]:
        url = _OPENROUTER_MODELS_URL + ("?output_modalities=all" if all_modalities else "")
        async with httpx.AsyncClient(
            timeout=20.0,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            models = normalize_openrouter(resp.json().get("data", []))
            # Only the image picker (=all) pays the per-endpoint pricing enrichment;
            # the LLM browser's default fetch stays a single request.
            if all_modalities:
                await _enrich_image_pricing(client, models)
        return models

    @router.get("/api/models/openrouter")
    async def get_openrouter_models(
        modalities: str | None = Query(
            None,
            description=(
                "Pass 'all' to include non-text output models (image generators like "
                "FLUX/Recraft/Seedream). OpenRouter's /models defaults to text-output only, "
                "so the image-gen picker needs this; the LLM browser omits it."
            ),
        ),
    ) -> list[OpenRouterModelDTO]:
        # Public endpoint (no key needed to list). Proxied so the browser avoids
        # CORS and gets a normalized shape. Adding a model reuses POST /api/models.
        try:
            if modalities != "all":
                return await _fetch(False)  # cheap single request; uncached
            # Enriched image catalogue: serve from TTL cache; singleflight so concurrent
            # callers collapse onto ONE fan-out instead of each firing ~34 sub-requests.
            hit = _img_cache.get("all")
            if hit and time.monotonic() - hit[0] < _IMG_CACHE_TTL_S:
                return hit[1]
            async with _img_lock:
                hit = _img_cache.get("all")
                if hit and time.monotonic() - hit[0] < _IMG_CACHE_TTL_S:
                    return hit[1]
                models = await _fetch(True)
                _img_cache["all"] = (time.monotonic(), models)
                return models
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(status_code=502, detail=f"OpenRouter unreachable: {exc}") from exc

    @router.get("/api/openrouter/key")
    async def get_openrouter_key() -> OpenRouterKeyStatus:
        return state.openrouter_key_status()

    @router.put("/api/openrouter/key")
    async def put_openrouter_key(body: OpenRouterKeyBody) -> OpenRouterKeyStatus:
        try:
            return state.set_openrouter_key(body.key)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.delete("/api/openrouter/key")
    async def delete_openrouter_key() -> OpenRouterKeyStatus:
        return state.clear_openrouter_key()

    return router
