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

import asyncio
import hashlib
import os
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
from disco.core.host_egress import EgressDenied, validate_untrusted_url

from .local_encoders import EncoderUnavailable
from .models import ExtractedDoc, Passage, SearchHit
from .nli import Entailment
from .providers import SearchProvider

# W-33: cap how long a remote-encoder connectivity probe waits. The probe only
# needs to confirm the host ANSWERS (any HTTP status counts) — a dead endpoint
# must fail FAST so Deep Research surfaces a named error instead of hanging.
_PROBE_TIMEOUT_S = 5.0
# P1-4: a HARD outer deadline on every probe (slightly above the httpx client
# timeout so httpx's own connect/read error — which is more descriptive — wins
# normally, while this asyncio.wait_for backstop guarantees the probe can NEVER
# hang the event loop even if the transport ignores its timeout).
_PROBE_DEADLINE_S = _PROBE_TIMEOUT_S + 2.0

# P1-4: httpx splits its failures across two unrelated bases — httpx.HTTPError
# (connect/read/timeout/transport/status) and httpx.InvalidURL (a malformed URL,
# e.g. an invalid port, raised when the request is built). A probe must convert
# BOTH to a named EncoderUnavailable, never let one escape uncaught.
_PROBE_HTTP_ERRORS = (httpx.HTTPError, httpx.InvalidURL)

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
        time_filter: str | None = None,
    ) -> list[SearchHit]:
        # DR-3 E1: SearXNG uses `time_range` param; accept "month"/"week".
        # "day" is avoided per spec (near-zero results).
        params: dict = {"q": query, "format": "json"}
        if time_filter in {"month", "week"}:
            params["time_range"] = time_filter
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                resp = await client.get(
                    f"{self._base}/search", params=params
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
        for url in urls:
            try:
                validate_untrusted_url(url)
            except EgressDenied as exc:
                return [self._failed(u, i, str(exc)) for i, u in enumerate(urls)]
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
                trust_env=False,
                follow_redirects=False,
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

    async def probe(self) -> None:
        """W-33 pre-flight: confirm this reranker endpoint is configured AND
        answering, BEFORE a Deep Research run starts. Raises EncoderUnavailable
        with a VERBOSE, named reason on an empty URL or a connect/timeout error.
        A non-2xx HTTP status still counts as REACHABLE (the service answered) —
        we only fail on transport-level failures."""
        if not self._base:
            raise EncoderUnavailable(
                "Deep Research needs the reranker, but it isn't connected "
                "(reranker_url is empty). Set Settings -> Encoders -> Reranker URL, "
                "or switch encoders to in-process (local)."
            )
        try:
            async with httpx.AsyncClient(
                timeout=min(self._timeout, _PROBE_TIMEOUT_S),
                transport=self._transport,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                await asyncio.wait_for(client.get(self._base), _PROBE_DEADLINE_S)
        except _PROBE_HTTP_ERRORS as exc:
            raise EncoderUnavailable(
                f"Deep Research needs the reranker, but it isn't reachable at "
                f"{self._base} ({type(exc).__name__}: {exc}). Check the reranker "
                "service, or switch encoders to in-process (local)."
            ) from exc
        except TimeoutError as exc:
            raise EncoderUnavailable(
                f"Deep Research needs the reranker, but it didn't answer within "
                f"{_PROBE_DEADLINE_S:.0f}s at {self._base}. Check the reranker "
                "service, or switch encoders to in-process (local)."
            ) from exc

    async def rerank(self, query: str, passages: list[Passage], *, top_k: int) -> list[Passage]:
        if not passages:
            return []
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
                trust_env=False,
                follow_redirects=False,
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

    async def probe(self) -> None:
        """W-33 pre-flight: confirm this embedder endpoint is configured AND
        answering. Raises EncoderUnavailable (verbose, named) on an empty URL or
        a connect/timeout failure; a non-2xx HTTP status counts as reachable."""
        if not self._base:
            raise EncoderUnavailable(
                "Deep Research needs the embedder, but it isn't connected "
                "(embedder_url is empty). Set Settings -> Encoders -> Embedder URL, "
                "or switch encoders to in-process (local)."
            )
        try:
            async with httpx.AsyncClient(
                timeout=min(self._timeout, _PROBE_TIMEOUT_S),
                transport=self._transport,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                await asyncio.wait_for(client.get(self._base), _PROBE_DEADLINE_S)
        except _PROBE_HTTP_ERRORS as exc:
            raise EncoderUnavailable(
                f"Deep Research needs the embedder, but it isn't reachable at "
                f"{self._base} ({type(exc).__name__}: {exc}). Check the embedder "
                "service, or switch encoders to in-process (local)."
            ) from exc
        except TimeoutError as exc:
            raise EncoderUnavailable(
                f"Deep Research needs the embedder, but it didn't answer within "
                f"{_PROBE_DEADLINE_S:.0f}s at {self._base}. Check the embedder "
                "service, or switch encoders to in-process (local)."
            ) from exc

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        headers = {"content-type": "application/json"}
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"
        async with httpx.AsyncClient(
            timeout=self._timeout,
            transport=self._transport,
            trust_env=False,
            follow_redirects=False,
        ) as client:
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

    async def probe(self) -> None:
        """W-33 pre-flight: confirm this NLI verifier endpoint is configured AND
        answering. Raises EncoderUnavailable (verbose, named) on an empty URL or
        a connect/timeout failure; a non-2xx HTTP status counts as reachable.
        The client transport is SYNC, so the GET runs off the event loop."""
        if not self._base:
            raise EncoderUnavailable(
                "Deep Research needs the verifier (NLI), but it isn't connected "
                "(nli_url is empty). Set Settings -> Encoders -> NLI URL, "
                "or switch encoders to in-process (local)."
            )

        def _ping() -> None:
            with httpx.Client(
                timeout=min(self._timeout, _PROBE_TIMEOUT_S),
                transport=self._transport,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                client.get(self._base)

        try:
            # P1-4: the sync GET runs off the event loop via to_thread; wrap it in
            # an OUTER asyncio deadline so a hung sidecar (a transport that ignores
            # its own timeout) can never block Deep Research indefinitely.
            await asyncio.wait_for(asyncio.to_thread(_ping), _PROBE_DEADLINE_S)
        except _PROBE_HTTP_ERRORS as exc:
            raise EncoderUnavailable(
                f"Deep Research needs the verifier (NLI), but it isn't reachable at "
                f"{self._base} ({type(exc).__name__}: {exc}). Check the NLI sidecar, "
                "or switch encoders to in-process (local)."
            ) from exc
        except TimeoutError as exc:
            raise EncoderUnavailable(
                f"Deep Research needs the verifier (NLI), but it didn't answer "
                f"within {_PROBE_DEADLINE_S:.0f}s at {self._base}. Check the NLI "
                "sidecar, or switch encoders to in-process (local)."
            ) from exc

    def _verify(self, premise: str, hypothesis: str) -> dict:
        key = (premise, hypothesis)
        if key in self._cache:
            return self._cache[key]
        try:
            with httpx.Client(
                timeout=self._timeout,
                transport=self._transport,
                trust_env=False,
                follow_redirects=False,
            ) as client:
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
    "DISCO_SEARXNG_URL": "http://192.168.1.202:8888",
    "DISCO_CRAWL4AI_URL": "http://192.168.1.237:11235",
    "DISCO_RERANKER_URL": "http://192.168.1.81:8091",
    "DISCO_EMBEDDER_URL": "http://192.168.1.81:8090/v1",
    "DISCO_NLI_URL": "http://192.168.1.81:8092",
}


def _make_search(provider: str, base_url: str, api_key: str):
    """Select the discovery provider (§B2). BUNDLED `ddgs` is the default — no key,
    no service. `searxng` self-hosts; `tavily` is a paid key."""
    from .bundled_providers import BraveSearchProvider, DdgsSearchProvider, TavilySearchProvider
    from .source_adapters import (
        ArxivSearchProvider,
        NewsSearchProvider,
        SemanticScholarSearchProvider,
        SiteScopedSearchProvider,
    )

    if provider == "searxng":
        return SearxngSearchProvider(base_url)
    if provider == "tavily":
        return TavilySearchProvider(api_key)
    if provider == "brave":
        return BraveSearchProvider(api_key, base_url=base_url or "https://api.search.brave.com")
    if provider == "arxiv":
        return ArxivSearchProvider(base_url=base_url) if base_url else ArxivSearchProvider()
    if provider == "news":
        return NewsSearchProvider(base_url=base_url) if base_url else NewsSearchProvider()
    if provider == "semantic_scholar":
        return (
            SemanticScholarSearchProvider(base_url=base_url, api_key=api_key)
            if base_url
            else SemanticScholarSearchProvider(api_key=api_key)
        )
    if provider == "site_scoped":
        return SiteScopedSearchProvider(sites=base_url)
    return DdgsSearchProvider()  # default / "ddgs"


def build_multi_search(
    sources: Sequence[str],
    *,
    searxng_url: str = "",
    tavily_key: str = "",
    ss_key: str = "",
    brave_key: str = "",
    brave_url: str = "",
    site_scoped_sites: str = "",
) -> SearchProvider:
    """Compose a MultiSearchProvider from a per-query source id list."""
    from .bundled_providers import DdgsSearchProvider
    from .source_adapters import MultiSearchProvider

    providers: list[SearchProvider] = []
    seen: set[str] = set()
    for raw in sources:
        source_id = str(raw).strip().lower()
        if not source_id or source_id in seen:
            continue
        seen.add(source_id)
        if source_id == "ddgs":
            providers.append(_make_search("ddgs", "", ""))
        elif source_id == "arxiv":
            providers.append(_make_search("arxiv", "", ""))
        elif source_id == "news":
            providers.append(_make_search("news", "", ""))
        elif source_id == "semantic_scholar":
            providers.append(_make_search("semantic_scholar", "", ss_key))
        elif source_id == "searxng" and searxng_url:
            providers.append(_make_search("searxng", searxng_url, ""))
        elif source_id == "tavily" and tavily_key:
            providers.append(_make_search("tavily", "", tavily_key))
        elif source_id == "brave" and brave_key:
            providers.append(_make_search("brave", brave_url, brave_key))
        elif source_id == "site_scoped" and site_scoped_sites:
            providers.append(_make_search("site_scoped", site_scoped_sites, ""))
    if not providers:
        return DdgsSearchProvider()
    if len(providers) == 1:
        return providers[0]
    return MultiSearchProvider(tuple(providers))


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
    search_secret_ref: str = "",
    search_override: SearchProvider | None = None,
    extraction_provider: str = "local",
    extraction_base_url: str = "",
    extraction_api_key: str = "",
    extraction_secret_ref: str = "",
    trusted_origins: tuple[str, ...] = (),
    origin_approved: Callable[[str, str, str | None], bool] | None = None,
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
    approved = origin_approved or (lambda *_: False)

    def url(key: str) -> str:
        # DISCO_<X> preferred; legacy PMX_<X> honored; else the LAN default.
        v = e.get(key)
        if v is None and key.startswith("DISCO_"):
            v = e.get("PMX_" + key[len("DISCO_") :])
        return v if v is not None else _DEFAULTS[key]

    if remote is None:
        encoders = e.get("DISCO_ENCODERS")
        if encoders is None:
            encoders = e.get("PMX_ENCODERS", "local")
        remote = encoders.lower() == "remote"
    use_remote = remote
    # B1/B2 — pluggable discovery + extraction. Bundled (ddgs/local) by default so a
    # fresh install works keyless; searxng/crawl4ai self-host (base_url, empty → env
    # default); tavily/firecrawl are paid (resolved api_key passed in by the runtime).
    search_url = search_base_url or (
        url("DISCO_SEARXNG_URL") if search_provider == "searxng" else ""
    )
    search_trust_url = {
        "tavily": "https://api.tavily.com",
        "brave": search_url or "https://api.search.brave.com",
        "semantic_scholar": search_url or "https://api.semanticscholar.org",
    }.get(search_provider, search_url)
    if search_trust_url and not approved(
        search_trust_url, f"search:{search_provider}", search_secret_ref
    ):
        search_provider, search_url, search_api_key = "ddgs", "", ""
    if extraction_provider == "crawl4ai":
        extraction_url = extraction_base_url or url("DISCO_CRAWL4AI_URL")
    elif extraction_provider == "firecrawl":
        extraction_url = extraction_base_url or "https://api.firecrawl.dev"
    else:
        extraction_url = extraction_base_url
    if extraction_provider != "local" and not approved(
        extraction_url,
        f"extraction:{extraction_provider}",
        extraction_secret_ref,
    ):
        extraction_provider, extraction_url, extraction_api_key = "local", "", ""
    providers: dict[str, Any] = {
        "search": search_override or _make_search(search_provider, search_url, search_api_key),
        "extraction": _make_extraction(extraction_provider, extraction_url, extraction_api_key),
    }
    r_url = reranker_url or url("DISCO_RERANKER_URL")
    e_url = embedder_url or url("DISCO_EMBEDDER_URL")
    n_url = nli_url or url("DISCO_NLI_URL")
    if use_remote:
        # config override (non-empty) wins; else the env/default for that endpoint
        use_remote = (
            approved(r_url, "encoder:reranker", "")
            and approved(e_url, "encoder:embedder", "")
            and approved(n_url, "encoder:nli", "")
        )
    if use_remote:
        providers["reranker"] = TeiReranker(r_url)
        providers["embedder"] = OpenAIEmbedder(e_url)
        providers["nli"] = SidecarNLIVerifier(n_url)
    else:
        # In-process ONNX/CPU encoders (imported lazily — models load on first use).
        from .local_encoders import FastEmbedEmbedder, FastEmbedNLIVerifier, FastEmbedReranker

        providers["reranker"] = FastEmbedReranker()
        providers["embedder"] = FastEmbedEmbedder()
        providers["nli"] = FastEmbedNLIVerifier()
    return providers
