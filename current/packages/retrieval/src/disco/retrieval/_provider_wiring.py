"""Provider wiring policy: requested providers resolve loudly or not at all.

Extracted from :mod:`.live` (module size cap). A requested search/extraction
provider that is unapproved or unconstructible raises
:class:`ProviderConfigError` at wiring time — never a silent substitution of
a bundled provider the user did not choose.

Imports :mod:`.live` lazily inside function bodies (for ``_env_url``) — the
parent imports this module at its own top level, so by call time those
attributes exist; a top-level import here would be circular.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping


class ProviderConfigError(RuntimeError):
    """A requested search/extraction provider cannot be used as configured.

    Raised at wiring time instead of silently downgrading to a bundled
    provider — a research run must fail loudly rather than search through a
    provider the user did not choose."""


def _resolve_search_wiring(
    search_provider: str,
    search_base_url: str,
    search_api_key: str,
    search_secret_ref: str,
    e: Mapping[str, str],
    approved: Callable[[str, str, str | None], bool],
) -> tuple[str, str, str]:
    # B2 — pluggable discovery. Bundled (ddgs) by default so a fresh install works
    # keyless; searxng self-hosts (base_url, empty -> env default); tavily/brave/
    # semantic_scholar are resolved against an approved trust origin.
    from .live import _env_url

    search_url = search_base_url or (
        _env_url(e, "DISCO_SEARXNG_URL") if search_provider == "searxng" else ""
    )
    if search_provider == "tavily" and search_url.rstrip("/") not in {
        "",
        "https://api.tavily.com",
    }:
        raise ProviderConfigError(
            "tavily accepts only the official API origin; refusing a custom keyed endpoint"
        )
    search_trust_url = {
        "tavily": "https://api.tavily.com",
        "brave": search_url or "https://api.search.brave.com",
        "semantic_scholar": search_url or "https://api.semanticscholar.org",
    }.get(search_provider, search_url)
    if search_trust_url and not approved(
        search_trust_url, f"search:{search_provider}", search_secret_ref
    ):
        raise ProviderConfigError(
            f"search provider {search_provider!r} origin {search_trust_url!r} is not an "
            "approved trust origin; approve the origin or choose another provider"
        )
    return search_provider, search_url, search_api_key


def _resolve_extraction_wiring(
    extraction_provider: str,
    extraction_base_url: str,
    extraction_api_key: str,
    extraction_secret_ref: str,
    e: Mapping[str, str],
    approved: Callable[[str, str, str | None], bool],
) -> tuple[str, str, str]:
    # B1 — pluggable extraction. Bundled (local) by default; crawl4ai self-hosts
    # (base_url, empty -> env default); firecrawl is paid (resolved api_key).
    from .live import _env_url

    if extraction_provider == "crawl4ai":
        extraction_url = extraction_base_url or _env_url(e, "DISCO_CRAWL4AI_URL")
    elif extraction_provider == "firecrawl":
        extraction_url = extraction_base_url or "https://api.firecrawl.dev"
    else:
        extraction_url = extraction_base_url
    if extraction_provider != "local" and not approved(
        extraction_url,
        f"extraction:{extraction_provider}",
        extraction_secret_ref,
    ):
        raise ProviderConfigError(
            f"extraction provider {extraction_provider!r} origin {extraction_url!r} is not "
            "an approved trust origin; approve the origin or choose another provider"
        )
    return extraction_provider, extraction_url, extraction_api_key


__all__ = [
    "ProviderConfigError",
    "_resolve_extraction_wiring",
    "_resolve_search_wiring",
]
