"""Feature-config repository split out of ConfigState (PY-0365): the
LLM-pipeline settings surfaces — each a simple GET/PUT DTO pair over one
`RouterConfig` section (encoders, TTS, image-gen, data sources, auxiliary-role
fallback) plus the data-source reachability probe. `ConfigState` exposes an
instance of this class as the plain `features` attribute.

The remaining three feature surfaces (live browser, Build kernel, project
storage) live in the sibling `config_platform_settings.py` /
`ConfigPlatformSettings` (`ConfigState.platform`): the seam evaluation's
"feature config" grouping is 17 methods, one more than this decomposition's own
public-method cap (12) — folding all 17 into one class would clear PY-0365 by
creating a fresh, equally-real `python_service_public_methods_gt_12_candidate`
row on the new class, which is debt relocated, not removed. Splitting along the
LLM-pipeline vs. Build-platform seam keeps both under the cap without
regrouping any method into a domain the seam evaluation didn't already name for
it — see the module docstring below."""

from __future__ import annotations

from disco.core.llm import ConfigStore, RouterConfig, SecretStore

from . import origin_approval_wiring as _origin_wiring
from .config.dtos import (
    DataSourcesConfigDTO,
    EncodersConfigDTO,
    ImageGenConfigDTO,
    ProbeResult,
    RoleFallbackConfigDTO,
    TtsConfigDTO,
)
from .config.mappers import (
    _data_sources_from,
    _encoders_from,
    _image_gen_from,
    _role_fallback_from,
    _tts_from,
)
from .config_state_errors import ConfigValidationError


