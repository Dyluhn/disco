"""Additional discovery adapters behind the SearchProvider protocol."""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from html.parser import HTMLParser
from urllib.parse import urlencode, urlparse

import httpx
from disco.core.host_egress import EgressDenied, GuardedResponse, guarded_get

from .models import SearchHit

_LOG = logging.getLogger(__name__)
from .providers import SearchProvider
from .url_policy import parse_source_date, source_url_key, url_allowed

_ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}


def _with_params(base: str, params: Mapping[str, object]) -> str:
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}{urlencode(params)}"


async def _fetch_get(
    url: str,
    *,
    timeout_s: float,
    headers: Mapping[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.Response | GuardedResponse:
    if transport is not None:
        async with httpx.AsyncClient(
            transport=transport,
            timeout=timeout_s,
            follow_redirects=True,
            trust_env=False,
        ) as client:
            return await client.get(url, headers=headers)
    return await guarded_get(url, timeout_s=timeout_s, headers=headers)


def _collapse_ws(value: str | None) -> str:
    return " ".join((value or "").split())


def _recency_start(time_filter: str | None) -> datetime.date | None:
    days = {"week": 7, "month": 31}.get(time_filter or "")
    return datetime.date.today() - datetime.timedelta(days=days) if days else None


def _atom_date(value: str | None) -> datetime.date | None:
    return parse_source_date(value)


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
        recency_start = _recency_start(time_filter)
        request_limit = min(limit * 4, 100) if recency_start else limit
        params = {"search_query": f"all:{query}", "start": 0, "max_results": request_limit}
        try:
            resp = await _fetch_get(
                _with_params(self._base_url, params),
                timeout_s=self._timeout,
                transport=self._transport,
            )
            if resp.status_code >= 400:
                return []
            root = ET.fromstring(resp.content)
        except (EgressDenied, OSError, ET.ParseError, ValueError, httpx.HTTPError):
            return []

        hits: list[SearchHit] = []
        for i, entry in enumerate(root.findall("a:entry", _ATOM_NS)):
            published = _atom_date(entry.findtext("a:published", namespaces=_ATOM_NS))
            if recency_start is not None:
                if published is None or published < recency_start:
                    continue
            url = _collapse_ws(entry.findtext("a:id", namespaces=_ATOM_NS))
            if not url or not url_allowed(url, domains_allow, domains_deny):
                continue
            hits.append(
                SearchHit(
                    url=url,
                    title=(_collapse_ws(entry.findtext("a:title", namespaces=_ATOM_NS)) or url),
                    snippet=(entry.findtext("a:summary", namespaces=_ATOM_NS) or "").strip(),
                    source_engine="arxiv",
                    rank=i,
                    published_at=published,
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
            resp = await _fetch_get(
                _with_params(self._base_url, params),
                timeout_s=self._timeout,
                transport=self._transport,
            )
            if resp.status_code >= 400:
                return []
            root = ET.fromstring(resp.content)
        except (EgressDenied, OSError, ET.ParseError, ValueError, httpx.HTTPError):
            return []

        hits: list[SearchHit] = []
        try:
            for i, item in enumerate(root.findall("channel/item")):
                title = _collapse_ws(item.findtext("title"))
                url = _collapse_ws(item.findtext("link"))
                if not url or not url_allowed(url, domains_allow, domains_deny):
                    continue
                hits.append(
                    SearchHit(
                        url=url,
                        title=title or url,
                        snippet=_strip_html(item.findtext("description")) or title or url,
                        source_engine="news",
                        rank=i,
                        published_at=parse_source_date(item.findtext("pubDate")),
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
        papers = await self._fetch_papers(query, limit, time_filter=time_filter)
        hits: list[SearchHit] = []
        for i, paper in enumerate(papers):
            hit = self._paper_to_hit(paper, i, domains_allow, domains_deny)
            if hit is None:
                continue
            hits.append(hit)
            if len(hits) >= limit:
                break
        return hits

    async def _fetch_papers(
        self, query: str, limit: int, *, time_filter: str | None = None
    ) -> list:
        params = {
            "query": query,
            "limit": limit,
            "fields": "title,abstract,url,year,publicationDate,externalIds",
        }
        recency_start = _recency_start(time_filter)
        if recency_start is not None:
            params["publicationDateOrYear"] = f"{recency_start.isoformat()}:"
        headers = {"x-api-key": self._api_key} if self._api_key else None
        try:
            resp = await _fetch_get(
                _with_params(f"{self._base_url}/paper/search", params),
                timeout_s=self._timeout,
                headers=headers,
                transport=self._transport,
            )
            if resp.status_code >= 400:
                return []
            payload = json.loads(resp.text)
            return payload.get("data", []) if isinstance(payload, dict) else []
        except (EgressDenied, OSError, ValueError, httpx.HTTPError):
            return []

    @staticmethod
    def _paper_to_hit(
        paper: object,
        i: int,
        domains_allow: frozenset[str] | None,
        domains_deny: frozenset[str] | None,
    ) -> SearchHit | None:
        if not isinstance(paper, dict):
            return None
        paper_id = str(paper.get("paperId") or "")
        url = str(paper.get("url") or "")
        if not url and paper_id:
            url = f"https://www.semanticscholar.org/paper/{paper_id}"
        if not url or not url_allowed(url, domains_allow, domains_deny):
            return None
        title = str(paper.get("title") or url)
        return SearchHit(
            url=url,
            title=title,
            snippet=str(paper.get("abstract") or ""),
            source_engine="semantic_scholar",
            rank=i,
            published_at=parse_source_date(paper.get("publicationDate")),
        )


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
        augmented = f"{query} ({' OR '.join('site:' + d for d in domains)})" if domains else query
        allow = frozenset(domains) | (domains_allow or frozenset()) if domains else domains_allow
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
                published_at=h.published_at,
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
        rows_by_provider = await self._gather_rows(
            query,
            limit=limit,
            domains_allow=domains_allow,
            domains_deny=domains_deny,
            time_filter=time_filter,
        )
        if not rows_by_provider:
            return []
        by_url, ordered_urls = self._merge_by_url(rows_by_provider)
        return self._finalize(by_url, ordered_urls, limit)

    async def _gather_rows(
        self,
        query: str,
        *,
        limit: int,
        domains_allow: frozenset[str] | None,
        domains_deny: frozenset[str] | None,
        time_filter: str | None,
    ) -> list[list[SearchHit]]:
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
        for provider, result in zip(self._providers, gathered, strict=True):
            if isinstance(result, BaseException):
                _LOG.warning(
                    "search provider %s failed: %s: %s",
                    getattr(provider, "name", type(provider).__name__),
                    type(result).__name__,
                    result,
                )
                continue
            if not result:
                continue
            rows_by_provider.append(list(result))
        return rows_by_provider

    @staticmethod
    def _merge_by_url(
        rows_by_provider: list[list[SearchHit]],
    ) -> tuple[dict[str, tuple[SearchHit, int, list[str]]], list[str]]:
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
                MultiSearchProvider._merge_hit(by_url, ordered_urls, hit)
        return by_url, ordered_urls

    @staticmethod
    def _merge_hit(
        by_url: dict[str, tuple[SearchHit, int, list[str]]],
        ordered_urls: list[str],
        hit: SearchHit,
    ) -> None:
        labels = _source_labels(hit.source_engine)
        key = source_url_key(hit.url)
        current = by_url.get(key)
        if current is None:
            by_url[key] = (hit, hit.rank, labels)
            ordered_urls.append(key)
            return
        best_hit, best_rank, engines = current
        for label in labels:
            if label not in engines:
                engines.append(label)
        if hit.rank < best_rank:
            best_hit, best_rank = hit, hit.rank
        by_url[key] = (best_hit, best_rank, engines)

    @staticmethod
    def _finalize(
        by_url: dict[str, tuple[SearchHit, int, list[str]]],
        ordered_urls: list[str],
        limit: int,
    ) -> list[SearchHit]:
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
