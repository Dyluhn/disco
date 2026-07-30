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
        parsed = int(value) if isinstance(value, (int, float, str)) else 0
    except (TypeError, ValueError):
        return None
    return parsed or None


def _output_limit(*values: object) -> int | None:
    """Return the first positive provider-declared output-token limit."""

    for value in values:
        parsed = _context(value)
        if parsed is not None and parsed > 0:
            return parsed
    return None


def _first_value(values: dict, *keys: str) -> object:
    for key in keys:
        value = values.get(key)
        if value is not None:
            return value
    return None


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
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    return [
        model
        for raw in data
        if isinstance(raw, dict) and (model := _normalize_openai_model(raw)) is not None
    ]


def _normalize_openai_model(raw: dict) -> ProviderCatalogueModelDTO | None:
    if raw.get("id") is None:
        return None
    raw_pricing = raw.get("pricing")
    raw_arch = raw.get("architecture")
    pricing = raw_pricing if isinstance(raw_pricing, dict) else {}
    architecture = raw_arch if isinstance(raw_arch, dict) else {}
    raw_top_provider = raw.get("top_provider")
    top_provider = raw_top_provider if isinstance(raw_top_provider, dict) else {}
    context_window = _context(
        _first_value(
            raw,
            "context_length",
            "context_window",
            "contextWindow",
            "max_context_tokens",
        )
    )
    model_id = str(raw["id"])
    return ProviderCatalogueModelDTO(
        model_id=model_id,
        label=str(raw.get("name") or raw.get("display_name") or model_id),
        context_window=context_window,
        max_output_tokens=_output_limit(
            raw.get("max_output_tokens"),
            raw.get("max_completion_tokens"),
            raw.get("output_token_limit"),
            top_provider.get("max_completion_tokens"),
        ),
        price_in_per_m=_token_price_per_m(_first_value(pricing, "prompt", "input", "input_token")),
        price_out_per_m=_token_price_per_m(
            _first_value(pricing, "completion", "output", "output_token")
        ),
        capabilities=_caps(
            context_window=context_window,
            input_modalities=architecture.get("input_modalities") or (),
            supported_parameters=raw.get("supported_parameters") or (),
            explicit=raw.get("capabilities") or (),
        ),
    )


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
                max_output_tokens=_output_limit(
                    raw.get("max_output_tokens"), raw.get("output_token_limit")
                ),
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
                max_output_tokens=_output_limit(raw.get("outputTokenLimit")),
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


class _ProviderRouteState:
    def __init__(self, state: ConfigState) -> None:
        self._state = state
        self._cache: dict[str, tuple[float, list[ProviderCatalogueModelDTO]]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def _probe(self, provider: ProviderDTO, api_key: str) -> tuple[bool, str | None]:
        if not self._state.provider_origin_approved(provider):
            return False, "provider key is not approved for this origin"
        try:
            await _fetch_provider_catalogue(provider, api_key)
            return True, None
        except (httpx.HTTPError, ValueError) as exc:
            return False, _error_text(exc)

    async def _models(self, provider: ProviderDTO) -> list[ProviderCatalogueModelDTO]:
        if not self._state.provider_origin_approved(provider):
            raise HTTPException(
                status_code=403,
                detail="Provider key is not approved for this origin. Re-save the provider.",
            )
        hit = self._fresh(provider.id)
        if hit is not None:
            return hit
        lock = self._locks.setdefault(provider.id, asyncio.Lock())
        async with lock:
            hit = self._fresh(provider.id)
            if hit is not None:
                return hit
            key = self._state._resolve_secret_value(provider.secret_name)
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
            self._cache[provider.id] = (time.monotonic(), models)
            return models

    def _fresh(self, provider_id: str) -> list[ProviderCatalogueModelDTO] | None:
        hit = self._cache.get(provider_id)
        if hit and time.monotonic() - hit[0] < _CATALOGUE_TTL_S:
            return hit[1]
        return None

    def _provider_or_404(self, provider_id: str) -> ProviderDTO:
        try:
            return self._state._provider_dto(self._state.provider_settings(provider_id))
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail=f"unknown provider {provider_id!r}",
            ) from exc

    def _catalogue_model(
        self,
        provider_id: str,
        model_id: str,
    ) -> ProviderCatalogueModelDTO | None:
        models = self._fresh(provider_id) or ()
        return next((model for model in models if model.model_id == model_id), None)

    def _invalidate(self, provider_id: str) -> None:
        self._cache.pop(provider_id, None)


def make_providers_router(state: ConfigState) -> APIRouter:
    router = APIRouter()
    route_state = _ProviderRouteState(state)

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
        ok, error = await route_state._probe(provider, body.api_key.strip())
        route_state._invalidate(provider.id)
        return ProviderMutationResult(provider=provider, catalogue_ok=ok, catalogue_error=error)

    @router.put("/api/providers/{provider_id}")
    async def put_provider(provider_id: str, body: ProviderPatch) -> ProviderMutationResult:
        try:
            provider = state.update_provider(provider_id, body)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail=f"unknown provider {provider_id!r}"
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        key = (
            body.api_key.strip()
            if body.api_key
            else state._resolve_secret_value(provider.secret_name)
        )
        if key:
            ok, error = await route_state._probe(provider, key)
        else:
            ok, error = False, f"No decryptable key stored for {provider.label}."
        route_state._invalidate(provider.id)
        return ProviderMutationResult(provider=provider, catalogue_ok=ok, catalogue_error=error)

    @router.delete("/api/providers/{provider_id}", status_code=204)
    async def delete_provider(provider_id: str) -> Response:
        try:
            state.delete_provider(provider_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail=f"unknown provider {provider_id!r}"
            ) from exc
        except ProviderInUseError as exc:
            raise HTTPException(
                status_code=409,
                detail=f"Provider is still used by catalogue models: {', '.join(exc.model_names)}",
            ) from exc
        route_state._invalidate(provider_id)
        return Response(status_code=204)

    @router.get("/api/providers/{provider_id}/models")
    async def get_provider_models(provider_id: str) -> list[ProviderCatalogueModelDTO]:
        return await route_state._models(route_state._provider_or_404(provider_id))

    @router.post("/api/providers/{provider_id}/enable")
    async def enable_provider_model(
        provider_id: str,
        body: ProviderEnableBody,
    ) -> list[ModelDTO]:
        provider = route_state._provider_or_404(provider_id)
        catalogue_model = route_state._catalogue_model(provider.id, body.model_id)
        try:
            return state.enable_provider_model(provider_id, body, catalogue_model)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.delete("/api/providers/{provider_id}/enable/{catalogue_id}")
    async def disable_provider_model(provider_id: str, catalogue_id: str) -> list[ModelDTO]:
        route_state._provider_or_404(provider_id)
        try:
            return state.disable_provider_model(provider_id, catalogue_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return router
