"""Wikipedia discovery — the keyless reference leg of the bundled search tier.

Its own module because ``source_adapters`` is at its size cap, and because this
is the one bundled leg that talks to a documented public API rather than to a
search product: MediaWiki keeps answering when the open metasearch engines are
cooling, which is exactly why the composite carries it.
"""

from __future__ import annotations

import json
from urllib.parse import quote

import httpx
from disco.core.host_egress import EgressDenied

from .models import SearchHit
from .source_adapters import _fetch_get, _strip_html, _with_params
from .url_policy import url_allowed

_WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
# Wikimedia's API etiquette asks for a descriptive agent naming the software and
# a way to reach its authors. A browser-shaped string would be a lie about what
# is calling; this one is true and gets us served.
_WIKIPEDIA_UA = "disco/1.0 (self-hosted research agent; +https://agenticdisco.app)"


class WikipediaSearchProvider:
    """Keyless MediaWiki full-text search — the bundled reference/encyclopedia leg.

    Wikipedia keeps answering when the open metasearch engines are cooling
    (its API is a documented public interface, not a scrape target), which is
    exactly why it belongs in the bundled composite: the tier degrades one leg
    at a time instead of all at once.

    ``search_detailed`` names its refusals like every other bundled adapter, so a
    429 from Wikimedia cools ``wikipedia`` rather than arriving as no results.
    The API's ``timestamp`` is the page's LAST EDIT, not a publication date, so
    ``published_at`` is deliberately left unset — feeding an edit time into a
    recency filter would make every article look days old.
    """

    name = "wikipedia"

    def __init__(
        self,
        *,
        base_url: str = _WIKIPEDIA_API,
        timeout_s: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url or _WIKIPEDIA_API
        self._timeout = timeout_s
        self._transport = transport

    async def search(
        self,
        query: str,
        *,
        limit: int = 10,
        domains_allow: frozenset[str] | None = None,
        domains_deny: frozenset[str] | None = None,
        time_filter: str | None = None,
    ) -> list[SearchHit]:
        hits, _diagnostic = await self.search_detailed(
            query,
            limit=limit,
            domains_allow=domains_allow,
            domains_deny=domains_deny,
            time_filter=time_filter,
        )
        return hits

    async def search_detailed(
        self,
        query: str,
        *,
        limit: int = 10,
        domains_allow: frozenset[str] | None = None,
        domains_deny: frozenset[str] | None = None,
        time_filter: str | None = None,
    ) -> tuple[list[SearchHit], dict[str, object]]:
        del time_filter  # an encyclopedia has no recency axis worth filtering on
        params = {
            "action": "query",
            "list": "search",
            "srsearch": query,
            "srlimit": max(1, min(limit, 50)),
            "srprop": "snippet",
            "format": "json",
        }
        try:
            resp = await _fetch_get(
                _with_params(self._base_url, params),
                timeout_s=self._timeout,
                headers={"User-Agent": _WIKIPEDIA_UA},
                transport=self._transport,
            )
            status = resp.status_code
            if status >= 400:
                return [], named_failure_diagnostic(self.name, f"http {status}", status)
            payload = json.loads(resp.text)
        except (EgressDenied, OSError, ValueError, httpx.HTTPError) as exc:
            return [], named_failure_diagnostic(self.name, type(exc).__name__, None)
        if not isinstance(payload, dict):
            return [], named_failure_diagnostic(self.name, "unreadable response body", status)
        error = payload.get("error")
        if isinstance(error, dict):
            code = str(error.get("code") or "api error")
            return [], named_failure_diagnostic(self.name, code, status)
        block = payload.get("query")
        rows = block.get("search") if isinstance(block, dict) else None
        rows = rows if isinstance(rows, list) else []
        hits = self._to_hits(rows, limit, domains_allow, domains_deny)
        return hits, {"status_code": status, "result_count": len(rows)}

    def _to_hits(
        self,
        rows: list,
        limit: int,
        domains_allow: frozenset[str] | None,
        domains_deny: frozenset[str] | None,
    ) -> list[SearchHit]:
        hits: list[SearchHit] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            title = str(row.get("title") or "").strip()
            if not title:
                continue
            # Wikipedia's own canonical form leaves brackets and commas literal.
            slug = quote(title.replace(" ", "_"), safe="/()',!*")
            url = f"https://en.wikipedia.org/wiki/{slug}"
            if not url_allowed(url, domains_allow, domains_deny):
                continue
            hits.append(
                SearchHit(
                    url=url,
                    title=title,
                    snippet=_strip_html(str(row.get("snippet") or "")),
                    source_engine=self.name,
                    rank=len(hits),
                )
            )
            if len(hits) >= limit:
                break
        return hits


def named_failure_diagnostic(provider: str, detail: str, status: int | None) -> dict[str, object]:
    """A bundled adapter's refusal, named in the shared classification vocabulary.

    The marker leads with the provider name so ``marker_engine`` cools exactly
    this leg of the composite; a load refusal goes in ``unresponsive_engines``
    (the key the cooldown is read from), anything else in ``provider_error``.
    """
    from ._mcp_search_providers import classify_failure_text, is_engine_cooling_marker

    marker = classify_failure_text(provider, detail, status)
    diagnostic: dict[str, object] = {"status_code": status, "result_count": 0}
    if is_engine_cooling_marker(marker):
        diagnostic["unresponsive_engines"] = [marker]
    else:
        diagnostic["provider_error"] = marker
    return diagnostic


__all__ = ["WikipediaSearchProvider", "named_failure_diagnostic"]
