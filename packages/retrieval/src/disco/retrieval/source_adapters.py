"""Additional discovery adapters behind the SearchProvider protocol.

arXiv ignores ``time_filter`` because its public Atom API has no simple recency
parameter matching the search provider contract. Google News maps the supported
filters onto RSS query ``when:`` operators.
"""

from __future__ import annotations

import asyncio
import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx

from .models import SearchHit
from .providers import SearchProvider

_ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _collapse_ws(value: str | None) -> str:
    return " ".join((value or "").split())


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _strip_html(value: str | None) -> str:
    parser = _HTMLTextExtractor()
    parser.feed(value or "")
    parser.close()
    text = _collapse_ws(" ".join(parser.parts))
    return re.sub(r"\s+([.,;:!?])", r"\1", text)


def _allowed(url: str, allow: frozenset[str] | None, deny: frozenset[str] | None) -> bool:
    host = _host(url)
    denied = {d.lower() for d in (deny or frozenset())}
    allowed = {d.lower() for d in allow} if allow else None
    if any(d in host for d in denied):
        return False
    return allowed is None or any(d in host for d in allowed)


def _parse_sites(sites: str) -> tuple[str, ...]:
    domains: list[str] = []
    seen: set[str] = set()
    for raw in re.split(r"[\s,]+", sites):
        token = raw.strip().lower().removeprefix("site:")
        if "://" in token:
            token = urlparse(token).hostname or ""
        else:
            token = token.split("/", 1)[0]
        token = token.strip(".")
        if token and token not in seen:
            seen.add(token)
            domains.append(token)
    return tuple(domains)


class ArxivSearchProvider:
    """arXiv Atom search adapter."""

    name = "arxiv"

    def __init__(
        self,
        *,
        base_url: str = "https://export.arxiv.org/api/query",
        timeout_s: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url
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
        del time_filter
        params = {"search_query": f"all:{query}", "start": 0, "max_results": limit}
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
                follow_redirects=True,
            ) as client:
                resp = await client.get(self._base_url, params=params)
                resp.raise_for_status()
            root = ET.fromstring(resp.content)
        except (httpx.HTTPError, httpx.InvalidURL, ET.ParseError, ValueError):
            return []

        hits: list[SearchHit] = []
        for i, entry in enumerate(root.findall("a:entry", _ATOM_NS)):
            url = _collapse_ws(entry.findtext("a:id", namespaces=_ATOM_NS))
            if not url or not _allowed(url, domains_allow, domains_deny):
                continue
            hits.append(
                SearchHit(
                    url=url,
                    title=(
                        _collapse_ws(entry.findtext("a:title", namespaces=_ATOM_NS)) or url
                    ),
                    snippet=(entry.findtext("a:summary", namespaces=_ATOM_NS) or "").strip(),
                    source_engine="arxiv",
                    rank=i,
                )
            )
            if len(hits) >= limit:
                break
        return hits


