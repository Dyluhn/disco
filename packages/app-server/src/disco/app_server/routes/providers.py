"""Generic provider routes — add a key, browse /models, toggle into catalogue.

This intentionally lives alongside the dedicated OpenRouter routes. The runtime
still sees only ordinary catalogue entries; provider objects are settings
metadata plus an encrypted SecretStore ref.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable

import httpx
from fastapi import APIRouter, HTTPException, Response

from ..config.dtos import (
    ModelDTO,
    ProviderCatalogueModelDTO,
    ProviderCreate,
    ProviderDTO,
    ProviderEnableBody,
    ProviderMutationResult,
    ProviderPatch,
    ProviderPresetDTO,
)
from ..config_state import ConfigState, ProviderInUseError

_CATALOGUE_TTL_S = 600.0
_ANTHROPIC_VERSION = "2023-06-01"

PROVIDER_PRESETS: tuple[ProviderPresetDTO, ...] = (
    ProviderPresetDTO(
        id="openrouter",
        label="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        kind="openai-compat",
    ),
    ProviderPresetDTO(
        id="openai",
        label="OpenAI",
        base_url="https://api.openai.com/v1",
        kind="openai-compat",
    ),
    ProviderPresetDTO(
        id="anthropic",
        label="Anthropic",
        base_url="https://api.anthropic.com/v1",
        kind="anthropic",
    ),
    ProviderPresetDTO(
        id="groq",
        label="Groq",
        base_url="https://api.groq.com/openai/v1",
        kind="openai-compat",
    ),
    ProviderPresetDTO(
        id="deepseek",
        label="DeepSeek",
        base_url="https://api.deepseek.com",
        kind="openai-compat",
    ),
    ProviderPresetDTO(
        id="together",
        label="Together",
        base_url="https://api.together.xyz/v1",
        kind="openai-compat",
    ),
    ProviderPresetDTO(
        id="fireworks",
        label="Fireworks",
        base_url="https://api.fireworks.ai/inference/v1",
        kind="openai-compat",
    ),
    ProviderPresetDTO(
        id="mistral",
        label="Mistral",
        base_url="https://api.mistral.ai/v1",
        kind="openai-compat",
    ),
    ProviderPresetDTO(
        id="xai",
        label="xAI",
        base_url="https://api.x.ai/v1",
        kind="openai-compat",
    ),
    ProviderPresetDTO(
        id="opencode-go",
        label="OpenCode Go",
        base_url="https://opencode.ai/zen/go/v1",
        kind="openai-compat",
    ),
    ProviderPresetDTO(
        id="custom-openai-compatible",
        label="Custom (OpenAI-compatible)",
        base_url="",
        kind="openai-compat",
        requires_base_url=True,
    ),
)


def _models_url(base_url: str) -> str:
    trimmed = base_url.rstrip("/")
    if trimmed.endswith("/models"):
        return trimmed
    return f"{trimmed}/models"


def _to_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _token_price_per_m(value: object) -> float | None:
    price = _to_float(value)
    if price is None:
        return None
    # OpenRouter reports USD/token; a few compatible catalogues report USD/Mtok.
    # Values below one cent are token rates in every known /models catalogue.
    return price * 1_000_000 if abs(price) < 0.01 else price


def _context(value: object) -> int | None:
    try:
        parsed = int(value) if value is not None else 0
    except (TypeError, ValueError):
        return None
    return parsed or None


def _caps(
    *,
    context_window: int | None,
    input_modalities: Iterable[object] = (),
    supported_parameters: Iterable[object] = (),
    explicit: Iterable[object] = (),
) -> list[str]:
    out: set[str] = set()
    params = {str(p) for p in supported_parameters}
    modalities = {str(m) for m in input_modalities}
    for value in explicit:
        if str(value) in {"vision", "long_context", "tool_calling", "json_mode"}:
            out.add(str(value))
    if "image" in modalities:
        out.add("vision")
    if "tools" in params or "tool_choice" in params:
        out.add("tool_calling")
    if "response_format" in params or "json_schema" in params:
        out.add("json_mode")
    if (context_window or 0) >= 32_000:
        out.add("long_context")
    return sorted(out)


def _normalize_openai_compat(payload: dict) -> list[ProviderCatalogueModelDTO]:
    out: list[ProviderCatalogueModelDTO] = []
    data = payload.get("data")
    if not isinstance(data, list):
        return out
    for raw in data:
        if not isinstance(raw, dict) or raw.get("id") is None:
            continue
        pricing = raw.get("pricing") if isinstance(raw.get("pricing"), dict) else {}
        arch = raw.get("architecture") if isinstance(raw.get("architecture"), dict) else {}
        context_window = _context(
            raw.get("context_length")
            or raw.get("context_window")
            or raw.get("contextWindow")
            or raw.get("max_context_tokens")
        )
        model_id = str(raw["id"])
        out.append(
            ProviderCatalogueModelDTO(
                model_id=model_id,
                label=str(raw.get("name") or raw.get("display_name") or model_id),
                context_window=context_window,
                price_in_per_m=_token_price_per_m(
                    pricing.get("prompt") or pricing.get("input") or pricing.get("input_token")
                ),
                price_out_per_m=_token_price_per_m(
                    pricing.get("completion")
                    or pricing.get("output")
                    or pricing.get("output_token")
                ),
                capabilities=_caps(
                    context_window=context_window,
                    input_modalities=arch.get("input_modalities") or (),
                    supported_parameters=raw.get("supported_parameters") or (),
                    explicit=raw.get("capabilities") or (),
                ),
            )
        )
    return out


def _normalize_anthropic(payload: dict) -> list[ProviderCatalogueModelDTO]:
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    out: list[ProviderCatalogueModelDTO] = []
    for raw in data:
        if not isinstance(raw, dict) or raw.get("id") is None:
            continue
        model_id = str(raw["id"])
        out.append(
            ProviderCatalogueModelDTO(
                model_id=model_id,
                label=str(raw.get("display_name") or model_id),
                context_window=_context(raw.get("context_window") or raw.get("input_token_limit")),
            )
        )
    return out


def _normalize_gemini(payload: dict) -> list[ProviderCatalogueModelDTO]:
    data = payload.get("models")
    if not isinstance(data, list):
        return []
    out: list[ProviderCatalogueModelDTO] = []
    for raw in data:
        if not isinstance(raw, dict) or raw.get("name") is None:
            continue
        model_id = str(raw["name"]).removeprefix("models/")
        context_window = _context(raw.get("inputTokenLimit"))
        out.append(
            ProviderCatalogueModelDTO(
                model_id=model_id,
                label=str(raw.get("displayName") or model_id),
                context_window=context_window,
                capabilities=_caps(context_window=context_window),
            )
        )
    return out


def _normalize_catalogue(kind: str, payload: dict) -> list[ProviderCatalogueModelDTO]:
    if kind == "openai-compat":
        return _normalize_openai_compat(payload)
    if kind == "anthropic":
        return _normalize_anthropic(payload)
    if kind == "gemini":
        return _normalize_gemini(payload)
    return []


async def _fetch_provider_catalogue(
    provider: ProviderDTO,
    api_key: str,
) -> list[ProviderCatalogueModelDTO]:
    url = _models_url(provider.base_url)
    headers: dict[str, str] = {}
    params: dict[str, str] = {}
    if provider.kind == "openai-compat":
        headers["Authorization"] = f"Bearer {api_key}"
    elif provider.kind == "anthropic":
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = _ANTHROPIC_VERSION
    elif provider.kind == "gemini":
        params["key"] = api_key
    async with httpx.AsyncClient(
        timeout=20.0,
        trust_env=False,
        follow_redirects=False,
    ) as client:
        resp = await client.get(url, headers=headers, params=params)
        resp.raise_for_status()
        return _normalize_catalogue(provider.kind, resp.json())


def _error_text(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"{exc.response.status_code} from /models"
    return str(exc) or exc.__class__.__name__


def make_providers_router(state: ConfigState) -> APIRouter:
    router = APIRouter()
    cache: dict[str, tuple[float, list[ProviderCatalogueModelDTO]]] = {}
    locks: dict[str, asyncio.Lock] = {}

    async def _probe(provider: ProviderDTO, api_key: str) -> tuple[bool, str | None]:
        try:
            await _fetch_provider_catalogue(provider, api_key)
            return True, None
        except (httpx.HTTPError, ValueError) as exc:
            return False, _error_text(exc)

    async def _cached_models(provider: ProviderDTO) -> list[ProviderCatalogueModelDTO]:
        hit = cache.get(provider.id)
        if hit and time.monotonic() - hit[0] < _CATALOGUE_TTL_S:
            return hit[1]
        lock = locks.setdefault(provider.id, asyncio.Lock())
        async with lock:
            hit = cache.get(provider.id)
            if hit and time.monotonic() - hit[0] < _CATALOGUE_TTL_S:
                return hit[1]
            key = state._resolve_secret_value(provider.secret_name)
            if not key:
                raise HTTPException(
                    status_code=400,
                    detail=f"No decryptable key stored for {provider.label}.",
                )
            try:
                models = await _fetch_provider_catalogue(provider, key)
            except (httpx.HTTPError, ValueError) as exc:
                raise HTTPException(
                    status_code=502,
                    detail=f"{provider.label} /models probe failed: {_error_text(exc)}",
                ) from exc
            cache[provider.id] = (time.monotonic(), models)
            return models

    def _provider_or_404(provider_id: str) -> ProviderDTO:
        try:
            return state._provider_dto(state.provider_settings(provider_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"unknown provider {provider_id!r}") from exc

    @router.get("/api/providers/presets")
    async def get_provider_presets() -> list[ProviderPresetDTO]:
        return list(PROVIDER_PRESETS)

    @router.get("/api/providers")
    async def get_providers() -> list[ProviderDTO]:
        return state.providers()

    @router.post("/api/providers", status_code=201)
    async def post_provider(body: ProviderCreate) -> ProviderMutationResult:
        try:
            provider = state.create_provider(body)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        ok, error = await _probe(provider, body.api_key.strip())
        cache.pop(provider.id, None)
        return ProviderMutationResult(provider=provider, catalogue_ok=ok, catalogue_error=error)

    @router.put("/api/providers/{provider_id}")
    async def put_provider(provider_id: str, body: ProviderPatch) -> ProviderMutationResult:
        try:
            provider = state.update_provider(provider_id, body)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"unknown provider {provider_id!r}") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        key = body.api_key.strip() if body.api_key else state._resolve_secret_value(provider.secret_name)
        if key:
            ok, error = await _probe(provider, key)
        else:
            ok, error = False, f"No decryptable key stored for {provider.label}."
        cache.pop(provider.id, None)
        return ProviderMutationResult(provider=provider, catalogue_ok=ok, catalogue_error=error)

    @router.delete("/api/providers/{provider_id}", status_code=204)
    async def delete_provider(provider_id: str) -> Response:
        try:
            state.delete_provider(provider_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"unknown provider {provider_id!r}") from exc
        except ProviderInUseError as exc:
            raise HTTPException(
                status_code=409,
                detail=f"Provider is still used by catalogue models: {', '.join(exc.model_names)}",
            ) from exc
        cache.pop(provider_id, None)
        return Response(status_code=204)

    @router.get("/api/providers/{provider_id}/models")
    async def get_provider_models(provider_id: str) -> list[ProviderCatalogueModelDTO]:
        return await _cached_models(_provider_or_404(provider_id))

    @router.post("/api/providers/{provider_id}/enable")
    async def enable_provider_model(
        provider_id: str,
        body: ProviderEnableBody,
    ) -> list[ModelDTO]:
        provider = _provider_or_404(provider_id)
        catalogue_model = None
        hit = cache.get(provider.id)
        if hit and time.monotonic() - hit[0] < _CATALOGUE_TTL_S:
            catalogue_model = next((m for m in hit[1] if m.model_id == body.model_id), None)
        try:
            return state.enable_provider_model(provider_id, body, catalogue_model)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.delete("/api/providers/{provider_id}/enable/{catalogue_id}")
    async def disable_provider_model(provider_id: str, catalogue_id: str) -> list[ModelDTO]:
        _provider_or_404(provider_id)
        try:
            return state.disable_provider_model(provider_id, catalogue_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return router
