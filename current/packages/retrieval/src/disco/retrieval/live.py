"""Live HTTP providers — the real retrieval/grounding backends (Stage 3).

Thin async HTTP clients that FULFILL the existing protocols (providers.py,
ranking.py, the NLI shape) — the interfaces don't change, the stubs are replaced:

  SearxngSearchProvider   -> SearchProvider      (SearXNG JSON)
  Crawl4aiExtractionProvider -> ExtractionProvider (Crawl4AI /crawl -> markdown)
  TeiReranker             -> Reranker             (TEI /rerank, cross-encoder)
  OpenAIEmbedder          -> Embedder             (bge-m3, OpenAI /v1/embeddings)
  SidecarNLIVerifier      -> _NLIVerifierLike      (NLI sidecar /verify)

Per-CALL failures degrade with a logged warning (a failed search returns no
hits for that round; a failed extract becomes an ExtractedDoc with status !=
"ok"), and the run-level zero-evidence gate turns total failure into a run
error. CONFIG failures never degrade: an unapproved or unconstructible
requested provider raises ProviderConfigError at wiring time instead of
silently substituting a bundled provider. Base URLs are injected
(env-configured at wiring time).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal

import httpx
from disco.core.host_egress import EgressDenied, validate_untrusted_url

from .local_encoders import EncoderUnavailable
from .models import ExtractedDoc, Passage, SearchHit
from .nli import Entailment
from .providers import SearchProvider
from .ranking import deduplicate_search_hits
from .url_policy import parse_source_date, url_allowed

_LOG = logging.getLogger(__name__)

# Wiring policy (unapproved/unconstructible requested providers raise) lives
# in `_provider_wiring`; re-exported here as the stable import surface.
from ._provider_wiring import (  # noqa: E402
    ProviderConfigError,
    _resolve_extraction_wiring,
    _resolve_search_wiring,
)

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
        params = self._build_params(query, time_filter)
        results, diagnostic = await self._fetch_results(params)
        return self._to_hits(results, domains_allow, domains_deny, limit), diagnostic

    def _build_params(self, query: str, time_filter: str | None) -> dict:
        # DR-3 E1: SearXNG uses `time_range` param; accept "month"/"week".
        # "day" is avoided per spec (near-zero results).
        params: dict = {"q": query, "format": "json"}
        if time_filter in {"month", "week"}:
            params["time_range"] = time_filter
        return params

    async def _fetch_results(self, params: dict) -> tuple[list, dict[str, object]]:
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                resp = await client.get(f"{self._base}/search", params=params)
                resp.raise_for_status()
                try:
                    payload = resp.json()
                except ValueError as exc:
                    raise _InvalidSearxngResponse(
                        "body was not valid JSON", status_code=resp.status_code
                    ) from exc
                if not isinstance(payload, dict):
                    raise _InvalidSearxngResponse(
                        "payload must be an object", status_code=resp.status_code
                    )
                results = payload.get("results")
                if not isinstance(results, list):
                    raise _InvalidSearxngResponse(
                        "results must be a list", status_code=resp.status_code
                    )
                if any(
                    not isinstance(result, dict)
                    or ("url" in result and not isinstance(result.get("url"), str))
                    for result in results
                ):
                    raise _InvalidSearxngResponse(
                        "result entries have an invalid shape", status_code=resp.status_code
                    )
                unresponsive = payload.get("unresponsive_engines", [])
                diagnostic: dict[str, object] = {
                    "provider": self.name,
                    "outcome": "ok" if results else "empty",
                    "status_code": resp.status_code,
                    "result_count": len(results),
                    "latency_ms": max(0, int((time.perf_counter() - started) * 1_000)),
                }
                if isinstance(unresponsive, list) and unresponsive:
                    diagnostic["unresponsive_engines"] = [
                        str(item)[:80] for item in unresponsive[:20]
                    ]
                return results if isinstance(results, list) else [], diagnostic
        except _InvalidSearxngResponse as exc:
            return [], {
                "provider": self.name,
                "outcome": "invalid_response",
                "provider_error": type(exc).__name__,
                "status_code": exc.status_code,
                "result_count": 0,
                "latency_ms": max(0, int((time.perf_counter() - started) * 1_000)),
            }
        except (httpx.HTTPError, ValueError) as exc:
            # Discovery failure degrades to no hits for THIS round (the run's
            # zero-evidence gate raises later if nothing was ever admitted),
            # but is never silent.
            _LOG.warning("SearXNG search failed: %s: %s", type(exc).__name__, exc)
            status_code = (
                exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            )
            return [], {
                "provider": self.name,
                "outcome": "upstream",
                "provider_error": type(exc).__name__,
                "status_code": status_code,
                "result_count": 0,
                "latency_ms": max(0, int((time.perf_counter() - started) * 1_000)),
            }

    def _to_hits(
        self,
        results: list,
        domains_allow: frozenset[str] | None,
        domains_deny: frozenset[str] | None,
        limit: int,
    ) -> list[SearchHit]:
        hits: list[SearchHit] = []
        for i, res in enumerate(results):
            url = res.get("url") or ""
            if not url:
                continue
            if not url_allowed(url, domains_allow, domains_deny):
                continue
            raw_engines = res.get("engines")
            engines = (
                [str(item).strip() for item in raw_engines if str(item).strip()]
                if isinstance(raw_engines, list)
                else []
            )
            if not engines:
                engines = [str(res.get("engine") or "searxng")]
            hits.append(
                SearchHit(
                    url=url,
                    title=res.get("title") or url,
                    snippet=res.get("content") or "",
                    source_engine="+".join(dict.fromkeys(engines)),
                    rank=i,
                    published_at=parse_source_date(
                        res.get("publishedDate") or res.get("published_date")
                    ),
                )
            )
        return deduplicate_search_hits(hits)[:limit]


class _InvalidSearxngResponse(ValueError):
    """The SearXNG endpoint answered, but not with its JSON result shape."""

    def __init__(self, message: str, *, status_code: int | None) -> None:
        super().__init__(message)
        self.status_code = status_code


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
        return TavilySearchProvider(api_key, base_url=base_url or "https://api.tavily.com")
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


# B2 dispatch table: source id -> (provider name, cfg key for base_url, cfg key
# for api_key, cfg key that gates inclusion). An empty spec slot means "no such
# field" (cfg.get("", "") always resolves to ""); an empty gate key means
# "always included" (bundled sources need no key/url).
_MULTI_SEARCH_SPEC: dict[str, tuple[str, str, str, str]] = {
    "ddgs": ("ddgs", "", "", ""),
    "arxiv": ("arxiv", "", "", ""),
    "news": ("news", "", "", ""),
    "semantic_scholar": ("semantic_scholar", "", "ss_key", ""),
    "searxng": ("searxng", "searxng_url", "", "searxng_url"),
    "tavily": ("tavily", "", "tavily_key", "tavily_key"),
    "brave": ("brave", "brave_url", "brave_key", "brave_key"),
    "site_scoped": ("site_scoped", "site_scoped_sites", "", "site_scoped_sites"),
}


def _multi_search_candidate(source_id: str, cfg: Mapping[str, str]) -> SearchProvider | None:
    spec = _MULTI_SEARCH_SPEC.get(source_id)
    if spec is None:
        return None
    provider, url_key, key_key, gate_key = spec
    if gate_key and not cfg[gate_key]:
        return None
    return _make_search(provider, cfg.get(url_key, ""), cfg.get(key_key, ""))


def _collect_multi_search_providers(
    sources: Sequence[str], cfg: Mapping[str, str]
) -> list[SearchProvider]:
    providers: list[SearchProvider] = []
    seen: set[str] = set()
    for raw in sources:
        source_id = str(raw).strip().lower()
        if not source_id or source_id in seen:
            continue
        seen.add(source_id)
        candidate = _multi_search_candidate(source_id, cfg)
        if candidate is not None:
            providers.append(candidate)
    return providers


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

    cfg = {
        "searxng_url": searxng_url,
        "tavily_key": tavily_key,
        "ss_key": ss_key,
        "brave_key": brave_key,
        "brave_url": brave_url,
        "site_scoped_sites": site_scoped_sites,
    }
    providers = _collect_multi_search_providers(sources, cfg)
    if not providers:
        requested = [str(source).strip().lower() for source in sources if str(source).strip()]
        if requested:
            # The user explicitly chose sources; substituting a different
            # provider behind their back is a config error, not a fallback.
            raise ProviderConfigError(
                f"none of the requested search sources could be constructed: {requested}; "
                "check their configuration or choose different sources"
            )
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


def _env_url(e: Mapping[str, str], key: str) -> str:
    """DISCO_<X> preferred; legacy PMX_<X> honored; else the LAN default."""
    v = e.get(key)
    if v is None and key.startswith("DISCO_"):
        v = e.get("PMX_" + key[len("DISCO_") :])
    return v if v is not None else _DEFAULTS[key]


def _resolve_remote_flag(remote: bool | None, e: Mapping[str, str]) -> bool:
    if remote is not None:
        return remote
    encoders = e.get("DISCO_ENCODERS")
    if encoders is None:
        encoders = e.get("PMX_ENCODERS", "local")
    return encoders.lower() == "remote"


def _resolve_encoder_urls(
    reranker_url: str, embedder_url: str, nli_url: str, e: Mapping[str, str]
) -> tuple[str, str, str]:
    # config override (non-empty) wins; else the env/default for that endpoint
    return (
        reranker_url or _env_url(e, "DISCO_RERANKER_URL"),
        embedder_url or _env_url(e, "DISCO_EMBEDDER_URL"),
        nli_url or _env_url(e, "DISCO_NLI_URL"),
    )


def _build_encoder_providers(
    use_remote: bool,
    r_url: str,
    e_url: str,
    n_url: str,
    approved: Callable[[str, str, str | None], bool],
) -> dict[str, Any]:
    if use_remote:
        # config override (non-empty) wins; else the env/default for that endpoint
        use_remote = (
            approved(r_url, "encoder:reranker", "")
            and approved(e_url, "encoder:embedder", "")
            and approved(n_url, "encoder:nli", "")
        )
    if use_remote:
        return {
            "reranker": TeiReranker(r_url),
            "embedder": OpenAIEmbedder(e_url),
            "nli": SidecarNLIVerifier(n_url),
        }
    # In-process ONNX/CPU encoders (imported lazily — models load on first use).
    from .local_encoders import FastEmbedEmbedder, FastEmbedNLIVerifier, FastEmbedReranker

    return {
        "reranker": FastEmbedReranker(),
        "embedder": FastEmbedEmbedder(),
        "nli": FastEmbedNLIVerifier(),
    }


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
    use_remote = _resolve_remote_flag(remote, e)

    search_provider, search_url, search_api_key = _resolve_search_wiring(
        search_provider, search_base_url, search_api_key, search_secret_ref, e, approved
    )
    extraction_provider, extraction_url, extraction_api_key = _resolve_extraction_wiring(
        extraction_provider,
        extraction_base_url,
        extraction_api_key,
        extraction_secret_ref,
        e,
        approved,
    )
    providers: dict[str, Any] = {
        "search": search_override or _make_search(search_provider, search_url, search_api_key),
        "extraction": _make_extraction(extraction_provider, extraction_url, extraction_api_key),
    }
    r_url, e_url, n_url = _resolve_encoder_urls(reranker_url, embedder_url, nli_url, e)
    providers.update(_build_encoder_providers(use_remote, r_url, e_url, n_url, approved))
    return providers