class ConfigFeatures:
    """The LLM-pipeline feature-toggle surfaces + the data-source probe."""

    def __init__(self, store: ConfigStore, secrets: SecretStore) -> None:
        self._store = store
        self._secrets = secrets

    # encoders: bundled-local vs remote (persisted; agent-server honors per request) -

    def encoders_config(self) -> EncodersConfigDTO:
        return _encoders_from(self._store.load())

    def update_encoders_config(self, dto: EncodersConfigDTO) -> EncodersConfigDTO:
        """Persist the encoder mode. The agent-server rebuilds its research providers
        when this changes, so the toggle takes effect on the NEXT research run."""
        from disco.core.llm import EncodersSettings

        self._store.sections.save_encoders(
            EncodersSettings(
                remote=dto.remote,
                reranker_url=dto.reranker_url.strip(),
                embedder_url=dto.embedder_url.strip(),
                nli_url=dto.nli_url.strip(),
            )
        )
        _origin_wiring.approve_encoder_origins(self._store, self._secrets, dto)
        return _encoders_from(self._store.load())

    # TTS: audio-overview toggle / bundled-vs-remote / voices (persisted) --------

    def tts_config(self) -> TtsConfigDTO:
        return _tts_from(self._store.load())

    def update_tts_config(self, dto: TtsConfigDTO) -> TtsConfigDTO:
        """Persist the audio-overview TTS settings. The agent-server reloads the config
        per request, so a toggle takes effect on the NEXT overview; disabling it also
        lets the agent-server unload the Kokoro model to free RAM."""
        from disco.core.llm import TtsSettings

        self._store.sections.save_tts(
            TtsSettings(
                enabled=dto.enabled,
                provider=dto.provider,
                base_url=dto.base_url.strip(),
                api_key_env=dto.api_key_env.strip(),
                model=dto.model.strip(),
                voice_a=dto.voice_a.strip() or "af_heart",
                voice_b=dto.voice_b.strip() or "af_bella",
            )
        )
        _origin_wiring.approve_tts_origin(self._store, self._secrets, dto)
        return _tts_from(self._store.load())

    # image generation: ComfyUI / OpenAI-compatible / OpenRouter (persisted) ------

    def image_gen_config(self) -> ImageGenConfigDTO:
        return _image_gen_from(self._store.load())

    def update_image_gen_config(self, dto: ImageGenConfigDTO) -> ImageGenConfigDTO:
        """Persist the image generation provider choice. The agent-server reloads the
        config per request, so a change takes effect on the NEXT image-gen call.
        `comfyui` is self-hosted (needs base_url); `openai` is paid and requires an
        api_key_env secret stored via /api/secrets; `openrouter` (default) is paid and
        uses the stored OpenRouter key. No tier is configured until its requirement is
        met — until then image generation fails NOT CONFIGURED (W-50, no placeholder)."""
        from disco.core.llm import ImageGenSettings

        self._store.sections.save_image_gen(
            ImageGenSettings(
                provider=dto.provider,
                base_url=dto.base_url.strip(),
                api_key_env=dto.api_key_env.strip(),
                model=dto.model.strip(),
                # Only meaningful for comfyui; .strip() trims edge whitespace but
                # preserves the JSON body (internal newlines/indent untouched).
                workflow_json=dto.workflow_json.strip(),
            )
        )
        _origin_wiring.approve_image_gen_origin(self._store, self._secrets, dto)
        return _image_gen_from(self._store.load())

    # data sources: web search + extraction provider tiers (persisted) ----------

    def data_sources_config(self) -> DataSourcesConfigDTO:
        cfg = self._store.load()
        return _data_sources_from(cfg, configured_sources=self._configured_research_sources(cfg))

    def update_data_sources_config(self, dto: DataSourcesConfigDTO) -> DataSourcesConfigDTO:
        """Persist the search + extraction provider choices. The agent-server rebuilds
        its research providers when these change → effective on the NEXT research run."""
        from disco.core.llm import ExtractionSettings, SearchSettings

        search_base_url = dto.search_base_url.strip()
        if dto.search_provider == "tavily" and search_base_url.rstrip("/") not in {
            "",
            "https://api.tavily.com",
        }:
            raise ConfigValidationError(
                "invalid_origin",
                detail="Tavily requires the official API origin https://api.tavily.com.",
            )
        self._store.sections.save_search(
            SearchSettings(
                provider=dto.search_provider,
                base_url=search_base_url,
                api_key_env=dto.search_api_key_env.strip(),
            )
        )
        self._store.sections.save_extraction(
            ExtractionSettings(
                provider=dto.extraction_provider,
                base_url=dto.extraction_base_url.strip(),
                api_key_env=dto.extraction_api_key_env.strip(),
            )
        )
        _origin_wiring.approve_data_source_origins(self._store, self._secrets, dto)
        cfg = self._store.load()
        return _data_sources_from(cfg, configured_sources=self._configured_research_sources(cfg))

    def _configured_research_sources(self, cfg: RouterConfig) -> list[str]:
        from disco.core.llm.secret_refs import (
            allowed_legacy_refs,
            resolve_provider_secret,
            secret_ref_allowed_for_origin,
        )

        configured = self._active_research_source(
            cfg, resolve_provider_secret, secret_ref_allowed_for_origin
        )
        configured.update(
            self._approved_research_sources(
                resolve_provider_secret, secret_ref_allowed_for_origin, allowed_legacy_refs
            )
        )
        return sorted(configured)

    def _active_research_source(self, cfg, resolve_secret, ref_allowed) -> set[str]:
        search = cfg.search
        origins = {
            "searxng": search.base_url.strip(),
            "tavily": "https://api.tavily.com",
            "brave": search.base_url.strip() or "https://api.search.brave.com",
            "semantic_scholar": search.base_url.strip() or "https://api.semanticscholar.org",
        }
        origin = origins.get(search.provider, "")
        ref = search.api_key_env.strip()
        secret_ready = bool(resolve_secret(ref, self._secrets)) if ref else True
        if search.provider in {"tavily", "brave"} and not ref:
            secret_ready = False
        approved = (
            origin
            and secret_ready
            and self._store.approvals.origin_approved(
                origin, f"search:{search.provider}", ref, secret_store=self._secrets
            )
            and ref_allowed(ref, origin)
        )
        if (
            approved
            or search.provider in {"ddgs", "arxiv", "news"}
            or (search.provider == "site_scoped" and bool(search.base_url.strip()))
        ):
            return {search.provider}
        return set()

    def _approved_research_sources(self, resolve_secret, ref_allowed, legacy_refs) -> set[str]:
        configured: set[str] = set()
        origins = {
            "tavily": "https://api.tavily.com",
            "brave": "https://api.search.brave.com",
            "semantic_scholar": "https://api.semanticscholar.org",
        }
        for provider, origin in origins.items():
            if any(
                resolve_secret(name, self._secrets)
                and self._store.approvals.origin_approved(
                    origin, f"search:{provider}", name, secret_store=self._secrets
                )
                and ref_allowed(name, origin)
                for name in (provider, *legacy_refs(provider))
            ):
                configured.add(provider)
        return configured

    def _approve_data_source_origins(self, dto: DataSourcesConfigDTO) -> None:
        _origin_wiring.approve_data_source_origins(self._store, self._secrets, dto)

    # auxiliary-role local fallback (persisted; agent-server reads per request) ---

    def role_fallback_config(self) -> RoleFallbackConfigDTO:
        return _role_fallback_from(self._store.load())

    def update_role_fallback_config(self, dto: RoleFallbackConfigDTO) -> RoleFallbackConfigDTO:
        """Persist auxiliary-role fallback settings. An enabled fallback must carry
        both base_url and model; otherwise the router would build a provider that
        cannot make a valid completion request."""
        from disco.core.llm import RoleFallbackSettings

        base_url = dto.base_url.strip()
        model = dto.model.strip()
        api_key_env = dto.api_key_env.strip()
        if dto.enabled and (not base_url or not model):
            raise ConfigValidationError(
                "incomplete_role_fallback",
                detail="Role fallback needs both a base URL and a model when enabled.",
            )
        self._store.sections.save_role_fallback(
            RoleFallbackSettings(
                enabled=dto.enabled,
                base_url=base_url,
                model=model,
                api_key_env=api_key_env,
            )
        )
        _origin_wiring.approve_role_fallback_origin(
            self._store, self._secrets, dto.enabled, base_url, api_key_env
        )
        return _role_fallback_from(self._store.load())

    # data-source connectivity probe ------------------------------------------

    async def test_data_source(self, kind: str) -> ProbeResult:
        """Probe T4.2: reachability of the configured search/extraction endpoint.
        Bundled (in-process) tiers have no network dependency — reported honestly
        as such, never as a remote "ok". Self-host tiers (searxng/crawl4ai) probe
        the configured base URL for bare reachability. Paid tiers (tavily/brave/
        firecrawl) make a REAL functional call in that vendor's actual auth shape
        (Brave: X-Subscription-Token header; Tavily: Authorization: Bearer;
        Firecrawl: Authorization: Bearer) through the production retrieval adapters —
        so a bad key is exercised and reported as `unauthorized`, not as a
        root-URL "reachable"."""
        from .probe_clients import (
            probe_brave_search,
            probe_firecrawl_extract,
            probe_reachable,
            probe_tavily_search,
        )

        cfg = self._store.load()
        # Vendor hosts for the paid tiers (no key-free models list; reachability only).
        _PAID_HOSTS = {
            "tavily": "https://api.tavily.com",
            "brave": "https://api.search.brave.com",
            "firecrawl": "https://api.firecrawl.dev",
        }
        if kind == "search":
            s = cfg.search
            provider, base_url, key_env = s.provider, s.base_url.strip(), s.api_key_env.strip()
            bundled = {"ddgs", "arxiv", "news", "semantic_scholar", "site_scoped"}
        elif kind == "extraction":
            e = cfg.extraction
            provider, base_url, key_env = e.provider, e.base_url.strip(), e.api_key_env.strip()
            bundled = {"local"}
        else:
            return ProbeResult(
                ok=False, status="error", detail=f"unknown data-source kind {kind!r}"
            )

        if provider in bundled:
            return ProbeResult(
                ok=True,
                status="bundled",
                detail=(
                    f"Bundled/keyless tier ({provider}) — requires no configured "
                    "service URL, so there is no self-hosted endpoint to reach."
                ),
                provider=provider,
            )
        # Self-host tiers (searxng / crawl4ai) need a configured base URL.
        if provider in ("searxng", "crawl4ai"):
            if not base_url:
                return ProbeResult(
                    ok=False,
                    status="misconfigured",
                    detail=f"No base URL set for {provider} — add the service URL above.",
                    provider=provider,
                )
            if gate := _origin_wiring.probe_approval_gate(
                self._store, kind, base_url, provider=provider, secrets=self._secrets
            ):
                return gate
            ok, status, detail = await probe_reachable(base_url)
            return ProbeResult(ok=ok, status=status, detail=detail, provider=provider)
        # Paid tiers (tavily / brave / firecrawl): probe the vendor host with the key.
        host = base_url or _PAID_HOSTS.get(provider)
        if not host:
            return ProbeResult(
                ok=False,
                status="misconfigured",
                detail=f"No endpoint known for provider {provider!r}.",
                provider=provider,
            )
        if gate := _origin_wiring.probe_approval_gate(
            self._store,
            kind,
            host,
            provider=provider,
            secret_ref=key_env,
            secrets=self._secrets,
            require_secret_ref_allowed=True,
        ):
            return gate
        from disco.core.llm.secret_refs import resolve_provider_secret

        key = resolve_provider_secret(key_env, self._secrets) if key_env else None
        _PAID_PROBES = {
            "tavily": probe_tavily_search,
            "brave": probe_brave_search,
            "firecrawl": probe_firecrawl_extract,
        }
        probe = _PAID_PROBES.get(provider, probe_reachable)
        ok, status, detail = await probe(host, api_key=key)
        return ProbeResult(ok=ok, status=status, detail=detail, provider=provider)
