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
import logging
import os
import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from . import _crawl4ai as _crawl4ai
from ._low_trust_hosts import drop_low_trust
from ._retrieval_cache import (
    RetrievalCache,
    cached_search,
    search_cache_key,
)
from .local_encoders import EncoderUnavailable
from .models import Passage, SearchHit
from .nli import Entailment
from .providers import SearchProvider
from .ranking import deduplicate_search_hits
from .url_policy import parse_source_date, query_domain_scopes, url_allowed

# Keep the historical ``live`` import surface while the cohesive provider lives
# in its own module. These private aliases are intentionally thin; behavior and
# dependency lookup remain in the provider implementation.
Crawl4aiExtractionProvider = _crawl4ai.Crawl4aiExtractionProvider
ExtractStatus = _crawl4ai.ExtractStatus
_CRAWL_CONFIG = _crawl4ai._CRAWL_CONFIG

_LOG = logging.getLogger(__name__)

# Wiring policy (unapproved/unconstructible requested providers raise) lives in
# `_provider_wiring`; provider selection + composition in `_search_wiring`. Both
# are re-exported here because `disco.retrieval.live` is the import surface every
# caller already knows — splitting the code must not split the callers.
from ._provider_wiring import ProviderConfigError as ProviderConfigError  # noqa: E402
from ._provider_wiring import _resolve_extraction_wiring as _resolve_extraction_wiring  # noqa: E402
from ._provider_wiring import _resolve_search_wiring as _resolve_search_wiring  # noqa: E402
from ._provider_wiring import build_retrieval_cache as build_retrieval_cache  # noqa: E402
from ._search_wiring import build_keyless_search as build_keyless_search  # noqa: E402
from ._search_wiring import build_multi_search as build_multi_search  # noqa: E402
from ._search_wiring import make_search as _make_search  # noqa: E402

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

# ---- SearXNG: discovery (query -> candidate URLs) ---------------------------


# Research queries are issued against the general engines AND the academic ones.
# `general` alone leaves journal coverage to whatever the web engines happen to
# index, which is how open-access papers went missing from reports whose whole
# question was academic; `science` adds arXiv/Crossref/OpenAlex/PubMed on the
# instance that has them enabled and costs nothing on one that doesn't.
SEARXNG_DEFAULT_CATEGORIES = "general,science"


class SearxngSearchProvider:
    """[ModelProvider — SearchProvider] SearXNG JSON. Discovery only: maps each
    result to a SearchHit (the `content` snippet is provider text, NOT trusted as
    content — extraction produces the citable passages).

    Per-engine truth is preserved end to end: the response's
    ``unresponsive_engines`` list reaches the diagnostic verbatim, so exactly the
    engines SearXNG named as cut off go into the cooldown registry — never the
    instance as a whole, which is still answering through its healthy engines.

    With a ``cache``, a repeat of the same question against the same instance is
    answered from disk. Only an answer that ANSWERED is ever stored (see
    ``_cached_results``) — a degraded or empty one would freeze an outage.
    """

    name = "searxng"

    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 15.0,
        categories: str = SEARXNG_DEFAULT_CATEGORIES,
        transport: httpx.AsyncBaseTransport | None = None,
        cache: RetrievalCache | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout_s
        self._categories = categories.strip()
        self._transport = transport
        self._cache = cache

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
        results, diagnostic = await self._cached_results(params)
        query_allow, query_deny = query_domain_scopes(query)
        scoped = [r for r in results if url_allowed(r.get("url") or "", query_allow, query_deny)]
        if len(scoped) != len(results):
            diagnostic["query_scope_dropped"] = len(results) - len(scoped)
        hits, low_trust_dropped = self._to_hits(scoped, domains_allow, domains_deny, limit)
        if low_trust_dropped:
            # One line per search, not per hit: the trace should show what the
            # wall removed without becoming the loudest thing in the log.
            _LOG.debug("low-trust wall dropped %d hit(s) for %r", low_trust_dropped, query)
            diagnostic["low_trust_dropped"] = low_trust_dropped
        return hits, diagnostic

    async def _cached_results(self, params: dict) -> tuple[list, dict[str, object]]:
        """This search's results, from the cache when one is warm and trustworthy."""
        key = search_cache_key(
            self._base, self._categories, str(params.get("time_range", "")), str(params["q"])
        )
        return await cached_search(self._cache, key, lambda: self._fetch_results(params))

    def _build_params(self, query: str, time_filter: str | None) -> dict:
        # DR-3 E1: SearXNG uses `time_range` param; accept "month"/"week".
        # "day" is avoided per spec (near-zero results).
        params: dict = {"q": query, "format": "json"}
        if self._categories:
            params["categories"] = self._categories
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
                payload = resp.json()
                results = payload.get("results", [])
                unresponsive = payload.get("unresponsive_engines", [])
                diagnostic: dict[str, object] = {
                    "status_code": resp.status_code,
                    "result_count": len(results) if isinstance(results, list) else 0,
                    "latency_ms": max(0, int((time.perf_counter() - started) * 1_000)),
                }
                if isinstance(unresponsive, list) and unresponsive:
                    diagnostic["unresponsive_engines"] = [
                        str(item)[:80] for item in unresponsive[:20]
                    ]
                return results if isinstance(results, list) else [], diagnostic
        except (httpx.HTTPError, ValueError) as exc:
            # Discovery failure degrades to no hits for THIS round (the run's
            # zero-evidence gate raises later if nothing was ever admitted),
            # but is never silent.
            _LOG.warning("SearXNG search failed: %s: %s", type(exc).__name__, exc)
            status_code = (
                exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            )
            return [], {
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
    ) -> tuple[list[SearchHit], int]:
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
        # The wall runs BEFORE dedupe and the limit so a page of Reddit threads
        # costs the round nothing: the freed slots go to hits that survive it.
        kept, low_trust_dropped = drop_low_trust(hits, domains_allow)
        return deduplicate_search_hits(kept)[:limit], low_trust_dropped


