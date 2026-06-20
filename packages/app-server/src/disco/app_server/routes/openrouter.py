"""OpenRouter routes — live catalogue proxy + the encrypted API key.

`GET /api/models/openrouter` proxies the public OpenRouter catalogue (so the
browser dodges CORS and gets a normalized shape); the `/api/openrouter/key`
trio manages the encrypted key stored in `ConfigState`.
"""

from __future__ import annotations

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


def make_openrouter_router(state: ConfigState) -> APIRouter:
    router = APIRouter()

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
        url = _OPENROUTER_MODELS_URL
        if modalities == "all":
            url = f"{url}?output_modalities=all"
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json().get("data", [])
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(status_code=502, detail=f"OpenRouter unreachable: {exc}") from exc
        return normalize_openrouter(data)

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