class NewsSearchProvider:
    """Google News RSS search adapter."""

    name = "news"

    def __init__(
        self,
        *,
        base_url: str = "https://news.google.com/rss/search",
        hl: str = "en-US",
        gl: str = "US",
        ceid: str = "US:en",
        timeout_s: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url
        self._hl = hl
        self._gl = gl
        self._ceid = ceid
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
        effective_query = _news_query(query, time_filter)
        params = {
            "q": effective_query,
            "hl": self._hl,
            "gl": self._gl,
            "ceid": self._ceid,
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
                follow_redirects=True,
            ) as client:
                resp = await client.get(self._base_url, params=params)
                resp.raise_for_status()
            root = ET.fromstring(resp.content)
        except (httpx.HTTPError, httpx.InvalidURL, ET.ParseError, ValueError):
            return []

        hits: list[SearchHit] = []
        try:
            for i, item in enumerate(root.findall("channel/item")):
                title = _collapse_ws(item.findtext("title"))
                url = _collapse_ws(item.findtext("link"))
                if not url or not _allowed(url, domains_allow, domains_deny):
                    continue
                hits.append(
                    SearchHit(
                        url=url,
                        title=title or url,
                        snippet=_strip_html(item.findtext("description")) or title or url,
                        source_engine="news",
                        rank=i,
                    )
                )
                if len(hits) >= limit:
                    break
        except ValueError:
            return []
        return hits


def _news_query(query: str, time_filter: str | None) -> str:
    when = {
        "week": "7d",
        "day": "1d",
        "today": "1d",
        "month": "30d",
    }.get((time_filter or "").lower())
    return f"{query} when:{when}" if when else query


class SemanticScholarSearchProvider:
    """Semantic Scholar Graph API search adapter."""

    name = "semantic_scholar"

    def __init__(
        self,
        *,
        base_url: str = "https://api.semanticscholar.org/graph/v1",
        api_key: str = "",
        timeout_s: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
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
        del time_filter
        params = {
            "query": query,
            "limit": limit,
            "fields": "title,abstract,url,year,externalIds",
        }
        headers = {"x-api-key": self._api_key} if self._api_key else None
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
                follow_redirects=True,
            ) as client:
                resp = await client.get(
                    f"{self._base_url}/paper/search", params=params, headers=headers
                )
                resp.raise_for_status()
                payload = resp.json()
                papers = payload.get("data", []) if isinstance(payload, dict) else []
        except (httpx.HTTPError, httpx.InvalidURL, ValueError):
            return []

        hits: list[SearchHit] = []
        for i, paper in enumerate(papers):
            if not isinstance(paper, dict):
                continue
            paper_id = str(paper.get("paperId") or "")
            url = str(paper.get("url") or "")
            if not url and paper_id:
                url = f"https://www.semanticscholar.org/paper/{paper_id}"
            if not url or not _allowed(url, domains_allow, domains_deny):
                continue
            title = str(paper.get("title") or url)
            hits.append(
                SearchHit(
                    url=url,
                    title=title,
                    snippet=str(paper.get("abstract") or ""),
                    source_engine="semantic_scholar",
                    rank=i,
                )
            )
            if len(hits) >= limit:
                break
        return hits


class SiteScopedSearchProvider:
    """Search wrapper that injects site: operators and allow-filters domains."""

    name = "site_scoped"

    def __init__(
        self,
        *,
        sites: str = "",
        inner: SearchProvider | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        del transport
        if inner is None:
            from .bundled_providers import DdgsSearchProvider

            inner = DdgsSearchProvider()
        self._sites = sites
        self._inner = inner

    async def search(
        self,
        query: str,
        *,
        limit: int = 10,
        domains_allow: frozenset[str] | None = None,
        domains_deny: frozenset[str] | None = None,
        time_filter: str | None = None,
    ) -> list[SearchHit]:
        domains = _parse_sites(self._sites)
        augmented = (
            f"{query} ({' OR '.join('site:' + d for d in domains)})" if domains else query
        )
        allow = (
            frozenset(domains) | (domains_allow or frozenset())
            if domains
            else domains_allow
        )
        try:
            hits = await self._inner.search(
                augmented,
                limit=limit,
                domains_allow=allow,
                domains_deny=domains_deny,
                time_filter=time_filter,
            )
        except Exception:  # noqa: BLE001 — discovery adapters must never break a run
            return []
        if not domains:
            return hits
        return [
            SearchHit(
                url=h.url,
                title=h.title,
                snippet=h.snippet,
                source_engine="site_scoped",
                rank=h.rank,
            )
            for h in hits
        ]


class MultiSearchProvider:
    name = "multi"

    def __init__(self, providers: tuple[SearchProvider, ...]) -> None:
        self._providers = providers

    async def search(
        self,
        query: str,
        *,
        limit: int = 10,
        domains_allow: frozenset[str] | None = None,
        domains_deny: frozenset[str] | None = None,
        time_filter: str | None = None,
    ) -> list[SearchHit]:
        if not self._providers or limit <= 0:
            return []
        gathered = await asyncio.gather(
            *[
                provider.search(
                    query,
                    limit=limit,
                    domains_allow=domains_allow,
                    domains_deny=domains_deny,
                    time_filter=time_filter,
                )
                for provider in self._providers
            ],
            return_exceptions=True,
        )
        rows_by_provider: list[list[SearchHit]] = []
        for result in gathered:
            if isinstance(result, BaseException) or not result:
                continue
            rows_by_provider.append(list(result))
        if not rows_by_provider:
            return []

        by_url: dict[str, tuple[SearchHit, int, list[str]]] = {}
        ordered_urls: list[str] = []
        max_len = max(len(rows) for rows in rows_by_provider)
        for idx in range(max_len):
            for rows in rows_by_provider:
                if idx >= len(rows):
                    continue
                hit = rows[idx]
                if not hit.url:
                    continue
                labels = _source_labels(hit.source_engine)
                current = by_url.get(hit.url)
                if current is None:
                    by_url[hit.url] = (hit, hit.rank, labels)
                    ordered_urls.append(hit.url)
                    continue
                best_hit, best_rank, engines = current
                for label in labels:
                    if label not in engines:
                        engines.append(label)
                if hit.rank < best_rank:
                    best_hit, best_rank = hit, hit.rank
                by_url[hit.url] = (best_hit, best_rank, engines)

        out: list[SearchHit] = []
        for url in ordered_urls:
            hit, _best_rank, engines = by_url[url]
            out.append(
                hit.model_copy(
                    update={
                        "source_engine": "+".join(engines),
                        "rank": len(out),
                    }
                )
            )
            if len(out) >= limit:
                break
        return out


def _source_labels(source_engine: str) -> list[str]:
    seen: set[str] = set()
    labels: list[str] = []
    for raw in (source_engine or "").split("+"):
        label = raw.strip()
        if label and label not in seen:
            seen.add(label)
            labels.append(label)
    return labels or ["unknown"]
