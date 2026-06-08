"""Live HTTP providers — the real retrieval/grounding backends (Stage 3).

Thin async HTTP clients that FULFILL the existing protocols (providers.py,
ranking.py, the NLI shape) — the interfaces don't change, the stubs are replaced:

  SearxngSearchProvider   -> SearchProvider      (SearXNG JSON)
  Crawl4aiExtractionProvider -> ExtractionProvider (Crawl4AI /crawl -> markdown)
  TeiReranker             -> Reranker             (TEI /rerank, cross-encoder)
  OpenAIEmbedder          -> Embedder             (bge-m3, OpenAI /v1/embeddings)
  SidecarNLIVerifier      -> _NLIVerifierLike      (NLI sidecar /verify)

Providers degrade gracefully: a failed search/rerank/embed returns a sane
fallback (empty / input order) rather than crashing the pipeline; a failed
extract becomes an ExtractedDoc with status != "ok" so the honest-failure
rendering (§2.2) shows it. Base URLs are injected (env-configured at wiring time).
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from typing import Any, Literal
from urllib.parse import urlparse

import httpx

from .models import ExtractedDoc, Passage, SearchHit
from .nli import Entailment

ExtractStatus = Literal["ok", "paywalled", "blocked", "not_found", "error"]


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


# ---- SearXNG: discovery (query -> candidate URLs) ---------------------------


class SearxngSearchProvider:
    """[ModelProvider — SearchProvider] SearXNG JSON. Discovery only: maps each
    result to a SearchHit (the `content` snippet is provider text, NOT trusted as
    content — extraction produces the citable passages)."""

    name = "searxng"

    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout_s
        self._transport = transport

    async def search(
        self,
        query: str,
        *,
        limit: int = 10,
        domains_allow: frozenset[str] | None = None,
        domains_deny: frozenset[str] | None = None,
    ) -> list[SearchHit]:
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                resp = await client.get(
                    f"{self._base}/search", params={"q": query, "format": "json"}
                )
                resp.raise_for_status()
                results = resp.json().get("results", [])
        except (httpx.HTTPError, ValueError):
            return []  # discovery failure degrades to no hits (pipeline still runs)

        deny = {d.lower() for d in (domains_deny or frozenset())}
        allow = {d.lower() for d in domains_allow} if domains_allow else None
        hits: list[SearchHit] = []
        for i, res in enumerate(results):
            url = res.get("url") or ""
            if not url:
                continue
            host = _host(url)
            if any(d in host for d in deny):
                continue
            if allow is not None and not any(d in host for d in allow):
                continue
            hits.append(
                SearchHit(
                    url=url,
                    title=res.get("title") or url,
                    snippet=res.get("content") or "",
                    source_engine=res.get("engine") or "searxng",
                    rank=i,
                )
            )
            if len(hits) >= limit:
                break
        return hits


# ---- Crawl4AI: extraction (URL -> clean content + passages) -----------------

# Crawl4AI's default `fit_markdown` is empty on many large pages (e.g. Wikipedia),
# leaving only `raw_markdown` — which opens with kilobytes of nav/menu/language-link
# chrome before any article prose. Asking for a PruningContentFilter makes the
# server populate `fit_markdown` with the actual content (verified: on the WP
# "Nineteen Eighty-Four" page this moves the body from char ~9k to char ~150).
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
    """[ExtractionProvider] Crawl4AI synchronous /crawl. Maps the returned
    `markdown.fit_markdown` to clean content and chunks it into citable Passages.
    Non-ok fetches become ExtractedDoc with the right status (§2.2)."""

    name = "crawl4ai"

    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 90.0,
        max_chars: int = 16_000,
        passage_chars: int = 1_100,
        max_passages: int = 12,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout_s
        self._max_chars = max_chars
        self._passage_chars = passage_chars
        self._max_passages = max_passages
        self._transport = transport

    async def extract(self, url: str) -> ExtractedDoc:
        docs = await self.extract_many([url])
        return docs[0]

    async def extract_many(self, urls: list[str]) -> list[ExtractedDoc]:
        if not urls:
            return []
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                resp = await client.post(
                    f"{self._base}/crawl",
                    json={"urls": urls, "crawler_config": _CRAWL_CONFIG},
                )
                resp.raise_for_status()
                results = resp.json().get("results", [])
        except (httpx.HTTPError, ValueError) as exc:
            return [self._failed(u, idx, str(exc)) for idx, u in enumerate(urls)]

        by_url = {r.get("url"): r for r in results}
        out: list[ExtractedDoc] = []
        for idx, url in enumerate(urls):
            res = by_url.get(url) or (results[idx] if idx < len(results) else None)
            out.append(self._to_doc(url, idx, res))
        return out

    def _failed(self, url: str, idx: int, error: str) -> ExtractedDoc:
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

    def _to_doc(self, url: str, idx: int, res: dict | None) -> ExtractedDoc:
        if res is None:
            return self._failed(url, idx, "no result returned")
        code = res.get("status_code")
        status = self._status(bool(res.get("success")), code)
        md = res.get("markdown")
        content = ""
        if isinstance(md, dict):
            content = md.get("fit_markdown") or md.get("raw_markdown") or ""
        elif isinstance(md, str):
            content = md
        content = content.strip()[: self._max_chars]
        title = (res.get("metadata") or {}).get("title") or url

        if status != "ok" or not content:
            return ExtractedDoc(
                url=url,
                title=title,
                content=content,
                fetched_ok=False,
                error=res.get("error_message") or (f"http {code}" if code else "empty content"),
                status="error" if status == "ok" else status,  # empty content => error
            )
        return ExtractedDoc(
            url=url,
            title=title,
            content=content,
            passages=self._chunk(url, title, content),
            fetched_ok=True,
            status="ok",
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


# ---- bge-reranker: cross-encoder rerank (TEI) -------------------------------


class TeiReranker:
    """[Reranker] TEI /rerank cross-encoder (bge-reranker-v2-m3). Returns
    [{index, score}]; we order by score (raw logits — order is what matters) and
    keep top_k. On failure, falls back to the input order (degrade, don't crash)."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout_s
        self._transport = transport

    async def rerank(self, query: str, passages: list[Passage], *, top_k: int) -> list[Passage]:
        if not passages:
            return []
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                resp = await client.post(
                    f"{self._base}/rerank",
                    json={"query": query, "texts": [p.text for p in passages], "raw_scores": False},
                )
                resp.raise_for_status()
                scored = resp.json()
        except (httpx.HTTPError, ValueError):
            return passages[:top_k]  # degrade to input order
        ordered = sorted(scored, key=lambda s: s.get("score", 0.0), reverse=True)
        out = [passages[s["index"]] for s in ordered if 0 <= s.get("index", -1) < len(passages)]
        return out[:top_k] or passages[:top_k]


# ---- bge-m3: embeddings (OpenAI shape) --------------------------------------


class OpenAIEmbedder:
    """[Embedder] bge-m3 via the OpenAI /embeddings shape. Dense vectors for the
    Space-corpus path; the live web flow doesn't require it."""

    def __init__(
        self,
        base_url: str,
        *,
        model: str = "bge-m3",
        api_key: str | None = None,
        timeout_s: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._model = model
        self._key = api_key
        self._timeout = timeout_s
        self._transport = transport

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        headers = {"content-type": "application/json"}
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            resp = await client.post(
                f"{self._base}/embeddings",
                json={"input": texts, "model": self._model},
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
        return [item["embedding"] for item in data.get("data", [])]


# ---- NLI sidecar: claim verification (off the LLM path, §9.2) ---------------

_LABEL: dict[str, Entailment] = {
    "entailment": "entail",
    "neutral": "neutral",
    "contradiction": "contradict",
}


class SidecarNLIVerifier:
    """[NLIVerifier] The NLI cross-encoder sidecar (`POST /verify {premise, claim}
    -> {entailment, label, scores}`). A REAL 3-way NLI: we use the model's argmax
    `label` for the verdict (entailment→entail→supported, neutral→weak,
    contradiction→unsupported) and the `entailment` probability as the score.

    SYNC to fulfill the existing `entail`/`score` interface; results are cached per
    (premise, claim) so the grounding pipeline's entail()+score() = one HTTP call."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 20.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout_s
        self._transport = transport
        self._cache: dict[tuple[str, str], dict] = {}

    def _verify(self, premise: str, hypothesis: str) -> dict:
        key = (premise, hypothesis)
        if key in self._cache:
            return self._cache[key]
        try:
            with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
                resp = client.post(
                    f"{self._base}/verify", json={"premise": premise, "claim": hypothesis}
                )
                resp.raise_for_status()
                data = resp.json()
        except (httpx.HTTPError, ValueError):
            data = {}  # verification failure -> neutral (weak), never crash
        self._cache[key] = data
        return data

    def score(self, premise: str, hypothesis: str) -> float:
        return float(self._verify(premise, hypothesis).get("entailment", 0.0) or 0.0)

    def entail(self, premise: str, hypothesis: str) -> Entailment:
        return _LABEL.get(self._verify(premise, hypothesis).get("label", ""), "neutral")


# ---- wiring factory ---------------------------------------------------------

# [VERIFY] LAN endpoints (overridable by env). Discovered at build; see
# api-endpoints.md / the live-wiring build.
_DEFAULTS = {
    "PMX_SEARXNG_URL": "http://192.168.1.202:8888",
    "PMX_CRAWL4AI_URL": "http://192.168.1.237:11235",
    "PMX_RERANKER_URL": "http://192.168.1.81:8091",
    "PMX_EMBEDDER_URL": "http://192.168.1.81:8090/v1",
    "PMX_NLI_URL": "http://192.168.1.81:8092",
}


def _make_search(provider: str, base_url: str, api_key: str):
    """Select the discovery provider (§B2). BUNDLED `ddgs` is the default — no key,
    no service. `searxng` self-hosts; `tavily` is a paid key."""
    from .bundled_providers import DdgsSearchProvider, TavilySearchProvider

    if provider == "searxng":
        return SearxngSearchProvider(base_url)
    if provider == "tavily":
        return TavilySearchProvider(api_key)
    return DdgsSearchProvider()  # default / "ddgs"


def _make_extraction(provider: str, base_url: str, api_key: str):
    """Select the extraction provider (§B1). BUNDLED `local` is the default — in-
    process, no service. `crawl4ai` self-hosts; `firecrawl` is a paid key."""
    from .bundled_providers import FirecrawlExtractionProvider, LocalExtractionProvider

    if provider == "crawl4ai":
        return Crawl4aiExtractionProvider(base_url)
    if provider == "firecrawl":
        return FirecrawlExtractionProvider(api_key, base_url or "https://api.firecrawl.dev")
    return LocalExtractionProvider()  # default / "local"


def build_live_retrieval(
    env: Mapping[str, str] | None = None,
    *,
    remote: bool | None = None,
    reranker_url: str = "",
    embedder_url: str = "",
    nli_url: str = "",
    search_provider: str = "ddgs",
    search_base_url: str = "",
    search_api_key: str = "",
    extraction_provider: str = "local",
    extraction_base_url: str = "",
    extraction_api_key: str = "",
) -> dict[str, Any]:
    """Construct the five providers. Returns a dict {search, extraction, reranker,
    embedder, nli} the research wiring composes into a RetrievalEngine +
    GroundingPipeline.

    ENCODERS — local by default (portability). Search + extraction are inherently
    remote (SearXNG / Crawl4AI are web services), but the three ENCODERS (reranker,
    embedder, NLI) are bundled IN-PROCESS via fastembed (ONNX/CPU) by default — no
    separate encoder server needed, and faster than the network hop. `remote=True`
    (the persisted Settings toggle) uses the LAN TEI/OpenAI/NLI-sidecar services;
    when `remote` is None it falls back to the `PMX_ENCODERS=remote` env.

    The three `*_url` args are the PERSISTED endpoint overrides (Settings → Encoders
    → Remote). Each takes precedence when non-empty; an empty one falls back to the
    PMX_*_URL env default — so a remote user who hasn't typed URLs still resolves."""
    e = os.environ if env is None else env

    def url(key: str) -> str:
        return e.get(key, _DEFAULTS[key])

    if remote is None:
        remote = e.get("PMX_ENCODERS", "local").lower() == "remote"
    use_remote = remote
    # B1/B2 — pluggable discovery + extraction. Bundled (ddgs/local) by default so a
    # fresh install works keyless; searxng/crawl4ai self-host (base_url, empty → env
    # default); tavily/firecrawl are paid (resolved api_key passed in by the runtime).
    providers: dict[str, Any] = {
        "search": _make_search(
            search_provider, search_base_url or url("PMX_SEARXNG_URL"), search_api_key
        ),
        "extraction": _make_extraction(
            extraction_provider, extraction_base_url or url("PMX_CRAWL4AI_URL"), extraction_api_key
        ),
    }
    if use_remote:
        # config override (non-empty) wins; else the env/default for that endpoint
        providers["reranker"] = TeiReranker(reranker_url or url("PMX_RERANKER_URL"))
        providers["embedder"] = OpenAIEmbedder(embedder_url or url("PMX_EMBEDDER_URL"))
        providers["nli"] = SidecarNLIVerifier(nli_url or url("PMX_NLI_URL"))
    else:
        # In-process ONNX/CPU encoders (imported lazily — models load on first use).
        from .local_encoders import FastEmbedEmbedder, FastEmbedNLIVerifier, FastEmbedReranker

        providers["reranker"] = FastEmbedReranker()
        providers["embedder"] = FastEmbedEmbedder()
        providers["nli"] = FastEmbedNLIVerifier()
    return providers
