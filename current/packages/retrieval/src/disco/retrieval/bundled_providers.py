"""Universal data providers (universal-readiness-plan §B). Each slot — search and
extraction — has THREE tiers so the app works for anyone:

  search:      ddgs (BUNDLED, no key) · searxng (self-host) · tavily (paid key)
  extraction:  local (BUNDLED, no service) · crawl4ai (self-host) · firecrawl (paid)

The BUNDLED tiers (`DdgsSearchProvider`, `LocalExtractionProvider`) need no
credential and no container — they are the first-run defaults so a fresh install
searches + reads the web the instant it's downloaded. The paid/self-host tiers are
upgrades configured in Settings.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Literal

import httpx
from disco.core._provider_contracts import (
    BRAVE_ORIGIN,
    FIRECRAWL_ORIGIN,
    TAVILY_ORIGIN,
    brave_search_request,
    firecrawl_extract_request,
    parse_brave_search_response,
    parse_firecrawl_response,
    parse_tavily_search_response,
    tavily_search_request,
)
from disco.core.host_egress import EgressDenied, guarded_get, validate_untrusted_url

from ._markdown import MarkdownExtractor as _MarkdownExtractor
from .models import ExtractedDoc, Passage, SearchHit
from .provider_http import BoundedHttpExecutor, HttpAttemptPolicy, HttpResult, with_outcome
from .source_adapters import _search_diagnostic
from .url_policy import parse_source_date, url_allowed

_LOG = logging.getLogger("disco.retrieval.providers")

# Content alone isn't enough: the research pipeline cites PASSAGES (ExtractedDoc.
# passages), so an extractor that returns only `content` yields zero passages and
# the run reports "couldn't read any of them". Every extractor must chunk.
_PASSAGE_CHARS = 1_100
_MAX_PASSAGES = 12


def chunk_passages(url: str, title: str, content: str) -> list[Passage]:
    """Split markdown content into citable Passages — same shape as the Crawl4AI
    provider (pack paragraphs to ~1.1k chars, cap at 12, stable per-url ids)."""
    sid = hashlib.md5(url.encode()).hexdigest()[:6]  # noqa: S324 — non-security id
    paras = [p.strip() for p in content.split("\n\n") if len(p.strip()) >= 40]
    passages: list[Passage] = []
    buf = ""
    for para in paras:
        if buf and len(buf) + len(para) > _PASSAGE_CHARS:
            passages.append(
                Passage(
                    id=f"{sid}_p{len(passages)}",
                    source_url=url,
                    source_title=title,
                    text=buf.strip(),
                )
            )
            buf = ""
        buf += para + "\n\n"
        if len(passages) >= _MAX_PASSAGES:
            break
    if buf.strip() and len(passages) < _MAX_PASSAGES:
        passages.append(
            Passage(
                id=f"{sid}_p{len(passages)}",
                source_url=url,
                source_title=title,
                text=buf.strip(),
            )
        )
    return passages


_UA = "Mozilla/5.0 (compatible; disco/1.0; +https://disco.local)"
_TIMEOUT = httpx.Timeout(20.0)
_SEARCH_POLICY = HttpAttemptPolicy(deadline_s=15.0)
_EXTRACTION_POLICY = HttpAttemptPolicy(deadline_s=30.0)

_FIRECRAWL_EXECUTORS: dict[str, BoundedHttpExecutor] = {}
# DDGS is synchronous. A small process-wide pool both keeps it off the event
# loop and prevents timed-out calls from creating an unbounded tail of stuck
# worker threads. There is deliberately no retry or fallback at this boundary.
_DDGS_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="disco-ddgs")


def _diagnostic_int(diagnostic: dict[str, object], key: str) -> int:
    value = diagnostic.get(key)
    return value if isinstance(value, int) else 0


def _ddgs_time_filter(time_filter: str | None) -> str | None:
    return {"month": "m", "week": "w"}.get(time_filter or "")


async def _ddgs_rows(
    blocking_search: Callable[[str, int, str | None], list[dict]],
    query: str,
    limit: int,
    timelimit: str | None,
    timeout: float,
) -> object:
    loop = asyncio.get_running_loop()
    rows = await asyncio.wait_for(
        loop.run_in_executor(_DDGS_EXECUTOR, blocking_search, query, limit, timelimit),
        timeout=timeout,
    )
    return rows


def _ddgs_hits(
    rows: list[object] | list[dict],
    domains_allow: frozenset[str] | None,
    domains_deny: frozenset[str] | None,
) -> tuple[list[SearchHit], int]:
    hits: list[SearchHit] = []
    invalid_rows = 0
    for i, row in enumerate(rows):
        hit, invalid = _ddgs_hit(row, i, domains_allow, domains_deny)
        invalid_rows += invalid
        if hit is not None:
            hits.append(hit)
    return hits, invalid_rows


def _ddgs_hit(
    row: object,
    rank: int,
    domains_allow: frozenset[str] | None,
    domains_deny: frozenset[str] | None,
) -> tuple[SearchHit | None, int]:
    if not isinstance(row, Mapping):
        return None, 1
    url, invalid = _ddgs_row_url(row)
    if invalid:
        return None, invalid
    if not url:
        return None, 0
    try:
        allowed = url_allowed(url, domains_allow, domains_deny)
    except (TypeError, ValueError):
        return None, 1
    if not allowed:
        return None, 0
    title, body, snippet = (row.get(key, "") for key in ("title", "body", "snippet"))
    if not _ddgs_text_fields_valid(title, body, snippet):
        return None, 1
    return SearchHit(
        url=url,
        title=title or url,
        snippet=body or snippet or "",
        source_engine="ddgs",
        rank=rank,
        published_at=parse_source_date(row.get("date")),
    ), 0


def _ddgs_row_url(row: Mapping[str, object]) -> tuple[str, int]:
    raw_url = row.get("href") or row.get("url")
    if raw_url is not None and not isinstance(raw_url, str):
        return "", 1
    return raw_url or "", 0


def _ddgs_text_fields_valid(*values: object) -> bool:
    return all(value is None or isinstance(value, str) for value in values)


async def _ddgs_search_detailed(
    provider_name: str,
    blocking_search: Callable[[str, int, str | None], list[dict]],
    query: str,
    limit: int,
    domains_allow: frozenset[str] | None,
    domains_deny: frozenset[str] | None,
    time_filter: str | None,
    timeout: float,
) -> tuple[list[SearchHit], dict[str, object]]:
    started = asyncio.get_running_loop().time()
    try:
        rows = await _ddgs_rows(
            blocking_search, query, limit, _ddgs_time_filter(time_filter), timeout
        )
    except TimeoutError as exc:
        _LOG.warning("ddgs search timed out")
        return [], _search_diagnostic(provider_name, "timeout", started=started, error=exc)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("ddgs search failed (%s)", type(exc).__name__)
        return [], _search_diagnostic(provider_name, "upstream", started=started, error=exc)
    if not isinstance(rows, list):
        return [], _search_diagnostic(
            provider_name,
            "invalid_response",
            started=started,
            error=ValueError("DDGS response must be a list"),
        )
    hits, invalid_rows = _ddgs_hits(rows, domains_allow, domains_deny)
    outcome = "invalid_response" if invalid_rows else ("ok" if hits else "empty")
    diagnostic = _search_diagnostic(provider_name, outcome, started=started, result_count=len(hits))
    if invalid_rows:
        diagnostic["invalid_row_count"] = invalid_rows
    return hits, diagnostic


def _tavily_hits(
    rows: list[dict],
    domains_allow: frozenset[str] | None,
    domains_deny: frozenset[str] | None,
) -> list[SearchHit]:
    return [
        SearchHit(
            url=row.get("url", ""),
            title=row.get("title", "") or row.get("url", ""),
            snippet=row.get("content", ""),
            source_engine="tavily",
            rank=i,
            published_at=parse_source_date(row.get("published_date")),
        )
        for i, row in enumerate(rows)
        if row.get("url") and url_allowed(row.get("url", ""), domains_allow, domains_deny)
    ]


def _brave_hits(
    rows: list[dict],
    limit: int,
    domains_allow: frozenset[str] | None,
    domains_deny: frozenset[str] | None,
) -> list[SearchHit]:
    hits: list[SearchHit] = []
    for i, row in enumerate(rows):
        url = row.get("url", "")
        if not url or not url_allowed(url, domains_allow, domains_deny):
            continue
        hits.append(
            SearchHit(
                url=url,
                title=row.get("title", "") or url,
                snippet=row.get("description", "") or row.get("snippet", ""),
                source_engine="brave",
                rank=i,
                published_at=parse_source_date(row.get("page_age")),
            )
        )
        if len(hits) >= limit:
            break
    return hits


def _firecrawl_unavailable(
    provider: str,
    url: str,
    reason: str,
    executor: BoundedHttpExecutor,
    status: Literal["ok", "paywalled", "blocked", "not_found", "error"],
) -> tuple[ExtractedDoc, dict[str, object]]:
    return ExtractedDoc(
        url=url, title="", content="", fetched_ok=False, error=reason, status=status
    ), {
        "provider": provider,
        "outcome": "auth" if status == "error" else "upstream",
        "status_code": None,
        "attempts": 0,
        "latency_ms": 0,
        "retry_wait_ms": 0,
        "result_count": 0,
        "passage_count": 0,
        "max_concurrency": executor.max_concurrency,
    }


def _firecrawl_response(
    result: HttpResult,
    url: str,
    provider: str,
) -> tuple[ExtractedDoc, dict[str, object]]:
    response = result.response
    diagnostic = dict(result.diagnostic)
    if response is None or result.diagnostic["outcome"] != "ok":
        return _firecrawl_failed_response(result, url, diagnostic)
    try:
        meta, content = parse_firecrawl_response(response)
    except (TypeError, ValueError):
        diagnostic = with_outcome(diagnostic, "invalid_response")
        diagnostic.update(result_count=0, passage_count=0)
        return ExtractedDoc(
            url=url,
            title=url,
            content="",
            fetched_ok=False,
            error="invalid firecrawl response",
            status="error",
        ), diagnostic
    title = str(meta.get("title") or url)
    passages = chunk_passages(url, title, content)
    if not passages:
        diagnostic = with_outcome(diagnostic, "empty")
        diagnostic.update(result_count=0, passage_count=0)
        return ExtractedDoc(
            url=url,
            title=title,
            content=content,
            fetched_ok=False,
            error="no readable content",
            status="error",
        ), diagnostic
    diagnostic = with_outcome(diagnostic, "ok")
    diagnostic.update(result_count=1, passage_count=len(passages))
    return ExtractedDoc(
        url=url, title=title, content=content, passages=passages, fetched_ok=True
    ), diagnostic


def _firecrawl_failed_response(
    result: HttpResult, url: str, diagnostic: dict[str, object]
) -> tuple[ExtractedDoc, dict[str, object]]:
    code = result.diagnostic.get("status_code")
    outcome = result.diagnostic["outcome"]
    status: Literal["blocked", "paywalled", "not_found", "error"] = (
        "blocked"
        if outcome == "auth"
        else "paywalled"
        if code == 402
        else "not_found"
        if code == 404
        else "error"
    )
    return ExtractedDoc(
        url=url,
        title="",
        content="",
        fetched_ok=False,
        error=f"firecrawl {outcome}",
        status=status,
    ), {**diagnostic, "result_count": 0, "passage_count": 0}


def _aggregate_firecrawl(
    docs: list[ExtractedDoc], diagnostics: list[dict[str, object]], provider: str, concurrency: int
) -> dict[str, object]:
    successes = sum(doc.fetched_ok for doc in docs)
    failures = [diag for doc, diag in zip(docs, diagnostics, strict=True) if not doc.fetched_ok]
    allowed = {"rate_limited", "auth", "quota", "timeout", "upstream", "invalid_response"}
    outcomes = [
        str(diag.get("outcome")) if str(diag.get("outcome")) in allowed else "upstream"
        for diag in failures
        if str(diag.get("outcome", "empty")) not in {"", "empty", "ok"}
    ]
    outcome = _firecrawl_batch_outcome(successes, failures, outcomes)
    status_code = _first_status_code(failures)

    def ints(key: str) -> list[int]:
        return [_diagnostic_int(diag, key) for diag in diagnostics]

    return {
        "provider": provider,
        "outcome": outcome,
        "status_code": status_code,
        "attempts": sum(ints("attempts")),
        "latency_ms": max(ints("latency_ms"), default=0),
        "retry_wait_ms": max(ints("retry_wait_ms"), default=0),
        "result_count": successes,
        "passage_count": sum(len(doc.passages) for doc in docs),
        "max_concurrency": max(ints("max_concurrency"), default=concurrency),
    }


def _firecrawl_batch_outcome(
    successes: int, failures: list[dict[str, object]], outcomes: list[str]
) -> str:
    if successes and failures:
        return "partial_outage"
    if successes:
        return "ok"
    return outcomes[0] if outcomes else "empty"


def _first_status_code(failures: list[dict[str, object]]) -> int | None:
    for diagnostic in failures:
        if str(diagnostic.get("outcome", "empty")) in {"", "empty", "ok"}:
            continue
        code = diagnostic.get("status_code")
        if isinstance(code, int):
            return code
    return None


def _firecrawl_executor(
    base_url: str,
) -> BoundedHttpExecutor:
    """Reuse one bounded executor for each process-wide Firecrawl endpoint."""

    executor = _FIRECRAWL_EXECUTORS.get(base_url)
    if executor is None:
        executor = BoundedHttpExecutor(
            "firecrawl",
            policy=_EXTRACTION_POLICY,
            concurrency=2,
        )
        _FIRECRAWL_EXECUTORS[base_url] = executor
    return executor


# ===== SEARCH ================================================================


class DdgsSearchProvider:
    """(c) BUNDLED search — DuckDuckGo via the maintained `ddgs` lib, in-process, no
    key. The first-run default. A single call is made per ordinary Search request;
    failures are returned as bounded diagnostics instead of being misreported as a
    valid empty result or retried by a second planner."""

    name = "ddgs"

    def __init__(self, *, timeout_s: float = _SEARCH_POLICY.deadline_s) -> None:
        self._timeout = max(0.001, float(timeout_s))

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
        return await _ddgs_search_detailed(
            self.name,
            self._blocking_search,
            query,
            limit,
            domains_allow,
            domains_deny,
            time_filter,
            self._timeout,
        )

    def _blocking_search(self, query: str, limit: int, timelimit: str | None = None) -> list[dict]:
        from ddgs import DDGS  # lazy: keeps import cost off the hot path

        with DDGS() as d:
            kw: dict = {"max_results": limit}
            if timelimit is not None:
                kw["timelimit"] = timelimit
            return list(d.text(query, **kw))


class TavilySearchProvider:
    """(b) PAID search — Tavily (BYO key). A clean answer-oriented search API."""

    name = "tavily"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = TAVILY_ORIGIN,
        transport: httpx.AsyncBaseTransport | None = None,
        executor: BoundedHttpExecutor | None = None,
    ) -> None:
        self._key = api_key
        normalized_base = base_url.rstrip("/")
        if normalized_base != TAVILY_ORIGIN:
            raise ValueError("Tavily base_url must use the official API origin")
        self._base = normalized_base
        self._executor = executor or BoundedHttpExecutor(
            "tavily", transport=transport, policy=_SEARCH_POLICY
        )

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
        if not self._key:
            return [], {
                "provider": self.name,
                "outcome": "auth",
                "status_code": None,
                "attempts": 0,
                "latency_ms": 0,
                "retry_wait_ms": 0,
                "result_count": 0,
                "max_concurrency": self._executor.max_concurrency,
            }
        request = tavily_search_request(
            self._base,
            self._key,
            query,
            limit=limit,
            time_filter=time_filter,
            domains_allow=domains_allow,
            domains_deny=domains_deny,
        )
        result = await self._executor.request(
            request.method,
            request.url,
            headers=request.headers,
            params=request.params,
            json=request.json,
        )
        diagnostic = dict(result.diagnostic)
        if result.response is None or result.diagnostic["outcome"] != "ok":
            diagnostic["result_count"] = 0
            return [], diagnostic
        try:
            rows = parse_tavily_search_response(result.response)
        except (TypeError, ValueError):
            diagnostic = with_outcome(diagnostic, "invalid_response")
            diagnostic["result_count"] = 0
            return [], diagnostic
        hits = _tavily_hits(rows, domains_allow, domains_deny)
        diagnostic = with_outcome(diagnostic, "ok" if hits else "empty")
        diagnostic["result_count"] = len(hits)
        return hits[:limit], diagnostic


class BraveSearchProvider:
    """(b) PAID search — Brave Search API (BYO key)."""

    name = "brave"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = BRAVE_ORIGIN,
        transport: httpx.AsyncBaseTransport | None = None,
        executor: BoundedHttpExecutor | None = None,
    ) -> None:
        self._key = api_key
        self._base = base_url.rstrip("/")
        self._executor = executor or BoundedHttpExecutor(
            "brave", transport=transport, policy=_SEARCH_POLICY
        )

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
        if not self._key:
            return [], {
                "provider": self.name,
                "outcome": "auth",
                "status_code": None,
                "attempts": 0,
                "latency_ms": 0,
                "retry_wait_ms": 0,
                "result_count": 0,
                "max_concurrency": self._executor.max_concurrency,
            }
        request = brave_search_request(
            self._base,
            self._key,
            query,
            limit=limit,
            time_filter=time_filter,
        )
        result = await self._executor.request(
            request.method,
            request.url,
            params=request.params,
            headers=request.headers,
            json=request.json,
        )
        diagnostic = dict(result.diagnostic)
        if result.response is None or result.diagnostic["outcome"] != "ok":
            diagnostic["result_count"] = 0
            return [], diagnostic
        try:
            results = parse_brave_search_response(result.response)
        except (TypeError, ValueError):
            diagnostic = with_outcome(diagnostic, "invalid_response")
            diagnostic["result_count"] = 0
            return [], diagnostic
        hits = _brave_hits(results, limit, domains_allow, domains_deny)
        diagnostic = with_outcome(diagnostic, "ok" if hits else "empty")
        diagnostic["result_count"] = len(hits)
        return hits, diagnostic


# ===== EXTRACTION ============================================================


class LocalExtractionProvider:
    """(c) BUNDLED extraction — in-process httpx fetch + stdlib HTML→markdown. No
    service, no key. The first-run default; the 'garden works offline' piece."""

    name = "local"

    async def extract(self, url: str) -> ExtractedDoc:
        try:
            r = await guarded_get(url, timeout_s=20.0, headers={"User-Agent": _UA})
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code}")
            html = r.text
        except EgressDenied as e:
            _LOG.info("local extract DENIED %s → %s", url[:80], e)
            return ExtractedDoc(
                url=url, title="", content="", fetched_ok=False, error=str(e), status="error"
            )
        except RuntimeError as e:
            code_match = re.search(r"HTTP (\d+)", str(e))
            code = int(code_match.group(1)) if code_match else 0
            status = "not_found" if code == 404 else "blocked" if code in (401, 403) else "error"
            _LOG.info("local extract FAIL %s → HTTP %s (%s)", url[:80], code, status)
            return ExtractedDoc(
                url=url, title="", content="", fetched_ok=False, error=str(e), status=status
            )
        except OSError as e:
            _LOG.info("local extract FAIL %s → %s: %s", url[:80], type(e).__name__, e)
            return ExtractedDoc(
                url=url, title="", content="", fetched_ok=False, error=str(e), status="error"
            )
        p = _MarkdownExtractor()
        p.feed(html)
        md = p.markdown()
        title = p.title.strip() or url
        passages = chunk_passages(url, title, md)
        if not passages:  # JS-only / empty page — honest failure, not a silent 0-passage doc
            _LOG.info("local extract %s → 0 usable passages (empty/JS-only)", url[:80])
            return ExtractedDoc(
                url=url,
                title=title,
                content=md,
                fetched_ok=False,
                error="no readable content",
                status="error",
            )
        _LOG.info("local extract %s → %d chars, %d passages", url[:80], len(md), len(passages))
        return ExtractedDoc(url=url, title=title, content=md, passages=passages, fetched_ok=True)

    async def extract_many(self, urls: list[str]) -> list[ExtractedDoc]:
        return list(await asyncio.gather(*(self.extract(u) for u in urls)))


class FirecrawlExtractionProvider:
    """(b) PAID extraction — Firecrawl V2 (BYO key), returns clean markdown.

    Instances for the same endpoint share one process-wide two-slot executor,
    including source-override runs. A test-only executor may be injected.
    """

    name = "firecrawl"

    def __init__(
        self,
        api_key: str,
        base_url: str = FIRECRAWL_ORIGIN,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        executor: BoundedHttpExecutor | None = None,
    ) -> None:
        self._key = api_key
        self._base = base_url.rstrip("/") or FIRECRAWL_ORIGIN
        self._transport = transport
        self._executor = executor or _firecrawl_executor(self._base)

    async def extract(self, url: str) -> ExtractedDoc:
        doc, _diagnostic = await self.extract_detailed(url)
        return doc

    async def extract_detailed(self, url: str) -> tuple[ExtractedDoc, dict[str, object]]:
        if not self._key:
            return _firecrawl_unavailable(
                self.name, url, "no firecrawl key configured", self._executor, "error"
            )
        try:
            validate_untrusted_url(url)
        except EgressDenied as e:
            return _firecrawl_unavailable(self.name, url, str(e), self._executor, "blocked")
        request = firecrawl_extract_request(self._base, self._key, url)
        result = await self._executor.request(
            request.method,
            request.url,
            transport=self._transport,
            headers=request.headers,
            params=request.params,
            json=request.json,
        )
        return _firecrawl_response(result, url, self.name)

    async def extract_many(self, urls: list[str]) -> list[ExtractedDoc]:
        docs, _diagnostic = await self.extract_many_detailed(urls)
        return docs

    async def extract_many_detailed(
        self, urls: list[str]
    ) -> tuple[list[ExtractedDoc], dict[str, object]]:
        if not urls:
            return [], {
                "provider": self.name,
                "outcome": "empty",
                "attempts": 0,
                "latency_ms": 0,
                "retry_wait_ms": 0,
                "result_count": 0,
                "passage_count": 0,
                "max_concurrency": self._executor.max_concurrency,
            }
        gathered = await asyncio.gather(*(self.extract_detailed(url) for url in urls))
        docs = [item[0] for item in gathered]
        diagnostics = [item[1] for item in gathered]
        return docs, _aggregate_firecrawl(
            docs, diagnostics, self.name, self._executor.max_concurrency
        )
