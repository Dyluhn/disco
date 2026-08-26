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
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser

import httpx
from disco.core.host_egress import EgressDenied, guarded_get, validate_untrusted_url

from .models import ExtractedDoc, Passage, SearchHit
from .provider_http import BoundedHttpExecutor, HttpAttemptPolicy, with_outcome
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

_TAVILY_ORIGIN = "https://api.tavily.com"
_FIRECRAWL_EXECUTORS: dict[str, BoundedHttpExecutor] = {}
# DDGS is synchronous. A small process-wide pool both keeps it off the event
# loop and prevents timed-out calls from creating an unbounded tail of stuck
# worker threads. There is deliberately no retry or fallback at this boundary.
_DDGS_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="disco-ddgs")


def _diagnostic_int(diagnostic: dict[str, object], key: str) -> int:
    value = diagnostic.get(key)
    return value if isinstance(value, int) else 0


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
        started = asyncio.get_running_loop().time()
        _timelimit: str | None = None
        if time_filter == "month":
            _timelimit = "m"
        elif time_filter == "week":
            _timelimit = "w"
        try:
            loop = asyncio.get_running_loop()
            rows = await asyncio.wait_for(
                loop.run_in_executor(
                    _DDGS_EXECUTOR,
                    self._blocking_search,
                    query,
                    limit,
                    _timelimit,
                ),
                timeout=self._timeout,
            )
        except TimeoutError as exc:
            _LOG.warning("ddgs search timed out")
            return [], _search_diagnostic(
                self.name,
                "timeout",
                started=started,
                error=exc,
            )
        except Exception as exc:  # noqa: BLE001 — classify provider failure, don't hide it
            _LOG.warning("ddgs search failed (%s)", type(exc).__name__)
            return [], _search_diagnostic(
                self.name,
                "upstream",
                started=started,
                error=exc,
            )
        if not isinstance(rows, list):
            return [], _search_diagnostic(
                self.name,
                "invalid_response",
                started=started,
            )
        _LOG.info("ddgs search %r → %d raw rows", query[:80], len(rows))
        hits: list[SearchHit] = []
        invalid_rows = 0
        for i, r in enumerate(rows):
            if not isinstance(r, Mapping):
                invalid_rows += 1
                continue
            raw_url = r.get("href") or r.get("url")
            if raw_url is not None and not isinstance(raw_url, str):
                invalid_rows += 1
                continue
            url = raw_url or ""
            if not url:
                continue
            try:
                allowed = url_allowed(url, domains_allow, domains_deny)
            except (TypeError, ValueError):
                invalid_rows += 1
                continue
            if not allowed:
                continue
            title = r.get("title", "")
            body = r.get("body", "")
            snippet = r.get("snippet", "")
            if any(
                value is not None and not isinstance(value, str)
                for value in (title, body, snippet)
            ):
                invalid_rows += 1
                continue
            hits.append(
                SearchHit(
                    url=url,
                    title=title or url,
                    snippet=body or snippet or "",
                    source_engine="ddgs",
                    rank=i,
                    published_at=parse_source_date(r.get("date")),
                )
            )
        diagnostic = _search_diagnostic(
            self.name,
            "invalid_response" if invalid_rows else ("ok" if hits else "empty"),
            started=started,
            result_count=len(hits),
        )
        if invalid_rows:
            diagnostic["invalid_row_count"] = invalid_rows
        return hits, diagnostic

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
        base_url: str = _TAVILY_ORIGIN,
        transport: httpx.AsyncBaseTransport | None = None,
        executor: BoundedHttpExecutor | None = None,
    ) -> None:
        self._key = api_key
        normalized_base = base_url.rstrip("/")
        if normalized_base != _TAVILY_ORIGIN:
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
        payload: dict[str, object] = {
            "query": query,
            "max_results": limit,
        }
        if time_filter in {"week", "month"}:
            payload["time_range"] = time_filter
        if domains_allow:
            payload["include_domains"] = sorted(domains_allow)
        if domains_deny:
            payload["exclude_domains"] = sorted(domains_deny)
        result = await self._executor.request(
            "POST",
            f"{self._base}/search",
            headers={"Authorization": f"Bearer {self._key}"},
            json=payload,
        )
        diagnostic = dict(result.diagnostic)
        if result.response is None or result.diagnostic["outcome"] != "ok":
            diagnostic["result_count"] = 0
            return [], diagnostic
        try:
            data = result.response.json()
            if not isinstance(data, dict):
                raise ValueError("response must be an object")
            rows = data.get("results")
            if not isinstance(rows, list):
                raise ValueError("results is not a list")
            if any(
                not isinstance(row, dict) or not isinstance(row.get("url"), str)
                for row in rows
            ):
                raise ValueError("result row has an invalid url")
        except (TypeError, ValueError):
            diagnostic = with_outcome(diagnostic, "invalid_response")
            diagnostic["result_count"] = 0
            return [], diagnostic
        hits = [
            SearchHit(
                url=h.get("url", ""),
                title=h.get("title", "") or h.get("url", ""),
                snippet=h.get("content", ""),
                source_engine="tavily",
                rank=i,
                published_at=parse_source_date(h.get("published_date")),
            )
            for i, h in enumerate(rows)
            if h.get("url") and url_allowed(h.get("url", ""), domains_allow, domains_deny)
        ]
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
        base_url: str = "https://api.search.brave.com",
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
        params: dict[str, str | int] = {"q": query, "count": max(1, min(limit, 20))}
        freshness = {"week": "pw", "month": "pm"}.get(time_filter or "")
        if freshness:
            params["freshness"] = freshness
        result = await self._executor.request(
            "GET",
            f"{self._base}/res/v1/web/search",
            params=params,
            headers={"X-Subscription-Token": self._key},
        )
        diagnostic = dict(result.diagnostic)
        if result.response is None or result.diagnostic["outcome"] != "ok":
            diagnostic["result_count"] = 0
            return [], diagnostic
        try:
            data = result.response.json()
            if not isinstance(data, dict):
                raise ValueError("response must be an object")
            web = data.get("web")
            results = web.get("results") if isinstance(web, dict) else None
            if not isinstance(results, list):
                raise ValueError("web.results is not a list")
            if any(
                not isinstance(row, dict) or not isinstance(row.get("url"), str)
                for row in results
            ):
                raise ValueError("result row has an invalid url")
        except (TypeError, ValueError):
            diagnostic = with_outcome(diagnostic, "invalid_response")
            diagnostic["result_count"] = 0
            return [], diagnostic
        hits: list[SearchHit] = []
        for i, h in enumerate(results):
            url = h.get("url", "")
            if not url or not url_allowed(url, domains_allow, domains_deny):
                continue
            hits.append(
                SearchHit(
                    url=url,
                    title=h.get("title", "") or url,
                    snippet=h.get("description", "") or h.get("snippet", ""),
                    source_engine="brave",
                    rank=i,
                    published_at=parse_source_date(h.get("page_age")),
                )
            )
            if len(hits) >= limit:
                break
        diagnostic = with_outcome(diagnostic, "ok" if hits else "empty")
        diagnostic["result_count"] = len(hits)
        return hits, diagnostic


