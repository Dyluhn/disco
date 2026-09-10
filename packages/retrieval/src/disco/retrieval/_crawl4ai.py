"""Crawl4AI extraction provider used by the live retrieval wiring."""

from __future__ import annotations

import hashlib
from typing import Literal

import httpx

from ._extraction_fallbacks import arxiv_id, extract_arxiv_abstracts, recover_blocked_pages
from ._extraction_text import CORRUPTED_TEXT_ERROR, corrupted_text, reject_challenge_page
from ._pdf_recovery import recover_pdf_sources
from ._retrieval_cache import RetrievalCache, cached_docs, store_docs
from ._transport_retry import extract_with_isolation
from .models import ExtractedDoc, Passage

ExtractStatus = Literal["ok", "paywalled", "blocked", "not_found", "error"]

# Crawl4AI's default ``fit_markdown`` is empty on many large pages. Asking for
# a pruning filter makes the server populate it with article content instead of
# navigation chrome.
_CRAWL_CONFIG = {
    "type": "CrawlerRunConfig",
    "params": {
        "markdown_generator": {
            "type": "DefaultMarkdownGenerator",
            "params": {
                "content_filter": {
                    "type": "PruningContentFilter",
                    "params": {"threshold": 0.45, "threshold_type": "dynamic"},
                }
            },
        }
    },
}


class Crawl4aiExtractionProvider:
    """[ExtractionProvider] Crawl4AI synchronous /crawl provider."""

    name = "crawl4ai"

    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 90.0,
        max_chars: int | None = None,
        passage_chars: int = 1_100,
        max_passages: int = 12,
        transport: httpx.AsyncBaseTransport | None = None,
        cache: RetrievalCache | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout_s
        self._max_chars = max_chars
        self._passage_chars = passage_chars
        self._max_passages = max_passages
        self._transport = transport
        self._cache = cache
        # Retention and chunk settings are part of extraction identity. Old
        # URL-only entries may contain a silently truncated 16k-character body.
        self._cache_namespace = (
            f"crawl4ai-full-v2:{self._base}:{max_chars}:{passage_chars}:{max_passages}:"
        )

    async def extract(self, url: str) -> ExtractedDoc:
        docs = await self.extract_many([url])
        return docs[0]

    async def extract_many(self, urls: list[str]) -> list[ExtractedDoc]:
        """Extract with cached sources, native abstracts, crawler, PDF and archive recovery."""
        if not urls:
            return []
        cached = cached_docs(self._cache, urls, namespace=self._cache_namespace)
        pending = [url for url in urls if url not in cached]
        docs = dict(cached)
        docs |= await extract_arxiv_abstracts(
            [url for url in pending if arxiv_id(url)],
            timeout_s=self._timeout,
            transport=self._transport,
            max_chars=self._max_chars,
            chunk=self._chunk,
            failed=self._failed,
        )
        crawl = [url for url in pending if not arxiv_id(url)]
        if crawl:
            from .bundled_providers import LocalExtractionProvider

            crawled = await extract_with_isolation(crawl, fetch=self._fetch, failed=self._failed)
            crawled |= await recover_pdf_sources(
                crawled, timeout_s=self._timeout, max_chars=self._max_chars, chunk=self._chunk
            )
            recovered = await recover_blocked_pages(
                crawled, fetch_archive=LocalExtractionProvider().extract, chunk=self._chunk
            )
            docs |= crawled | recovered
        out = [docs.get(url) or self._failed(url, "no result returned") for url in urls]
        fresh = frozenset(pending)
        store_docs(
            self._cache,
            [doc for doc in out if doc.url in fresh],
            namespace=self._cache_namespace,
        )
        return out

    async def _fetch(self, urls: list[str]) -> dict[str, ExtractedDoc]:
        async with httpx.AsyncClient(
            timeout=self._timeout,
            transport=self._transport,
            trust_env=False,
            follow_redirects=True,
        ) as client:
            resp = await client.post(
                f"{self._base}/crawl",
                json={"urls": urls, "crawler_config": _CRAWL_CONFIG},
            )
            resp.raise_for_status()
            payload = resp.json().get("results", [])
        results = payload if isinstance(payload, list) else []
        by_url = {r.get("url"): r for r in results}
        docs = {
            url: self._to_doc(
                url, by_url.get(url) or (results[idx] if idx < len(results) else None)
            )
            for idx, url in enumerate(urls)
        }
        from .bundled_providers import LocalExtractionProvider

        for url, doc in docs.items():
            if doc.error == CORRUPTED_TEXT_ERROR:
                # A successful crawler response can still contain undecoded binary.
                # Re-read the original once through the guarded local extractor.
                recovered = await LocalExtractionProvider().extract(url)
                if recovered.fetched_ok:
                    content = recovered.content[: self._max_chars]
                    recovered = recovered.model_copy(
                        update={
                            "content": content,
                            "passages": self._chunk(url, recovered.title, content),
                        }
                    )
                docs[url] = recovered
        return docs

    def _failed(self, url: str, error: str) -> ExtractedDoc:
        return ExtractedDoc(
            url=url, title=url, content="", fetched_ok=False, error=error, status="error"
        )

    def _status(self, success: bool, code: int | None) -> ExtractStatus:
        if success and (code is None or 200 <= code < 300):
            return "ok"
        if code in (401, 403):
            return "blocked"
        if code == 402:
            return "paywalled"
        if code == 404:
            return "not_found"
        return "error"

    def _to_doc(self, url: str, res: dict | None) -> ExtractedDoc:
        if res is None:
            return self._failed(url, "no result returned")
        code = res.get("redirected_status_code") or res.get("status_code")
        status = self._status(bool(res.get("success")), code)
        md = res.get("markdown")
        content = ""
        if isinstance(md, dict):
            content = md.get("fit_markdown") or md.get("raw_markdown") or ""
        elif isinstance(md, str):
            content = md
        content = content.strip()[: self._max_chars]
        title = (res.get("metadata") or {}).get("title") or url
        if corrupted_text(content):
            return self._failed(url, CORRUPTED_TEXT_ERROR)
        if status != "ok" or not content:
            return ExtractedDoc(
                url=url,
                title=title,
                content=content,
                fetched_ok=False,
                error=res.get("error_message") or (f"http {code}" if code else "empty content"),
                status="error" if status == "ok" else status,
            )
        return reject_challenge_page(
            ExtractedDoc(
                url=url,
                title=title,
                content=content,
                passages=self._chunk(url, title, content),
                fetched_ok=True,
                status="ok",
            )
        )

    def _chunk(self, url: str, title: str, content: str) -> list[Passage]:
        sid = hashlib.md5(url.encode()).hexdigest()[:6]  # noqa: S324 — non-security id
        paras = [p.strip() for p in content.split("\n\n") if len(p.strip()) >= 40]
        passages: list[Passage] = []
        buf = ""
        for para in paras:
            if buf and len(buf) + len(para) > self._passage_chars:
                passages.append(self._passage(sid, len(passages), url, title, buf))
                buf = ""
            buf += para + "\n\n"
            if len(passages) >= self._max_passages:
                break
        if buf.strip() and len(passages) < self._max_passages:
            passages.append(self._passage(sid, len(passages), url, title, buf))
        return passages

    @staticmethod
    def _passage(sid: str, i: int, url: str, title: str, text: str) -> Passage:
        return Passage(id=f"{sid}_p{i}", source_url=url, source_title=title, text=text.strip())
