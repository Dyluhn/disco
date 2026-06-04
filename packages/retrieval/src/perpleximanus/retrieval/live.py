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
                resp = await client.post(f"{self._base}/crawl", json={"urls": urls})
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


def build_live_retrieval(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Construct the five live providers from env (LAN defaults). Returns a dict
    {search, extraction, reranker, embedder, nli} the research wiring composes into
    a RetrievalEngine + GroundingPipeline."""
    e = os.environ if env is None else env

    def url(key: str) -> str:
        return e.get(key, _DEFAULTS[key])

    return {
        "search": SearxngSearchProvider(url("PMX_SEARXNG_URL")),
        "extraction": Crawl4aiExtractionProvider(url("PMX_CRAWL4AI_URL")),
        "reranker": TeiReranker(url("PMX_RERANKER_URL")),
        "embedder": OpenAIEmbedder(url("PMX_EMBEDDER_URL")),
        "nli": SidecarNLIVerifier(url("PMX_NLI_URL")),
    }
