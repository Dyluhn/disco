"""Deep Research provider, secret, and origin resolution.

Extracted from ``DeepResearchService`` so the service constructor takes
explicit named owners instead of reaching through a whole runtime. This
module owns:

- live retrieval provider construction (``build_live_retrieval``) keyed by
  the persisted encoder/search/extraction config;
- paid-provider secret resolution via the configured env-var name (encrypted
  store wins, else the live env);
- origin-approval gating (a paid key is only resolved when the origin is
  approved AND the secret ref is allowed for that origin);
- multi-search override construction for per-run source selection;
- MCP retrieval composition (bundled + MCP providers join the same pipeline);
- the live embedder accessor used by ``SpaceService`` for corpus ingestion.

The provider holds NO runtime handle. It takes the three narrow collaborators
it needs — ``ConfigStore``, ``SecretStore``, and the MCP retrieval composer —
plus an optional statically-injected provider set (tests).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from disco.core.llm import ConfigStore, SecretStore

if TYPE_CHECKING:
    from disco.retrieval.ranking import Embedder


@runtime_checkable
class McpRetrievalComposer(Protocol):
    """Compose bundled search/extraction with MCP retrieval providers."""

    def _compose_mcp_retrieval(self, deps: dict[str, Any]) -> tuple[Any, Any]: ...


@runtime_checkable
class SecretResolver(Protocol):
    """Resolve a provider secret by env-var name (encrypted store, else env)."""

    def __call__(self, name: str | None) -> str | None: ...


@runtime_checkable
class OriginApprover(Protocol):
    """Check whether an origin+purpose+secret-ref triple is approved."""

    def __call__(
        self,
        url: str,
        purpose: str,
        secret_ref: str | None = "",
    ) -> bool: ...


def _make_secret_resolver(secret_store: SecretStore) -> SecretResolver:
    from disco.core.llm.secret_refs import resolve_provider_secret

    def resolve(name: str | None) -> str | None:
        return resolve_provider_secret(name, secret_store)

    return resolve


def _make_origin_approver(
    config_store: ConfigStore,
    secret_store: SecretStore,
) -> OriginApprover:
    def approve(
        url: str,
        purpose: str,
        secret_ref: str | None = "",
    ) -> bool:
        return config_store.origin_approved(
            url,
            purpose,
            secret_ref,
            secret_store=secret_store,
        )

    return approve


class DeepResearchProvider:
    """Own Deep Research provider/secret/origin resolution and embedder access.

    Holds NO runtime handle. The constructor takes the narrow collaborators
    needed to resolve providers: the config store (for encoder/search/extraction
    config), the secret store (for paid-provider keys), and the MCP retrieval
    composer (so MCP-discovered hits join the same citation path). An optional
    statically-injected provider set (tests) bypasses live construction.
    """

    def __init__(
        self,
        config_store: ConfigStore,
        secret_store: SecretStore,
        mcp: McpRetrievalComposer,
        *,
        injected_providers: dict[str, Any] | None = None,
    ) -> None:
        self._config_store = config_store
        self._secret_store = secret_store
        self._mcp = mcp
        self._injected_providers = injected_providers
        self._resolve_secret: SecretResolver = _make_secret_resolver(secret_store)
        self._origin_approved: OriginApprover = _make_origin_approver(
            config_store,
            secret_store,
        )
        self._research_providers: dict[str, Any] | None = None
        self._research_encoders_key: tuple[object, ...] | None = None

    # ---- public surface ---------------------------------------------------

    def embedder(self) -> Embedder:
        """Return the live research embedder for Space corpus ingestion."""
        from typing import cast

        return cast("Embedder", self.research()["embedder"])

    def compose_mcp_retrieval(
        self,
        deps: dict[str, Any],
    ) -> tuple[Any, Any]:
        """Compose bundled search/extraction with MCP retrieval providers."""
        return self._mcp._compose_mcp_retrieval(deps)

    def research(
        self,
        search_override: Any | None = None,
    ) -> dict[str, Any]:
        """Build (or return cached) live retrieval providers.

        A statically-injected provider set (tests) is used as-is. Otherwise
        build from the PERSISTED encoder mode, and rebuild if the Settings
        toggle changed it — so flipping local↔remote takes effect on the next
        research run without a restart (rebuild is cheap: fastembed models are
        module-cached, not per provider instance).
        """
        if self._injected_providers is not None:
            if search_override is not None:
                return {
                    **self._injected_providers,
                    "search": search_override,
                }
            return self._injected_providers
        cfg = self._config_store.load()
        enc, sch, ext = cfg.encoders, cfg.search, cfg.extraction
        from disco.core.llm.secret_refs import secret_ref_allowed_for_origin

        search_secret_url = {
            "tavily": "https://api.tavily.com",
            "brave": sch.base_url or "https://api.search.brave.com",
            "semantic_scholar": sch.base_url or "https://api.semanticscholar.org",
        }.get(sch.provider, sch.base_url)
        extraction_secret_url = (
            ext.base_url or "https://api.firecrawl.dev"
            if ext.provider == "firecrawl"
            else ext.base_url
        )
        search_purpose = f"search:{sch.provider}"
        extraction_purpose = f"extraction:{ext.provider}"
        # paid-provider keys resolve by the configured env-var NAME (same
        # mechanism as model api_key_env): the encrypted store wins, else the
        # live env. Bundled providers (ddgs/local) need no key.
        search_key = (
            self._resolve_secret(sch.api_key_env) or ""
            if self._origin_approved(search_secret_url, search_purpose, sch.api_key_env)
            and secret_ref_allowed_for_origin(sch.api_key_env, search_secret_url)
            else ""
        )
        ext_key = (
            self._resolve_secret(ext.api_key_env) or ""
            if self._origin_approved(extraction_secret_url, extraction_purpose, ext.api_key_env)
            and secret_ref_allowed_for_origin(ext.api_key_env, extraction_secret_url)
            else ""
        )
        approval_key = self._config_store.approval_store(
            secret_store=self._secret_store
        ).verified()
        key = (
            enc.remote,
            enc.reranker_url,
            enc.embedder_url,
            enc.nli_url,
            sch.provider,
            sch.base_url,
            sch.api_key_env,
            ext.provider,
            ext.base_url,
            ext.api_key_env,
            approval_key,
        )
        if search_override is not None:
            from disco.retrieval.live import build_live_retrieval

            return build_live_retrieval(
                remote=enc.remote,
                reranker_url=enc.reranker_url,
                embedder_url=enc.embedder_url,
                nli_url=enc.nli_url,
                search_provider=sch.provider,
                search_base_url=sch.base_url,
                search_api_key=search_key,
                search_secret_ref=sch.api_key_env,
                search_override=search_override,
                extraction_provider=ext.provider,
                extraction_base_url=ext.base_url,
                extraction_api_key=ext_key,
                extraction_secret_ref=ext.api_key_env,
                origin_approved=self._origin_approved,
            )
        if self._research_providers is None or self._research_encoders_key != key:
            from disco.retrieval.live import build_live_retrieval

            self._research_providers = build_live_retrieval(
                remote=enc.remote,
                reranker_url=enc.reranker_url,
                embedder_url=enc.embedder_url,
                nli_url=enc.nli_url,
                search_provider=sch.provider,
                search_base_url=sch.base_url,
                search_api_key=search_key,
                search_secret_ref=sch.api_key_env,
                extraction_provider=ext.provider,
                extraction_base_url=ext.base_url,
                extraction_api_key=ext_key,
                extraction_secret_ref=ext.api_key_env,
                origin_approved=self._origin_approved,
            )
            self._research_encoders_key = key
        return self._research_providers

    def search_override_for_sources(
        self,
        sources: Sequence[str] | None,
    ) -> Any | None:
        """Build a multi-search override for per-run source selection."""
        clean = tuple(
            str(source).strip() for source in (sources or ()) if str(source).strip()
        )
        if not clean:
            return None
        cfg = self._config_store.load()
        sch = cfg.search
        from disco.core.llm.secret_refs import secret_ref_allowed_for_origin

        provider_urls = {
            "tavily": "https://api.tavily.com",
            "semantic_scholar": sch.base_url or "https://api.semanticscholar.org",
            "brave": sch.base_url or "https://api.search.brave.com",
        }

        def key_for(provider: str, *fallback_names: str) -> str:
            target_url = provider_urls.get(provider, "")
            purpose = f"search:{provider}"
            if not target_url:
                return ""
            if sch.provider == provider and sch.api_key_env:
                key = (
                    self._resolve_secret(sch.api_key_env)
                    if self._origin_approved(target_url, purpose, sch.api_key_env)
                    and secret_ref_allowed_for_origin(sch.api_key_env, target_url)
                    else None
                )
                if key:
                    return key
            for name in fallback_names:
                key = (
                    self._resolve_secret(name)
                    if self._origin_approved(target_url, purpose, name)
                    and secret_ref_allowed_for_origin(name, target_url)
                    else None
                )
                if key:
                    return key
            return ""

        from disco.retrieval.live import build_multi_search

        return build_multi_search(
            clean,
            searxng_url=(
                sch.base_url
                if sch.provider == "searxng"
                and self._origin_approved(sch.base_url, "search:searxng", "")
                else ""
            ),
            tavily_key=key_for("tavily", "TAVILY_API_KEY", "DISCO_TAVILY_API_KEY"),
            ss_key=key_for(
                "semantic_scholar",
                "SEMANTIC_SCHOLAR_API_KEY",
                "DISCO_SEMANTIC_SCHOLAR_API_KEY",
                "S2_API_KEY",
            ),
            brave_key=key_for(
                "brave",
                "BRAVE_SEARCH_API_KEY",
                "DISCO_BRAVE_SEARCH_API_KEY",
                "BRAVE_API_KEY",
            ),
            brave_url=(
                sch.base_url
                if sch.provider == "brave"
                and self._origin_approved(sch.base_url, "search:brave", sch.api_key_env)
                else ""
            ),
            site_scoped_sites=sch.base_url if sch.provider == "site_scoped" else "",
        )

    def in_process_encoders(self) -> bool:
        """True when the persisted encoder mode is in-process (not remote)."""
        return not self._config_store.load().encoders.remote

    # ---- test-injected provider set --------------------------------------

    @property
    def injected_providers(self) -> dict[str, Any] | None:
        return self._injected_providers


def make_secret_resolver(secret_store: SecretStore) -> Callable[[str | None], str | None]:
    """Public factory for the secret resolver (used by wiring/tests)."""
    return _make_secret_resolver(secret_store)


def make_origin_approver(
    config_store: ConfigStore,
    secret_store: SecretStore,
) -> Callable[[str, str, str | None], bool]:
    """Public factory for the origin approver (used by wiring/tests)."""
    return _make_origin_approver(config_store, secret_store)