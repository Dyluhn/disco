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
from html.parser import HTMLParser

import httpx

from .models import ExtractedDoc, Passage, SearchHit

_LOG = logging.getLogger("perpleximanus.retrieval.providers")

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

_UA = "Mozilla/5.0 (compatible; perpleximanus/1.0; +https://perpleximanus.local)"
_TIMEOUT = httpx.Timeout(20.0)


# ===== SEARCH ================================================================


class DdgsSearchProvider:
    """(c) BUNDLED search — DuckDuckGo via the maintained `ddgs` lib, in-process, no
    key. Moderate rate limits (fine for personal use); on a rate-limit it returns an
    empty list rather than crashing the run. The first-run default."""

    name = "ddgs"

    async def search(
        self,
        query: str,
        *,
        limit: int = 10,
        domains_allow: frozenset[str] | None = None,
        domains_deny: frozenset[str] | None = None,
    ) -> list[SearchHit]:
        rows = await asyncio.to_thread(self._blocking_search, query, limit)
        _LOG.info("ddgs search %r → %d raw rows", query[:80], len(rows))
        hits: list[SearchHit] = []
        for i, r in enumerate(rows):
            url = r.get("href") or r.get("url") or ""
            if not url:
                continue
            if domains_deny and any(d in url for d in domains_deny):
                continue
            hits.append(
                SearchHit(
                    url=url,
                    title=r.get("title", "") or url,
                    snippet=r.get("body", "") or r.get("snippet", ""),
                    source_engine="ddgs",
                    rank=i,
                )
            )
        return hits

    def _blocking_search(self, query: str, limit: int) -> list[dict]:
        try:
            from ddgs import DDGS  # lazy: keeps import cost off the hot path

            with DDGS() as d:
                return list(d.text(query, max_results=limit))
        except Exception:  # noqa: BLE001 — rate-limit / network: degrade, don't crash
            return []


class TavilySearchProvider:
    """(b) PAID search — Tavily (BYO key). A clean answer-oriented search API."""

    name = "tavily"

    def __init__(self, api_key: str):
        self._key = api_key

    async def search(
        self,
        query: str,
        *,
        limit: int = 10,
        domains_allow: frozenset[str] | None = None,
        domains_deny: frozenset[str] | None = None,
    ) -> list[SearchHit]:
        if not self._key:
            return []
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(
                "https://api.tavily.com/search",
                json={"api_key": self._key, "query": query, "max_results": limit},
            )
            r.raise_for_status()
            data = r.json()
        return [
            SearchHit(
                url=h.get("url", ""),
                title=h.get("title", "") or h.get("url", ""),
                snippet=h.get("content", ""),
                source_engine="tavily",
                rank=i,
            )
            for i, h in enumerate(data.get("results", []))
            if h.get("url")
        ]


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
            async with httpx.AsyncClient(
                timeout=_TIMEOUT, follow_redirects=True, headers={"User-Agent": _UA}
            ) as c:
                r = await c.get(url)
                r.raise_for_status()
                html = r.text
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            status = "not_found" if code == 404 else "blocked" if code in (401, 403) else "error"
            _LOG.info("local extract FAIL %s → HTTP %s (%s)", url[:80], code, status)
            return ExtractedDoc(
                url=url, title="", content="", fetched_ok=False, error=str(e), status=status
            )
        except httpx.HTTPError as e:
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
                url=url, title=title, content=md, fetched_ok=False,
                error="no readable content", status="error",
            )
        _LOG.info("local extract %s → %d chars, %d passages", url[:80], len(md), len(passages))
        return ExtractedDoc(url=url, title=title, content=md, passages=passages, fetched_ok=True)

    async def extract_many(self, urls: list[str]) -> list[ExtractedDoc]:
        return list(await asyncio.gather(*(self.extract(u) for u in urls)))


class FirecrawlExtractionProvider:
    """(b) PAID extraction — Firecrawl /v1/scrape (BYO key), returns clean markdown."""

    name = "firecrawl"

    def __init__(self, api_key: str, base_url: str = "https://api.firecrawl.dev"):
        self._key = api_key
        self._base = base_url.rstrip("/") or "https://api.firecrawl.dev"

    async def extract(self, url: str) -> ExtractedDoc:
        if not self._key:
            return ExtractedDoc(
                url=url, title="", content="", fetched_ok=False,
                error="no firecrawl key configured", status="error",
            )
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
                r = await c.post(
                    f"{self._base}/v1/scrape",
                    headers={"Authorization": f"Bearer {self._key}"},
                    json={"url": url, "formats": ["markdown"]},
                )
                r.raise_for_status()
                data = r.json().get("data", {})
        except httpx.HTTPError as e:
            return ExtractedDoc(
                url=url, title="", content="", fetched_ok=False, error=str(e), status="error"
            )
        meta = data.get("metadata", {}) or {}
        title = meta.get("title", "") or url
        content = data.get("markdown", "") or ""
        passages = chunk_passages(url, title, content)
        if not passages:
            return ExtractedDoc(
                url=url, title=title, content=content, fetched_ok=False,
                error="no readable content", status="error",
            )
        return ExtractedDoc(
            url=url, title=title, content=content, passages=passages, fetched_ok=True
        )

    async def extract_many(self, urls: list[str]) -> list[ExtractedDoc]:
        return list(await asyncio.gather(*(self.extract(u) for u in urls)))