# ---- Crawl4AI: extraction (URL -> clean content + passages) -----------------

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
    (premise, claim) so the grounding pipeline's entail()+score() = one HTTP call.

    A transport failure still degrades to neutral — grounding feedback must never
    fabricate a contradiction out of an outage — but the no-op is COUNTED, and
    `verifier_failures` reaches the report's metadata so a run whose grounding
    checks partially did not run says so."""

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
        self._failures = 0

    @property
    def verifier_failures(self) -> int:
        """Verification calls that failed and degraded to neutral."""
        return self._failures

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
            # Neutral (weak), never a crash — and never silent: the count is
            # what lets the report declare its grounding as partial.
            data = {}
            self._failures += 1
        self._cache[key] = data
        return data

    def score(self, premise: str, hypothesis: str) -> float:
        return float(self._verify(premise, hypothesis).get("entailment", 0.0) or 0.0)

    def entail(self, premise: str, hypothesis: str) -> Entailment:
        return _LABEL.get(self._verify(premise, hypothesis).get("label", ""), "neutral")

    def verification_available(self, premise: str, hypothesis: str) -> bool:
        data = self._verify(premise, hypothesis)
        return data.get("label") in _LABEL


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


def _make_extraction(provider: str, base_url: str, api_key: str, data_dir: str = ""):
    """Select the extraction provider (§B1). BUNDLED `local` is the default — in-
    process, no service. `crawl4ai` self-hosts; `firecrawl` is a paid key.

    The page cache is built only for the provider that can use it, and only when
    `data_dir` names somewhere to put it — an empty one means run uncached."""
    from .bundled_providers import FirecrawlExtractionProvider, LocalExtractionProvider

    if provider == "crawl4ai":
        return Crawl4aiExtractionProvider(base_url, cache=build_retrieval_cache(data_dir))
    if provider == "firecrawl":
        return FirecrawlExtractionProvider(api_key, base_url or "https://api.firecrawl.dev")
    return LocalExtractionProvider()  # default / "local"


def _env_url(e: Mapping[str, str], key: str) -> str:
    """DISCO_<X> preferred; legacy PMX_<X> honored; else the LAN default."""
    v = e.get(key)
    if v is None and key.startswith("DISCO_"):
        v = e.get("PMX_" + key[len("DISCO_") :])
    return v if v is not None else _DEFAULTS[key]


def _env_data_dir(e: Mapping[str, str]) -> str:
    """The data dir, or "" when none is configured. Read from the SAME mapping
    the rest of this wiring reads, so `build_live_retrieval(env={})` stays
    hermetic instead of reaching past its argument into the real environment."""
    return e.get("DISCO_DATA_DIR") or e.get("PMX_DATA_DIR") or ""


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
    search_provider: str = "bundled",
    search_base_url: str = "",
    search_api_key: str = "",
    search_secret_ref: str = "",
    search_categories: str = "",
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
        "search": search_override
        or _make_search(
            search_provider,
            search_url,
            search_api_key,
            categories=search_categories,
            data_dir=_env_data_dir(e),
        ),
        "extraction": _make_extraction(
            extraction_provider, extraction_url, extraction_api_key, _env_data_dir(e)
        ),
    }
    r_url, e_url, n_url = _resolve_encoder_urls(reranker_url, embedder_url, nli_url, e)
    providers.update(_build_encoder_providers(use_remote, r_url, e_url, n_url, approved))
    return providers
