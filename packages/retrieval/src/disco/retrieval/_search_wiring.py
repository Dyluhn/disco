"""Which discovery providers a configured id builds, and how they compose.

Extracted from :mod:`.live` (module size cap). Two related jobs live here:

* ``make_search`` — one provider id → one constructed provider.
* ``build_multi_search`` / ``build_keyless_search`` — a list of source ids, or
  the bundled default, → one ``MultiSearchProvider`` over them.

The bundled first-run tier is the composite: Parallel + Exa (keyless hosted MCP
search) alongside Wikipedia, arXiv and Semantic Scholar (open APIs). It replaced
a single scraped-metasearch provider whose rate limit was indistinguishable from
an empty web, and it is a composite for exactly that reason — five legs that fail
independently and name themselves when they do, rather than one leg that fails
silently and takes the whole run with it.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, TypedDict

from ._provider_wiring import ProviderConfigError

if TYPE_CHECKING:
    from .providers import SearchProvider

_LOG = logging.getLogger(__name__)

# The composite that is the first-run default, in merge order: the two broad
# keyless web legs first, then the reference/academic legs whose coverage is
# narrower but whose availability is far steadier under load.
BUNDLED_SEARCH_SOURCES: tuple[str, ...] = (
    "parallel",
    "exa",
    "wikipedia",
    "arxiv",
    "semantic_scholar",
)

# The two broad web legs on their own — what the per-run "Web" source toggle and
# the site: wrapper mean when they ask for general web search.
WEB_SEARCH_SOURCES: tuple[str, ...] = ("parallel", "exa")

# The provider id a config may still name after the 2026-09-02 removal of the
# scraped-metasearch tier, mapped to what now does that job. Migration, not an
# affordance: neither id appears in any settings list or prompt.
LEGACY_SOURCE_IDS: dict[str, str] = {"ddgs": "web"}


def canonical_source_id(source_id: str) -> str:
    """Fold a retired source id onto the one that replaced it."""
    return LEGACY_SOURCE_IDS.get(source_id, source_id)


class _EndpointOptions(TypedDict, total=False):
    base_url: str


def make_search(
    provider: str, base_url: str, api_key: str, *, categories: str = "", data_dir: str = ""
) -> SearchProvider:
    """Select the discovery provider (§B2).

    The BUNDLED composite is the default — keyless, no service. `searxng`
    self-hosts; `tavily`/`brave` are paid keys; `exa`/`parallel` are keyless
    hosted MCP servers that take an optional key for volume. `data_dir` is
    where the self-hosted tier keeps its 24-hour result cache; empty means the
    provider runs uncached.
    """
    from ._provider_wiring import build_retrieval_cache
    from ._wikipedia_search import WikipediaSearchProvider
    from .bundled_providers import BraveSearchProvider, TavilySearchProvider
    from .live import SEARXNG_DEFAULT_CATEGORIES, SearxngSearchProvider
    from .source_adapters import (
        ArxivSearchProvider,
        NewsSearchProvider,
        SemanticScholarSearchProvider,
        SiteScopedSearchProvider,
    )

    provider = canonical_source_id(provider)
    endpoint: _EndpointOptions = {"base_url": base_url} if base_url else {}
    if provider == "searxng":
        return SearxngSearchProvider(
            base_url,
            categories=categories.strip() or SEARXNG_DEFAULT_CATEGORIES,
            cache=build_retrieval_cache(data_dir),
        )
    if provider == "tavily":
        return TavilySearchProvider(api_key)
    if provider == "brave":
        return BraveSearchProvider(api_key, base_url=base_url or "https://api.search.brave.com")
    if provider in ("exa", "parallel"):
        return _make_mcp_search(provider, base_url, api_key)
    if provider == "wikipedia":
        return WikipediaSearchProvider(**endpoint)
    if provider == "arxiv":
        return ArxivSearchProvider(**endpoint)
    if provider == "news":
        return NewsSearchProvider(**endpoint)
    if provider == "semantic_scholar":
        return SemanticScholarSearchProvider(api_key=api_key, **endpoint)
    if provider == "site_scoped":
        return SiteScopedSearchProvider(sites=base_url)
    if provider == "web":
        return _compose(WEB_SEARCH_SOURCES, {})
    return _compose(BUNDLED_SEARCH_SOURCES, {})  # default / "bundled"


def _make_mcp_search(provider: str, base_url: str, api_key: str) -> SearchProvider:
    from ._mcp_search_providers import ExaMcpSearchProvider, ParallelMcpSearchProvider

    if provider == "exa":
        return ExaMcpSearchProvider(api_key=api_key, endpoint=base_url)
    return ParallelMcpSearchProvider(api_key=api_key, endpoint=base_url)


# B2 dispatch table: source id -> (provider name, cfg key for base_url, cfg key
# for api_key, cfg key that gates inclusion). An empty spec slot means "no such
# field" (cfg.get("", "") always resolves to ""); an empty gate key means
# "always included" (bundled sources need no key/url).
_MULTI_SEARCH_SPEC: dict[str, tuple[str, str, str, str]] = {
    "web": ("web", "", "", ""),
    "exa": ("exa", "", "exa_key", ""),
    "parallel": ("parallel", "", "parallel_key", ""),
    "wikipedia": ("wikipedia", "", "", ""),
    "arxiv": ("arxiv", "", "", ""),
    "news": ("news", "", "", ""),
    "semantic_scholar": ("semantic_scholar", "", "ss_key", ""),
    "searxng": ("searxng", "searxng_url", "", "searxng_url"),
    "tavily": ("tavily", "", "tavily_key", "tavily_key"),
    "brave": ("brave", "brave_url", "brave_key", "brave_key"),
    "site_scoped": ("site_scoped", "site_scoped_sites", "", "site_scoped_sites"),
}


def multi_search_source_ids() -> frozenset[str]:
    """Every source id the per-run source picker may legitimately name."""
    return frozenset(_MULTI_SEARCH_SPEC) | frozenset(LEGACY_SOURCE_IDS)


def _multi_search_candidate(source_id: str, cfg: Mapping[str, str]) -> SearchProvider | None:
    spec = _MULTI_SEARCH_SPEC.get(source_id)
    if spec is None:
        return None
    provider, url_key, key_key, gate_key = spec
    if gate_key and not cfg[gate_key]:
        return None
    return make_search(
        provider,
        cfg.get(url_key, ""),
        cfg.get(key_key, ""),
        categories=cfg.get("searxng_categories", ""),
    )


def _collect_multi_search_providers(
    sources: Sequence[str], cfg: Mapping[str, str]
) -> list[SearchProvider]:
    providers: list[SearchProvider] = []
    seen: set[str] = set()
    for raw in sources:
        source_id = canonical_source_id(str(raw).strip().lower())
        if not source_id or source_id in seen:
            continue
        seen.add(source_id)
        candidate = _multi_search_candidate(source_id, cfg)
        if candidate is not None:
            providers.append(candidate)
    return providers


def _compose(sources: Sequence[str], cfg: Mapping[str, str]) -> SearchProvider:
    from .source_adapters import MultiSearchProvider

    providers = _collect_multi_search_providers(sources, cfg)
    if len(providers) == 1:
        return providers[0]
    return MultiSearchProvider(tuple(providers))


def build_keyless_search(*, exa_key: str = "", parallel_key: str = "") -> SearchProvider:
    """The bundled first-run search tier: the keyless composite, keys optional."""
    return _compose(BUNDLED_SEARCH_SOURCES, {"exa_key": exa_key, "parallel_key": parallel_key})


def build_multi_search(
    sources: Sequence[str],
    *,
    searxng_url: str = "",
    tavily_key: str = "",
    ss_key: str = "",
    brave_key: str = "",
    brave_url: str = "",
    exa_key: str = "",
    parallel_key: str = "",
    site_scoped_sites: str = "",
    searxng_categories: str = "",
) -> SearchProvider:
    """Compose a MultiSearchProvider from a per-query source id list."""
    cfg = {
        "searxng_url": searxng_url,
        "searxng_categories": searxng_categories,
        "tavily_key": tavily_key,
        "ss_key": ss_key,
        "brave_key": brave_key,
        "brave_url": brave_url,
        "exa_key": exa_key,
        "parallel_key": parallel_key,
        "site_scoped_sites": site_scoped_sites,
    }
    providers = _collect_multi_search_providers(sources, cfg)
    if not providers:
        requested = [str(source).strip().lower() for source in sources if str(source).strip()]
        if requested:
            # The user explicitly chose sources; substituting a different
            # provider behind their back is a config error, not a fallback.
            raise ProviderConfigError(
                f"none of the requested search sources could be constructed: {requested}; "
                "check their configuration or choose different sources"
            )
        return build_keyless_search(exa_key=exa_key, parallel_key=parallel_key)
    if len(providers) == 1:
        return providers[0]
    from .source_adapters import MultiSearchProvider

    return MultiSearchProvider(tuple(providers))


__all__ = [
    "BUNDLED_SEARCH_SOURCES",
    "LEGACY_SOURCE_IDS",
    "WEB_SEARCH_SOURCES",
    "build_keyless_search",
    "build_multi_search",
    "canonical_source_id",
    "make_search",
    "multi_search_source_ids",
]
