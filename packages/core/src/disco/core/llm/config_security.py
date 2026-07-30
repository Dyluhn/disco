"""Secret-reference migration and origin diagnostics for router settings."""

from __future__ import annotations

from collections.abc import Callable

from ..host_egress import origin_for_url
from .config import (
    ExtractionSettings,
    ImageGenSettings,
    ModelEntry,
    RouterConfig,
    SearchSettings,
    TtsSettings,
)
from .secret_refs import migrate_legacy_secret_ref
from .secrets import SecretStore

OriginApproved = Callable[[str, str, str | None], bool]


def _model_secret_slot(key: str, entry: ModelEntry) -> str:
    if entry.provider == "openrouter" or key.startswith("or-"):
        return "openrouter"
    if entry.provider == "gemma" or "gemma" in entry.model_id.lower():
        return "gemma"
    return entry.provider or key


def _model_purpose(entry: ModelEntry) -> str:
    return f"model:{entry.provider or 'unknown'}"


def _search_url(search: SearchSettings) -> str:
    if search.provider == "tavily":
        return "https://api.tavily.com"
    if search.provider == "brave":
        return search.base_url.strip() or "https://api.search.brave.com"
    if search.provider == "semantic_scholar":
        return search.base_url.strip() or "https://api.semanticscholar.org"
    if search.provider == "searxng":
        return search.base_url.strip()
    return ""


def _extraction_url(extraction: ExtractionSettings) -> str:
    if extraction.provider == "crawl4ai":
        return extraction.base_url.strip()
    if extraction.provider == "firecrawl":
        return extraction.base_url.strip() or "https://api.firecrawl.dev"
    return ""


def _tts_url(tts: TtsSettings) -> str:
    if tts.provider == "speaches":
        return tts.base_url.strip()
    if tts.provider == "openai":
        return tts.base_url.strip() or "https://api.openai.com"
    return ""


def _image_url(image_gen: ImageGenSettings) -> str:
    if image_gen.provider == "openrouter":
        return "https://openrouter.ai/api/v1"
    if image_gen.provider == "openai":
        return image_gen.base_url.strip() or "https://api.openai.com"
    if image_gen.provider == "comfyui":
        return image_gen.base_url.strip()
    return ""


def _migrate_settings_secret_refs(
    config: RouterConfig,
    store: SecretStore,
    diagnostics: list[str],
    origin_approved: OriginApproved,
) -> dict[str, object]:
    updates: dict[str, object] = {}
    pairs = (
        ("search", config.search, config.search.provider, _search_url(config.search)),
        (
            "extraction",
            config.extraction,
            config.extraction.provider,
            _extraction_url(config.extraction),
        ),
        (
            "tts",
            config.tts,
            "openai" if config.tts.provider == "openai" else config.tts.provider,
            _tts_url(config.tts),
        ),
        (
            "image_gen",
            config.image_gen,
            "openai" if config.image_gen.provider == "openai" else config.image_gen.provider,
            _image_url(config.image_gen),
        ),
        ("role_fallback", config.role_fallback, "openai", config.role_fallback.base_url.strip()),
    )
    for field, settings, slot, url in pairs:
        ref = getattr(settings, "api_key_env", "")
        provider = getattr(settings, "provider", field)
        purpose = "role_fallback" if field == "role_fallback" else f"{field}:{provider}"
        new_ref = migrate_legacy_secret_ref(
            ref,
            slot=slot,
            url=url,
            purpose=purpose,
            store=store,
            diagnostics=diagnostics,
            origin_approved=origin_approved,
        )
        if new_ref != (ref or ""):
            updates[field] = settings.model_copy(update={"api_key_env": new_ref})
    return updates


def _model_approvals(config: RouterConfig) -> list[tuple[str, str, str, str]]:
    return [
        (f"model:{key}", entry.base_url, _model_purpose(entry), entry.api_key_env or "")
        for key, entry in config.models.items()
        if entry.base_url
    ]


