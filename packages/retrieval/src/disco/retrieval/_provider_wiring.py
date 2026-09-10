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

import logging
import sqlite3
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ._retrieval_cache import RetrievalCache

_LOG = logging.getLogger(__name__)


class ProviderConfigError(RuntimeError):
    """A requested search/extraction provider cannot be used as configured.

    Raised at wiring time instead of silently downgrading to a bundled
    provider — a research run must fail loudly rather than search through a
    provider the user did not choose."""


# The origin each keyed/hosted search provider is trusted at. Bundled keyless
# adapters (the composite and its legs, arxiv, news, wikipedia, site_scoped)
# resolve to "" and so need no operator approval — the security fact is that a
# keyless leg sends a QUERY and no secret, which is exactly the posture the
# removed `ddgs` tier had, and requiring an approval click before a fresh
# install can search at all would break the keyless first run this tier exists
# to provide. The moment a secret_ref is attached to one of them (see
# `_needs_origin_approval`) it stops being that thing and is approved like any
# other credentialed origin.
_SEARCH_TRUST_ORIGINS: dict[str, str] = {
    "tavily": "https://api.tavily.com",
    "brave": "https://api.search.brave.com",
    "semantic_scholar": "https://api.semanticscholar.org",
    "exa": "https://mcp.exa.ai",
    "parallel": "https://search.parallel.ai",
}

# Providers whose origin is third-party but whose default use carries no
# credential. Approval is required only once a key is configured for them.
_KEYLESS_TRUST_PROVIDERS = frozenset({"exa", "parallel"})


def search_trust_origin(provider: str, base_url: str) -> str:
    """The origin a search provider must be approved at, or "" if none applies."""
    pinned = _SEARCH_TRUST_ORIGINS.get(provider)
    if pinned is None:
        return base_url
    return base_url or pinned


def _needs_origin_approval(provider: str, secret_ref: str) -> bool:
    """Whether this provider's origin must be operator-approved before use."""
    return provider not in _KEYLESS_TRUST_PROVIDERS or bool(secret_ref.strip())


def _resolve_search_wiring(
    search_provider: str,
    search_base_url: str,
    search_api_key: str,
    search_secret_ref: str,
    e: Mapping[str, str],
    approved: Callable[[str, str, str | None], bool],
) -> tuple[str, str, str]:
    # B2 — pluggable discovery. The bundled keyless composite is the default so a
    # fresh install works with no keys; searxng self-hosts (base_url, empty -> env
    # default); tavily/brave/semantic_scholar are resolved against an approved
    # trust origin; exa/parallel are keyless third-party origins that need one
    # only once a key is attached.
    from .live import _env_url

    search_url = search_base_url or (
        _env_url(e, "DISCO_SEARXNG_URL") if search_provider == "searxng" else ""
    )
    search_trust_url = search_trust_origin(search_provider, search_url)
    if (
        search_trust_url
        and _needs_origin_approval(search_provider, search_secret_ref)
        and not approved(search_trust_url, f"search:{search_provider}", search_secret_ref)
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


def build_retrieval_cache(data_dir: str) -> RetrievalCache | None:
    """The shared retrieval cache for this data dir, or None when there isn't one.

    A data dir is the only place the cache may live — it is what the container
    entrypoint sets (``DISCO_DATA_DIR``, legacy ``PMX_DATA_DIR``) and the same
    root the projects store uses. With none configured the providers run
    uncached rather than inventing a directory in somebody's home, and an
    unusable path degrades to uncached with a named warning rather than taking
    the run down over a cache.
    """
    from ._retrieval_cache import CACHE_FILENAME, open_cache

    if not data_dir.strip():
        return None
    path = Path(data_dir.strip()) / CACHE_FILENAME
    try:
        return open_cache(path)
    except (OSError, sqlite3.Error) as exc:
        _LOG.warning(
            "retrieval cache unavailable at %s (%s: %s); running uncached",
            path,
            type(exc).__name__,
            exc,
        )
        return None


__all__ = [
    "ProviderConfigError",
    "_resolve_extraction_wiring",
    "_resolve_search_wiring",
    "build_retrieval_cache",
    "search_trust_origin",
]
