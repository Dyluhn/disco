"""Origin approval wiring for app-server settings flows."""

from __future__ import annotations

from typing import Any

from disco.core.llm import ConfigStore, SecretStore

from .config.dtos import (
    DataSourcesConfigDTO,
    EncodersConfigDTO,
    ImageGenConfigDTO,
    ProbeResult,
    TtsConfigDTO,
)


def approve_origin(
    store: ConfigStore,
    secrets: SecretStore,
    url: str,
    purpose: str,
    secret_ref: str | None = "",
) -> None:
    origin = url.strip()
    if origin:
        store.approve_origin(
            origin,
            purpose.strip(),
            secret_ref or "",
            secret_store=secrets,
        )


sign_origin = approve_origin


def approve_model_origin(store: ConfigStore, secrets: SecretStore, entry: Any) -> None:
    if entry.base_url:
        approve_origin(store, secrets, entry.base_url, f"model:{entry.provider}", entry.api_key_env or "")


def approve_encoder_origins(
    store: ConfigStore, secrets: SecretStore, dto: EncodersConfigDTO
) -> None:
    if dto.remote:
        approve_origin(store, secrets, dto.reranker_url, "encoder:reranker")
        approve_origin(store, secrets, dto.embedder_url, "encoder:embedder")
        approve_origin(store, secrets, dto.nli_url, "encoder:nli")


def approve_tts_origin(store: ConfigStore, secrets: SecretStore, dto: TtsConfigDTO) -> None:
    if dto.provider == "speaches":
        approve_origin(store, secrets, dto.base_url, "tts:speaches")
    elif dto.provider == "openai":
        approve_origin(
            store,
            secrets,
            dto.base_url.strip() or "https://api.openai.com",
            "tts:openai",
            dto.api_key_env.strip(),
        )


def approve_image_gen_origin(
    store: ConfigStore, secrets: SecretStore, dto: ImageGenConfigDTO
) -> None:
    if dto.provider == "comfyui":
        approve_origin(store, secrets, dto.base_url, "image:comfyui")
    elif dto.provider == "openai":
        approve_origin(
            store,
            secrets,
            dto.base_url.strip() or "https://api.openai.com",
            "image:openai",
            dto.api_key_env.strip(),
        )
    elif dto.provider == "openrouter":
        approve_origin(
            store, secrets, "https://openrouter.ai/api/v1", "image:openrouter", "openrouter"
        )


def approve_data_source_origins(
    store: ConfigStore, secrets: SecretStore, dto: DataSourcesConfigDTO
) -> None:
    search_url = ""
    if dto.search_provider == "searxng":
        search_url = dto.search_base_url.strip()
    elif dto.search_provider == "tavily":
        search_url = "https://api.tavily.com"
    elif dto.search_provider == "brave":
        search_url = dto.search_base_url.strip() or "https://api.search.brave.com"
    elif dto.search_provider == "semantic_scholar":
        search_url = dto.search_base_url.strip() or "https://api.semanticscholar.org"
    if search_url:
        approve_origin(
            store,
            secrets,
            search_url,
            f"search:{dto.search_provider}",
            dto.search_api_key_env.strip(),
        )
    extraction_url = ""
    if dto.extraction_provider == "crawl4ai":
        extraction_url = dto.extraction_base_url.strip()
    elif dto.extraction_provider == "firecrawl":
        extraction_url = dto.extraction_base_url.strip() or "https://api.firecrawl.dev"
    if extraction_url:
        approve_origin(
            store,
            secrets,
            extraction_url,
            f"extraction:{dto.extraction_provider}",
            dto.extraction_api_key_env.strip(),
        )


def approve_role_fallback_origin(
    store: ConfigStore,
    secrets: SecretStore,
    enabled: bool,
    base_url: str,
    api_key_env: str,
) -> None:
    if enabled:
        approve_origin(store, secrets, base_url, "role_fallback", api_key_env)


def approve_mcp_server_origin(
    store: ConfigStore, secrets: SecretStore, name: str, srv: dict
) -> None:
    if srv.get("transport") == "streamable_http":
        for ref in mcp_secret_refs(srv):
            approve_origin(store, secrets, str(srv.get("url") or ""), f"mcp:{name}", ref)


def mcp_secret_refs(srv: dict) -> tuple[str, ...]:
    raw_headers = srv.get("headers")
    headers = raw_headers if isinstance(raw_headers, dict) else {}
    refs = tuple(sorted(str(v).strip() for v in headers.values() if str(v).strip()))
    return refs or ("",)


def probe_approval_gate(
    store: ConfigStore,
    kind: str,
    url: str,
    *,
    provider: str = "",
    secret_ref: str | None = "",
    secrets: SecretStore | None = None,
    require_secret_ref_allowed: bool = False,
) -> ProbeResult | None:
    purpose = f"model:{provider}" if kind == "model" else f"{kind}:{provider}"
    if not store.origin_approved(url, purpose, secret_ref or "", secret_store=secrets):
        return ProbeResult(
            ok=False,
            status="misconfigured",
            detail=_unapproved_probe_detail(kind, provider),
            provider=None if kind == "model" else provider,
        )
    if require_secret_ref_allowed and secret_ref:
        from disco.core.llm.secret_refs import secret_ref_allowed_for_origin

        if not secret_ref_allowed_for_origin(secret_ref, url):
            return ProbeResult(
                ok=False,
                status="misconfigured",
                detail=_secret_ref_probe_detail(kind),
                provider=None if kind == "model" else provider,
            )
    return None


def _unapproved_probe_detail(kind: str, provider: str) -> str:
    if kind == "model":
        return "This model origin is not operator-approved. Save the model first."
    return f"{provider} origin is not operator-approved. Save Data sources first."


def _secret_ref_probe_detail(kind: str) -> str:
    if kind == "model":
        return "This secret_ref is not allowed for the model origin."
    return "This secret_ref is not allowed for the data-source origin."