# ===== EXTRACTION ============================================================


class _MarkdownExtractor(HTMLParser):
    """A tiny stdlib HTML→markdown reader: drops script/style/nav noise, turns
    headings/links/lists/paragraphs into markdown. The 'lesser-but-usable' bundled
    tier — no bs4/trafilatura, works anywhere Python does."""

    # NB: `head` is NOT skipped — its only text-bearing child is <title>, which we
    # want; its script/style children are skipped on their own.
    _SKIP = {"script", "style", "noscript", "nav", "footer", "svg"}
    _BLOCK = {"p", "div", "section", "article", "br", "li", "tr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.title = ""
        self._skip_depth = 0
        self._in_title = False
        self._href: str | None = None
        self._a_mark = -1

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.out.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "li":
            self.out.append("\n- ")
        elif tag in self._BLOCK:
            self.out.append("\n")
        elif tag == "a":
            self._href = dict(attrs).get("href")
            self._a_mark = len(self.out)  # remember where this anchor began
            self.out.append("[")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False
        elif tag == "a":
            href = self._href or ""
            inner = "".join(self.out[self._a_mark + 1 :]).strip()
            if inner:
                self.out.append(f"]({href})" if href else "]")
            else:
                # empty-anchor link (nav icon / menu) → drop it entirely, no `[](url)`
                del self.out[self._a_mark :]
            self._href = None
            self._a_mark = -1
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.out.append("\n")

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data.strip() + " "
            return
        text = data.replace("\xa0", " ")
        if text.strip() or text == " ":
            self.out.append(text)

    def markdown(self) -> str:
        raw = "".join(self.out)
        # collapse runs of blank lines + trailing spaces; drop now-empty list bullets
        # (a `- ` left behind after an empty-anchor link was removed).
        lines = [ln.rstrip() for ln in raw.splitlines()]
        md, blanks = [], 0
        for ln in lines:
            stripped = ln.strip()
            if stripped in ("-", "*", "•"):
                continue  # empty bullet → drop
            if not stripped:
                blanks += 1
                if blanks <= 1:
                    md.append("")
            else:
                blanks = 0
                md.append(ln)
        return "\n".join(md).strip()


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
        base_url: str = "https://api.firecrawl.dev",
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        executor: BoundedHttpExecutor | None = None,
    ) -> None:
        self._key = api_key
        self._base = base_url.rstrip("/") or "https://api.firecrawl.dev"
        self._transport = transport
        self._executor = executor or _firecrawl_executor(self._base)

    async def extract(self, url: str) -> ExtractedDoc:
        doc, _diagnostic = await self.extract_detailed(url)
        return doc

    async def extract_detailed(self, url: str) -> tuple[ExtractedDoc, dict[str, object]]:
        if not self._key:
            return ExtractedDoc(
                url=url,
                title="",
                content="",
                fetched_ok=False,
                error="no firecrawl key configured",
                status="error",
            ), {
                "provider": self.name,
                "outcome": "auth",
                "status_code": None,
                "attempts": 0,
                "latency_ms": 0,
                "retry_wait_ms": 0,
                "result_count": 0,
                "passage_count": 0,
                "max_concurrency": self._executor.max_concurrency,
            }
        try:
            validate_untrusted_url(url)
        except EgressDenied as e:
            return ExtractedDoc(
                url=url, title="", content="", fetched_ok=False, error=str(e), status="blocked"
            ), {
                "provider": self.name,
                "outcome": "upstream",
                "status_code": None,
                "attempts": 0,
                "latency_ms": 0,
                "retry_wait_ms": 0,
                "result_count": 0,
                "passage_count": 0,
                "max_concurrency": self._executor.max_concurrency,
            }
        result = await self._executor.request(
            "POST",
            f"{self._base}/v2/scrape",
            transport=self._transport,
            headers={"Authorization": f"Bearer {self._key}"},
            json={"url": url, "formats": ["markdown"]},
        )
        diagnostic = dict(result.diagnostic)
        if result.response is None or result.diagnostic["outcome"] != "ok":
            status_code = result.diagnostic.get("status_code")
            status = (
                "blocked"
                if result.diagnostic["outcome"] == "auth"
                else "paywalled"
                if status_code == 402
                else "not_found"
                if status_code == 404
                else "error"
            )
            return ExtractedDoc(
                url=url,
                title="",
                content="",
                fetched_ok=False,
                error=f"firecrawl {result.diagnostic['outcome']}",
                status=status,
            ), {**diagnostic, "result_count": 0, "passage_count": 0}
        try:
            payload = result.response.json()
            if not isinstance(payload, dict):
                raise ValueError("response must be an object")
            data = payload.get("data")
            if not isinstance(data, dict) or payload.get("success") is False:
                raise ValueError("missing successful data")
            meta = data.get("metadata") or {}
            content = data.get("markdown") or ""
            if not isinstance(meta, dict) or not isinstance(content, str):
                raise ValueError("invalid markdown response")
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
        successes = sum(doc.fetched_ok for doc in docs)
        failures = [diag for doc, diag in zip(docs, diagnostics, strict=True) if not doc.fetched_ok]
        failure_outcomes = [
            (
                str(diag.get("outcome"))
                if str(diag.get("outcome"))
                in {"rate_limited", "auth", "quota", "timeout", "upstream", "invalid_response"}
                else "upstream"
            )
            for diag in failures
            if str(diag.get("outcome", "empty")) not in {"", "empty", "ok"}
        ]
        if successes and failures:
            outcome = "partial_outage"
        elif successes:
            outcome = "ok"
        elif failure_outcomes:
            outcome = failure_outcomes[0]
        else:
            outcome = "empty"
        failed_status_code: int | None = None
        for diagnostic in failures:
            if str(diagnostic.get("outcome", "empty")) in {"", "empty", "ok"}:
                continue
            candidate_status = diagnostic.get("status_code")
            if isinstance(candidate_status, int):
                failed_status_code = candidate_status
                break
        aggregate: dict[str, object] = {
            "provider": self.name,
            "outcome": outcome,
            "status_code": failed_status_code,
            "attempts": sum(_diagnostic_int(diag, "attempts") for diag in diagnostics),
            # Requests run concurrently; max is the truthful batch wall-time
            # approximation. Summing would overstate the user-visible stall.
            "latency_ms": max(
                [_diagnostic_int(diag, "latency_ms") for diag in diagnostics], default=0
            ),
            "retry_wait_ms": max(
                [_diagnostic_int(diag, "retry_wait_ms") for diag in diagnostics], default=0
            ),
            "result_count": successes,
            "passage_count": sum(len(doc.passages) for doc in docs),
            "max_concurrency": max(
                [_diagnostic_int(diag, "max_concurrency") for diag in diagnostics], default=0
            ),
        }
        return docs, aggregate
