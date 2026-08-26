"""Pure request and response contracts for the bundled paid providers.

The retrieval adapters and app-server Settings probes must agree on the exact
vendor endpoint, authentication header, payload, and response shape.  This
module owns those facts and does not perform I/O or import either server or
retrieval code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import httpx

TAVILY_ORIGIN = "https://api.tavily.com"
BRAVE_ORIGIN = "https://api.search.brave.com"
FIRECRAWL_ORIGIN = "https://api.firecrawl.dev"


@dataclass(frozen=True)
class ProviderRequest:
    """A fully materialized provider request, ready for an HTTP executor."""

    method: Literal["GET", "POST"]
    url: str
    headers: dict[str, str]
    params: dict[str, object] | None = None
    json: dict[str, object] | None = None


def _root(base_url: str, default: str) -> str:
    return base_url.rstrip("/") or default


def brave_search_request(
    base_url: str,
    api_key: str,
    query: str,
    *,
    limit: int = 10,
    time_filter: str | None = None,
) -> ProviderRequest:
    """Build Brave's GET web-search request and its subscription-token auth."""

    params: dict[str, object] = {"q": query, "count": max(1, min(limit, 20))}
    freshness = {"week": "pw", "month": "pm"}.get(time_filter or "")
    if freshness:
        params["freshness"] = freshness
    return ProviderRequest(
        method="GET",
        url=f"{_root(base_url, BRAVE_ORIGIN)}/res/v1/web/search",
        headers={"X-Subscription-Token": api_key},
        params=params,
    )


def tavily_search_request(
    base_url: str,
    api_key: str,
    query: str,
    *,
    limit: int = 10,
    time_filter: str | None = None,
    domains_allow: frozenset[str] | None = None,
    domains_deny: frozenset[str] | None = None,
) -> ProviderRequest:
    """Build Tavily's POST search request and Bearer auth.

    Tavily is intentionally pinned to its official origin, matching the
    production adapter's fail-closed configuration rule.
    """

    root = _root(base_url, TAVILY_ORIGIN)
    if root != TAVILY_ORIGIN:
        raise ValueError("Tavily base_url must use the official API origin")
    payload: dict[str, object] = {"query": query, "max_results": limit}
    if time_filter in {"week", "month"}:
        payload["time_range"] = time_filter
    if domains_allow:
        payload["include_domains"] = sorted(domains_allow)
    if domains_deny:
        payload["exclude_domains"] = sorted(domains_deny)
    return ProviderRequest(
        method="POST",
        url=f"{root}/search",
        headers={"Authorization": f"Bearer {api_key}"},
        json=payload,
    )


def firecrawl_extract_request(
    base_url: str,
    api_key: str,
    url: str,
) -> ProviderRequest:
    """Build Firecrawl V2's markdown scrape request and Bearer auth."""

    root = _root(base_url, FIRECRAWL_ORIGIN)
    return ProviderRequest(
        method="POST",
        url=f"{root}/v2/scrape",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"url": url, "formats": ["markdown"]},
    )


def _json_result_rows(response: httpx.Response, *, nested: bool) -> list[dict]:
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("response must be an object")
    rows: object = data.get("results")
    if nested:
        web = data.get("web")
        rows = web.get("results") if isinstance(web, dict) else None
    if not isinstance(rows, list) or any(
        not isinstance(row, dict) or not isinstance(row.get("url"), str) for row in rows
    ):
        raise ValueError("result rows have an invalid shape")
    return rows


def parse_tavily_search_response(response: httpx.Response) -> list[dict]:
    """Parse Tavily's result rows, raising ValueError for contract violations."""

    return _json_result_rows(response, nested=False)


def parse_brave_search_response(response: httpx.Response) -> list[dict]:
    """Parse Brave's nested ``web.results`` rows."""

    return _json_result_rows(response, nested=True)


def parse_firecrawl_response(response: httpx.Response) -> tuple[dict, str]:
    """Parse successful Firecrawl V2 metadata and markdown content."""

    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("response must be an object")
    data = payload.get("data")
    if not isinstance(data, dict) or payload.get("success") is False:
        raise ValueError("missing successful data")
    meta = data.get("metadata") or {}
    content = data.get("markdown") or ""
    if not isinstance(meta, dict) or not isinstance(content, str):
        raise ValueError("invalid markdown response")
    return meta, content


# Short aliases keep call sites readable and offer one obvious vocabulary for
# callers that want to import only the contract helpers.
build_brave_search_request = brave_search_request
build_tavily_search_request = tavily_search_request
build_firecrawl_extract_request = firecrawl_extract_request


__all__ = [
    "BRAVE_ORIGIN",
    "FIRECRAWL_ORIGIN",
    "ProviderRequest",
    "TAVILY_ORIGIN",
    "brave_search_request",
    "build_brave_search_request",
    "build_firecrawl_extract_request",
    "build_tavily_search_request",
    "firecrawl_extract_request",
    "parse_brave_search_response",
    "parse_firecrawl_response",
    "parse_tavily_search_response",
    "tavily_search_request",
]
