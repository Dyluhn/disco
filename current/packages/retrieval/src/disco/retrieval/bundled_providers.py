"""Universal data providers (universal-readiness-plan §B). Each slot — search and
extraction — has THREE tiers so the app works for anyone:

  search:      bundled keyless composite · searxng (self-host) · tavily (paid key)
  extraction:  local (BUNDLED, no service) · crawl4ai (self-host) · firecrawl (paid)

The two PAID tiers cost money per call, so their failures are classified rather
than swallowed: a rejected key, an exhausted plan and a rate limit each get their
own named marker (see `_provider_marker`), none of them is retried, and none of
them can reach the research model disguised as "no results".

The BUNDLED tiers need no credential and no container — they are the first-run
defaults so a fresh install searches + reads the web the instant it's downloaded.
Bundled EXTRACTION is `LocalExtractionProvider`, here; bundled SEARCH is the
keyless composite assembled in `live.build_keyless_search` out of
`_mcp_search_providers` plus the open academic/reference adapters in
`source_adapters`. The paid/self-host tiers are upgrades configured in Settings.

The bundled search tier used to be `DdgsSearchProvider`, removed 2026-09-02. It
scraped the same HTML endpoints SearXNG does, so it inherited the same IP bans,
and — the reason it had to go — a rate limit came back from it as an empty list
that no caller could tell apart from "the web has nothing". Every bundled search
adapter that replaced it implements `search_detailed` and names its refusals.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from html.parser import HTMLParser

import httpx
from disco.core.host_egress import EgressDenied, guarded_get

from ._extraction_fallbacks import recover_blocked_pages
from ._extraction_text import CORRUPTED_TEXT_ERROR, corrupted_text, reject_challenge_page
from ._pdf_extract import pdf_document
from ._transport_retry import extract_with_isolation
from .models import ExtractedDoc, Passage, SearchHit
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


# A bot-shaped request is refused on its headers alone: on the 2026-09-07 keyless
# runs, ScienceDirect, doi.org, MDPI, RSC and several .gov hosts answered the old
# `compatible; disco/1.0` agent with HTTP 403 and nothing else. Send what an
# ordinary desktop browser sends, so the wall has to judge the page and not the
# client. Accept-Encoding lists ONLY the codings `guarded_get` can decode — an
# advertised br or zstd would come back as bytes this process cannot read.
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,application/pdf,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}
_TIMEOUT = httpx.Timeout(20.0)

# Government and lab report PDFs routinely run past `guarded_get`'s 5 MB default:
# the four sources it denied on 2026-09-07 measure 6.1 MB (swri.org), 8.9 MB
# (osti.gov) and 10.2 MB (rosap.ntl.bts.gov), and each came back as an egress
# denial instead of its text. 25 MB clears the largest of those by 2.4x and still
# refuses the hundred-megabyte scans, which carry no text layer to read anyway.
# Only extraction reads this much — `guarded_get` keeps its own default for every
# other caller — and the passages stay bounded whatever the file size, because
# `pdf_document` caps pages and characters and `chunk_passages` caps passages.
_EXTRACT_MAX_BYTES = 25_000_000

# ---- paid-provider failure markers ------------------------------------------
#
# A metered provider's failure is usually a fact about the ACCOUNT, not about the
# query or the page — and the money is lost when it reaches the research model as
# a semantic result. These build the ONE bounded phrase each failure is named by;
# `_transport_retry` reads "auth rejected" as the auth class and "quota"/"rate
# limit" as the rate/quota class, and never retries into either. Nothing from the
# provider's own response body is ever copied into a marker (keys travel in
# headers, and unbounded upstream prose has no place in a prompt or a trace).
#
# A VALID EMPTY RESPONSE carries no marker at all, and so stays the honest zero
# it is.
_AUTH_STATUSES = frozenset({401, 403})


def _provider_marker(provider: str, status_code: int | None, exception: str = "") -> str:
    """Name one paid-provider failure in the fixed classification vocabulary."""
    if status_code in _AUTH_STATUSES:
        return f"{provider} auth rejected (http {status_code})"
    if status_code == 402:
        return f"{provider} quota exhausted (http 402)"
    if status_code == 429:
        return f"{provider} rate limit (http 429)"
    if status_code is not None:
        return f"{provider} upstream error (http {status_code})"
    return f"{provider} transport error ({exception or 'unknown'})"


def _no_key_marker(provider: str) -> str:
    """A provider configured without a key is an auth fault, not an empty world."""
    return f"{provider} auth rejected (no api key configured)"


def _status_code(exc: Exception) -> int | None:
    """The HTTP status behind an httpx failure, when it had one."""
    return exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None


def _failure_diagnostic(marker: str, status_code: int | None, started: float) -> dict[str, object]:
    """The search diagnostic shape the degradation classifier already reads."""
    return {
        "provider_error": marker,
        "status_code": status_code,
        "result_count": 0,
        "latency_ms": max(0, int((time.perf_counter() - started) * 1_000)),
    }


# ===== SEARCH ================================================================


class TavilySearchProvider:
    """(b) PAID search — Tavily (BYO key). A clean answer-oriented search API.

    Tavily's current contract is ``POST https://api.tavily.com/search`` with
    ``Authorization: Bearer <key>`` and the key ABSENT from the JSON body. This
    adapter used to send the obsolete body-key form and no header, so the very
    first credited call would have been rejected — and, because every failure
    collapsed into ``return []``, that rejection would have reached the research
    model as "no results" and been answered with pointless query rewrites.

    So the failures are now NAMED. ``search_detailed`` returns the same
    (hits, diagnostic) shape SearXNG does; the diagnostic's ``provider_error``
    marker is what makes a rejected key an auth outage and a 402/429 a
    quota/rate outage instead of a research finding. A real empty result set
    carries no marker and stays a clean zero.
    """

    name = "tavily"
    endpoint = "https://api.tavily.com/search"

    def __init__(self, api_key: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._key = api_key
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
        started = time.perf_counter()
        if not self._key:
            return [], _failure_diagnostic(_no_key_marker(self.name), None, started)
        payload = self._payload(query, limit, domains_allow, domains_deny, time_filter)
        try:
            async with httpx.AsyncClient(
                timeout=_TIMEOUT,
                transport=self._transport,
                trust_env=False,
                follow_redirects=False,
            ) as c:
                r = await c.post(
                    self.endpoint,
                    json=payload,
                    headers={"Authorization": f"Bearer {self._key}"},
                )
                r.raise_for_status()
                status, data = r.status_code, r.json()
        except (httpx.HTTPError, ValueError) as exc:
            code = _status_code(exc)
            _LOG.warning("tavily search failed: %s: %s", type(exc).__name__, code or exc)
            marker = _provider_marker(self.name, code, type(exc).__name__)
            return [], _failure_diagnostic(marker, code, started)
        results = data.get("results", [])
        rows = results if isinstance(results, list) else []
        return self._to_hits(rows, domains_allow, domains_deny), {
            "status_code": status,
            "result_count": len(rows),
            "latency_ms": max(0, int((time.perf_counter() - started) * 1_000)),
        }

    @staticmethod
    def _payload(
        query: str,
        limit: int,
        domains_allow: frozenset[str] | None,
        domains_deny: frozenset[str] | None,
        time_filter: str | None,
    ) -> dict[str, object]:
        """The request body — credential-free; the key rides in the header."""
        payload: dict[str, object] = {"query": query, "max_results": limit}
        if time_filter in {"week", "month"}:
            payload["time_range"] = time_filter
        if domains_allow:
            payload["include_domains"] = sorted(domains_allow)
        if domains_deny:
            payload["exclude_domains"] = sorted(domains_deny)
        return payload

    @staticmethod
    def _to_hits(
        rows: list,
        domains_allow: frozenset[str] | None,
        domains_deny: frozenset[str] | None,
    ) -> list[SearchHit]:
        return [
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


class BraveSearchProvider:
    """(b) PAID search — Brave Search API (BYO key)."""

    name = "brave"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.search.brave.com",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._key = api_key
        self._base = base_url.rstrip("/")
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
        if not self._key:
            return []
        params: dict[str, str | int] = {"q": query, "count": max(1, min(limit, 20))}
        freshness = {"week": "pw", "month": "pm"}.get(time_filter or "")
        if freshness:
            params["freshness"] = freshness
        try:
            async with httpx.AsyncClient(
                timeout=_TIMEOUT,
                transport=self._transport,
                trust_env=False,
                follow_redirects=False,
            ) as c:
                r = await c.get(
                    f"{self._base}/res/v1/web/search",
                    params=params,
                    headers={"X-Subscription-Token": self._key},
                )
                r.raise_for_status()
                data = r.json()
        except (httpx.HTTPError, ValueError):
            return []
        results = (data.get("web") or {}).get("results", [])
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
        return hits


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
    """(c) BUNDLED extraction — guarded fetch, HTML markdown and PDF text.

    No extraction service or key is required for public HTML and text PDFs.
    """

    name = "local"

    async def extract(self, url: str) -> ExtractedDoc:
        try:
            r = await guarded_get(
                url,
                timeout_s=20.0,
                headers=_BROWSER_HEADERS,
                max_bytes=_EXTRACT_MAX_BYTES,
            )
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code}")
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
        pdf = await asyncio.to_thread(pdf_document, url, r, chunk=chunk_passages)
        if pdf is not None:
            return pdf
        p = _MarkdownExtractor()
        p.feed(r.text)
        md = p.markdown()
        title = p.title.strip() or url
        passages = chunk_passages(url, title, md)
        if not passages or corrupted_text(md):
            _LOG.info("local extract %s → unreadable content", url[:80])
            return ExtractedDoc(
                url=url,
                title=title,
                content=md,
                fetched_ok=False,
                error=CORRUPTED_TEXT_ERROR if corrupted_text(md) else "no readable content",
                status="error",
            )
        _LOG.info("local extract %s → %d chars, %d passages", url[:80], len(md), len(passages))
        return reject_challenge_page(
            ExtractedDoc(url=url, title=title, content=md, passages=passages, fetched_ok=True)
        )

    async def extract_many(self, urls: list[str]) -> list[ExtractedDoc]:
        """Read every URL, then re-read the anti-bot refusals out of the archive.

        The bundled tier meets the same publisher walls the crawler does, and the
        Internet Archive holds the same copies, so it gets the same recovery the
        Crawl4AI path already has. Whatever the archive cannot supply is left as
        the original refusal.
        """
        docs = list(await asyncio.gather(*(self.extract(url) for url in urls)))
        recovered = await recover_blocked_pages(
            {doc.url: doc for doc in docs}, fetch_archive=self.extract, chunk=chunk_passages
        )
        return [recovered.get(doc.url, doc) for doc in docs]


# Firecrawl scrapes ONE url per request, and one exhaustive research turn can
# ask for two dozen at once. Bound how many of a batch are ever in flight so a
# single turn cannot open a stampede against a metered account.
_FIRECRAWL_CONCURRENCY = 2
_FIRECRAWL_DEFAULT_BASE = "https://api.firecrawl.dev"


def _firecrawl_base(base_url: str) -> str:
    """Normalise a configured base so the version path is appended exactly once.

    A user who pasted the versioned URL from Firecrawl's own docs must not end up
    requesting ``/v2/v2/scrape``. This strips a trailing version segment; it does
    NOT guess between contracts — the adapter speaks V2 and only V2.
    """
    root = base_url.rstrip("/") or _FIRECRAWL_DEFAULT_BASE
    for version in ("/v1", "/v2"):
        if root.endswith(version):
            return root[: -len(version)]
    return root


class FirecrawlExtractionProvider:
    """(b) PAID extraction — Firecrawl V2 scrape (BYO key), returns clean markdown.

    Current contract: ``POST {base}/v2/scrape``, ``Authorization: Bearer <key>``,
    ONE url per request, answering ``{"data": {"markdown", "metadata"}}``.

    Per-URL isolation is the point of the batch path: one unreadable publisher
    PDF must cost only itself, never the batch it happened to travel in. And a
    failure that is really about the ACCOUNT — a rejected key, an exhausted plan,
    a rate limit — is named as such, so it is neither retried (that is how a
    credit balance disappears into an outage) nor reported to the research model
    as an unreadable page.
    """

    name = "firecrawl"

    def __init__(
        self,
        api_key: str,
        base_url: str = _FIRECRAWL_DEFAULT_BASE,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._key = api_key
        self._base = _firecrawl_base(base_url)
        self._transport = transport

    async def extract(self, url: str) -> ExtractedDoc:
        docs = await self.extract_many([url])
        return docs[0]

    async def extract_many(self, urls: list[str]) -> list[ExtractedDoc]:
        if not urls:
            return []
        if not self._key:
            return [self._failed(url, _no_key_marker(self.name)) for url in urls]
        docs = await extract_with_isolation(urls, fetch=self._fetch, failed=self._failed)
        return [docs.get(url) or self._failed(url, "no result returned") for url in urls]

    async def _fetch(self, urls: list[str]) -> dict[str, ExtractedDoc]:
        """Scrape a batch as the per-URL requests Firecrawl actually takes.

        Each URL owns its own request and its own failure, so this never raises
        and the isolation helper never needs to split a batch — the batch was
        never shared in the first place.
        """
        gate = asyncio.Semaphore(_FIRECRAWL_CONCURRENCY)

        async def one(url: str) -> tuple[str, ExtractedDoc]:
            async with gate:
                return url, await self._scrape(url)

        return dict(await asyncio.gather(*(one(url) for url in urls)))

    async def _scrape(self, url: str) -> ExtractedDoc:
        try:
            async with httpx.AsyncClient(
                timeout=_TIMEOUT,
                transport=self._transport,
                trust_env=False,
                follow_redirects=False,
            ) as c:
                r = await c.post(
                    f"{self._base}/v2/scrape",
                    headers={"Authorization": f"Bearer {self._key}"},
                    json={"url": url, "formats": ["markdown"]},
                )
                if r.status_code >= 400:
                    return self._failed(url, _provider_marker(self.name, r.status_code))
                payload = r.json()
        except (httpx.HTTPError, EgressDenied, ValueError) as exc:
            marker = _provider_marker(self.name, _status_code(exc), type(exc).__name__)
            return self._failed(url, marker)
        data = payload.get("data") if isinstance(payload, dict) else None
        return self._to_doc(url, data if isinstance(data, dict) else {})

    def _failed(self, url: str, error: str) -> ExtractedDoc:
        return ExtractedDoc(
            url=url, title="", content="", fetched_ok=False, error=error, status="error"
        )

    def _to_doc(self, url: str, data: dict) -> ExtractedDoc:
        meta = data.get("metadata", {}) or {}
        raw_title = meta.get("title") or ""
        # V2's metadata title is `string | string[]`.
        title = str(raw_title[0] if isinstance(raw_title, list) and raw_title else raw_title) or url
        content = data.get("markdown", "") or ""
        passages = chunk_passages(url, title, content)
        if not passages:
            return ExtractedDoc(
                url=url,
                title=title,
                content=content,
                fetched_ok=False,
                error="no readable content",
                status="error",
            )
        return reject_challenge_page(
            ExtractedDoc(url=url, title=title, content=content, passages=passages, fetched_ok=True)
        )