def _settings_approvals(config: RouterConfig) -> list[tuple[str, str, str, str]]:
    urls: list[tuple[str, str, str, str]] = []
    if config.role_fallback.enabled and config.role_fallback.base_url.strip():
        urls.append(
            (
                "role_fallback",
                config.role_fallback.base_url.strip(),
                "role_fallback",
                config.role_fallback.api_key_env.strip(),
            )
        )
    if config.encoders.remote:
        urls.extend(
            (name, url.strip(), f"encoder:{name}", "")
            for name, url in (
                ("reranker", config.encoders.reranker_url),
                ("embedder", config.encoders.embedder_url),
                ("nli", config.encoders.nli_url),
            )
            if url.strip()
        )
    for label, provider, url, secret_ref in (
        (
            "search",
            config.search.provider,
            _search_url(config.search),
            config.search.api_key_env,
        ),
        (
            "extraction",
            config.extraction.provider,
            _extraction_url(config.extraction),
            config.extraction.api_key_env,
        ),
        ("tts", config.tts.provider, _tts_url(config.tts), config.tts.api_key_env),
        (
            "image",
            config.image_gen.provider,
            _image_url(config.image_gen),
            "openrouter"
            if config.image_gen.provider == "openrouter"
            else config.image_gen.api_key_env,
        ),
    ):
        if url:
            urls.append((f"{label}:{provider}", url, f"{label}:{provider}", secret_ref.strip()))
    return urls


def _mcp_approvals(config: RouterConfig) -> list[tuple[str, str, str, str]]:
    urls: list[tuple[str, str, str, str]] = []
    for name, raw in config.mcp.servers.items():
        if (
            not isinstance(raw, dict)
            or raw.get("transport") != "streamable_http"
            or not raw.get("url")
        ):
            continue
        raw_headers = raw.get("headers")
        headers = raw_headers if isinstance(raw_headers, dict) else {}
        refs = tuple(sorted(str(value).strip() for value in headers.values() if str(value).strip()))
        for ref in refs or ("",):
            urls.append((f"mcp:{name}", str(raw["url"]), f"mcp:{name}", ref))
    return urls


def _with_security_diagnostics(
    config: RouterConfig,
    origin_approved: OriginApproved,
    extra: list[str] | None = None,
) -> RouterConfig:
    diagnostics = list(extra or [])
    approvals = _model_approvals(config) + _settings_approvals(config) + _mcp_approvals(config)
    for label, url, purpose, secret_ref in approvals:
        origin = origin_for_url(url)
        if origin and not origin_approved(url, purpose, secret_ref):
            diagnostics.append(f"{label} origin {origin} awaiting operator approval")
    return config.model_copy(update={"security_diagnostics": tuple(sorted(set(diagnostics)))})


def migrate_secret_refs_and_trust(
    config: RouterConfig,
    origin_approved: OriginApproved,
) -> tuple[RouterConfig, bool]:
    store = SecretStore()
    diagnostics: list[str] = []
    models = dict(config.models)
    changed = False
    for key, entry in config.models.items():
        new_ref = migrate_legacy_secret_ref(
            entry.api_key_env,
            slot=_model_secret_slot(key, entry),
            url=entry.base_url,
            purpose=_model_purpose(entry),
            store=store,
            diagnostics=diagnostics,
            origin_approved=origin_approved,
        )
        if new_ref != (entry.api_key_env or ""):
            models[key] = entry.model_copy(update={"api_key_env": new_ref or None})
            changed = True
    updates: dict[str, object] = {"models": models}
    updates.update(_migrate_settings_secret_refs(config, store, diagnostics, origin_approved))
    migrated = _with_security_diagnostics(
        config.model_copy(update=updates),
        origin_approved,
        diagnostics,
    )
    return migrated, changed or migrated != config


def with_security_diagnostics(
    config: RouterConfig,
    origin_approved: OriginApproved,
) -> RouterConfig:
    return _with_security_diagnostics(config, origin_approved)
