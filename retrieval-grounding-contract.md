# Technical Design Contract — Retrieval & Grounding

**Document type:** Detailed Technical Design (Contract Spec)
**Subsystem:** Retrieval Engine + Citation/Grounding — BoD §9 (retrieval) + §14 (grounding)
**Status:** v1.1 — authoritative contract (clarifications after Phase 2 build)

> **v1.1 changelog (post-Phase-2-build).** Five clarifications surfaced by the build, no behavioral change: (1) **packaging** — retrieval is its **own package** (`packages/retrieval`), not under `core`; (2) **tool granularity** — the `search` tool performs *discovery only* (returns `SearchHit`s) and `extract` performs *extraction only*; the full retrieve→rerank→ground pipeline is the **`research_answer` composition** (§6), not a single tool call; (3) **NLI verdict mapping** is pinned: `entail`→supported, `contradict`→unsupported, `neutral`→**weak** (the prior prose conflated "unentailed" with "contradicted"); (4) **claim-extraction format** — the shipped parser reads inline `text [id1, id2].` claims; the two-stage "emit a JSON Claim list first" remains `[INTERIOR]`, not required; (5) **self-correction** ships **drop-unsupported**; "regenerate-from-supported" remains `[INTERIOR]`, not required.

**Depends on:**
- Event & State contract (v1.2) — `ToolResult` (search/extract observations flow through it), the event log (indexing runs as a §7.6 callback).
- LLM Router contract (v1.1) — `NLIVerifier` (the citation-entailment cross-encoder, defined there, *implemented* here), `CapabilityProfile`/`ModelRole` (`RAG_ANSWERER`, `QUERY_REWRITER` route here), `LLMRouter`.
- Tool/Sandbox contract (v1.1) — the `search` and `extract` tools are registered there and **back onto the providers defined here**; they reach providers via the orchestrator-mediated `CapabilitySet` (provider keys stay out of the sandbox).
**Consumed by:** the Research surface (BoD §8.1, its whole pipeline), the Agent surface (which also searches/reads), the citation UI (BoD §13.3, which renders this subsystem's provenance), and Spaces (BoD §8.3, bounded corpora).
**Grounding from the corpus:** retrieval is the #1 quality determinant (BoD §1.1); the pipeline patterns (multi-query, RRF, cross-encoder rerank, span-provenance, NLI verification) are from the distilled research findings.

---

## 0. What this document is (and is not)

**Is:** the binding contract for (a) the **retrieval engine** — discovery (find URLs), extraction (URL → clean content), the quality pipeline (query transformation, reranking), the vector store, and Space corpora; and (b) the **grounding pipeline** — span-provenance retrieval, constrained generation, NLI verification, and self-correction. It defines the providers the tool contract's `search`/`extract` tools sit on, and it **implements** the router contract's `NLIVerifier`.

**Is not:** the tool *registration*/scoping (tool contract owns that — this owns what the tools *do*), the LLM router (this consumes it for the RAG-answerer and query-rewriter roles), or the answer-rendering UI (BoD §13.3 — this produces the provenance the UI renders). Interior choices below the contract line (which embedding model build, exact reranker checkpoint, HTTP client) are the builder's, subject to the `[VERIFY]` notes.

**Conventions** (identical to prior contracts): illustrative Python 3.12 + Pydantic v2; field names/types/signatures **normative**, bodies illustrative. **[CONTRACT]** = relied-upon guarantee; **[INTERIOR]** = builder's free choice; **[VERIFY]** = confirm at build (model/checkpoint names, provider availability, versions).

**Packaging note (v1.1).** Retrieval is its **own package** (`packages/retrieval`, importable as `perpleximanus.retrieval`), depending only on `core` — **not** under `core`. The `search`/`extract` *tools* live in `tools` (`perpleximanus.tools.builtin`); the capability *handlers* that back those tools onto this engine live in `perpleximanus.retrieval.wiring` (registered with the tool contract's `CapabilityBroker`), so retrieval stays a `core`-only-dependent sibling and never imports `tools`.

---

## 1. Foundational principles [CONTRACT]

1. **Retrieval quality dominates answer quality** (BoD §1.1). This subsystem gets disproportionate care: the reranking + query-transformation layer matters more than the model. Reranking is cheaper than a bigger context and is the single highest-leverage quality lever.
2. **Two slots: discovery and extraction.** *Discovery* finds candidate URLs (SearXNG + optional APIs); *extraction* turns a URL into clean, LLM-ready content (the owned Firecrawl). They are distinct interfaces; "I have Firecrawl" does not cover discovery.
3. **Self-hosted-first, provider-extensible** (BoD §4.2). SearXNG + Firecrawl (both owned, both self-hosted, zero third-party query egress) are the defaults, behind a provider abstraction so third-party APIs (Serper/Brave/Exa/Tavily/Jina) can be added for quality without dependency.
4. **Never trust the snippet** (BoD §9.4) `[OH: info_rules]`. Information priority is *authoritative source/API > extracted page content > model internal knowledge*. The pipeline opens the original URL; it does not answer from a search snippet.
5. **Grounding is a pipeline, not a prompt** (BoD §14). Citation faithfulness is enforced by a verification stage (NLI), not by instructing the model to cite. Even strong models leave a meaningful fraction of claims unsupported on their own.
6. **Span-level provenance end to end.** Every retrieved passage carries its source identity and location, and that provenance survives into generation and into the UI's citations. A claim can always be traced to a span.
7. **Provider keys stay out of the sandbox.** Search/extract providers requiring keys are reached via the orchestrator-mediated capability (tool contract §6); the key is never in the sandbox or a prompt.
8. **Corpora are bounded and namespaced** (Spaces, BoD §8.3). A Space's uploaded documents form an *exclusive* retrieval silo; a query in one Space never retrieves another's corpus. No cross-contamination.

---

## 2. The two slots: discovery & extraction providers [CONTRACT]

```python
from __future__ import annotations
from typing import Any, Protocol, Literal
from pydantic import BaseModel, Field, ConfigDict

class SearchHit(BaseModel):
    """[CONTRACT] One discovery result — a candidate URL, not yet read."""
    model_config = ConfigDict(frozen=True)
    url: str
    title: str
    snippet: str = ""                  # provider snippet — NOT trusted as content (§1.4)
    source_engine: str = ""            # which engine produced it (provenance)
    rank: int = 0                      # provider's original rank

class SearchProvider(Protocol):
    """[CONTRACT] Discovery: a query -> candidate URLs. SearXNG is the default;
    APIs are alternative implementations. Key-bearing providers are reached via
    the orchestrator capability (tool contract §6)."""
    name: str                          # "searxng" | "serper" | "brave" | "exa" | "tavily" | ...
    async def search(self, query: str, *, limit: int = 10,
                     domains_allow: frozenset[str] | None = None,
                     domains_deny: frozenset[str] | None = None) -> list[SearchHit]: ...

class ExtractedDoc(BaseModel):
    """[CONTRACT] Clean, LLM-ready content from one URL, with provenance."""
    model_config = ConfigDict(frozen=True)
    url: str
    title: str
    content: str                       # cleaned main text (markdown/plain)
    # passage-level segmentation with provenance; each carries enough to cite.
    passages: list["Passage"] = Field(default_factory=list)
    fetched_ok: bool = True
    error: str | None = None           # populated iff fetch/extract failed (§ explicit-failure)
    status: Literal["ok", "paywalled", "blocked", "not_found", "error"] = "ok"

class ExtractionProvider(Protocol):
    """[CONTRACT] Extraction: a URL -> ExtractedDoc. The owned Firecrawl is the
    default; Trafilatura/Crawl4AI/Jina-Reader are alternatives."""
    name: str                          # "firecrawl" | "trafilatura" | "jina" | ...
    async def extract(self, url: str) -> ExtractedDoc: ...
    async def extract_many(self, urls: list[str]) -> list[ExtractedDoc]: ...

class Passage(BaseModel):
    """[CONTRACT] A citable span of source content. The unit of provenance that
    survives retrieval -> generation -> UI citation."""
    model_config = ConfigDict(frozen=True)
    id: str                            # stable id for citing (e.g. "src3_p7")
    source_url: str
    source_title: str
    text: str
    # location within the source, for the UI's "corroborating snippet" and
    # for the user to verify without leaving the page (BoD §13.3).
    char_start: int | None = None
    char_end: int | None = None
    # set when this passage comes from a Space corpus rather than the live web.
    corpus_id: str | None = None
```

### 2.1 Default providers [INTERIOR impls, CONTRACT boundary] (BoD §4.2)
- **Discovery:** `searxng` (self-hosted, the privacy/cost anchor, default). Optional: `serper`, `brave`, `exa`, `tavily`, `jina` — each a `SearchProvider`, selectable per query/per Space, **off by default**, key reached via capability.
- **Extraction:** `firecrawl` (owned, self-hosted, default). Optional: `trafilatura`/`crawl4ai` (local), `jina` reader — each an `ExtractionProvider`.
- **[CONTRACT]:** the `search`/`extract` tools (tool contract §9) call these interfaces; switching or adding a provider is config, no tool/loop change. The owned SearXNG+Firecrawl path has zero third-party query egress (BoD §4.2).

### 2.2 Explicit failure (BoD §13.3) [CONTRACT]
Extraction failures are **first-class, not swallowed**: a paywalled/blocked/404/error URL yields an `ExtractedDoc` with `fetched_ok=False` and a `status` the UI renders as a visible warning (BoD §13.3 "make failures explicit — hidden failures destroy trust"). A failed extraction never silently becomes "no content" with no signal.

---

## 3. The quality pipeline [CONTRACT] — the #1 quality lever

Raw search results are not good enough for Perplexity-class answers (BoD §9.3). The pipeline transforms the query, retrieves broadly, reranks hard, and carries provenance forward. The **stages and their guarantees** are contractual; the exact algorithms are tunable.

```python
class RetrievalRequest(BaseModel):
    model_config = ConfigDict(frozen=True)
    query: str
    # which corpora to search: the live web, and/or specific Space corpora.
    corpus_ids: frozenset[str] = frozenset()    # empty => live web only
    use_web: bool = True
    # difficulty hint → how aggressive the pipeline is (multi-query etc.)
    depth: Literal["shallow", "standard", "deep"] = "standard"
    top_k: int = 8                              # final passages to return
    domains_allow: frozenset[str] | None = None
    domains_deny: frozenset[str] | None = None
    provider: str | None = None                 # override default SearchProvider

class RetrievalResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    passages: list[Passage]                     # the reranked top_k, with provenance
    # the full discovery set (for the UI's "All Searched" tab, BoD §13.3),
    # including failed/blocked sources (§2.2).
    all_hits: list[SearchHit]
    extracted: list[ExtractedDoc]
    # diagnostics: which queries were issued, timings, provider(s) used.
    issued_queries: list[str]
    notes: dict[str, Any] = Field(default_factory=dict)

class RetrievalEngine(Protocol):
    """[CONTRACT] The orchestrator of the pipeline. The search/extract tools and
    the RAG answerer call this."""
    async def retrieve(self, req: RetrievalRequest) -> RetrievalResult: ...
```

**[CONTRACT] the pipeline stages** (BoD §9.3), in order:
1. **Query transformation.** `standard`: rewrite-retrieve-read (one rewritten query). `deep`: **multi-query / RAG-Fusion** — generate 3–5 paraphrases via the `QUERY_REWRITER` role (router), retrieve each. (HyDE/step-back are [INTERIOR] options.) `shallow`: the raw query. The rewriter is a **local 7–8B** model (router §15.2).
2. **Broad retrieval.** Pull a wide candidate set (20–50 passages) across the selected provider(s) and corpora. For multi-query, fuse the per-query result lists with **Reciprocal Rank Fusion (RRF)** before reranking.
3. **Extraction with provenance.** Fetch the candidate URLs via the `ExtractionProvider`, segment into `Passage`s carrying source + location (§2). **Never** rank on snippets alone — open the source (§1.4).
4. **Rerank.** A **cross-encoder reranker** (self-hosted `bge-reranker-v2-m3`-class with `bge-m3` embeddings; Cohere/Jina rerankers as API options) scores candidate passages against the query and reduces to `top_k` (5–10). This is the highest-leverage stage.
5. **Return** the reranked passages (with provenance) plus `all_hits`/`extracted` for the UI's source panel.

**[CONTRACT] guarantees:**
- The returned `passages` are ranked and carry full provenance (`Passage` ids the grounding stage and UI will cite).
- `all_hits` includes everything discovered — including failed/blocked sources (§2.2) — so the UI's "All Searched" vs "Cited" tabs (BoD §13.3) are both populated.
- Caching and domain allow/deny weighting are applied (BoD §9.3); [INTERIOR] cache mechanics.
- `depth` controls aggressiveness and therefore cost/latency; the loop/surface sets it.

### 3.1 The reranker & embeddings [CONTRACT boundary]
```python
class Reranker(Protocol):
    """[CONTRACT] Scores (query, passage) pairs; the pipeline keeps the top_k.
    Self-hosted cross-encoder by default; API rerankers are alternatives."""
    async def rerank(self, query: str, passages: list[Passage], *, top_k: int) -> list[Passage]: ...

class Embedder(Protocol):
    """[CONTRACT] Produces vectors for passages/queries for the vector store
    (§4) and hybrid retrieval. Local bge-m3-class by default."""
    async def embed(self, texts: list[str]) -> list[list[float]]: ...
```
[VERIFY] exact checkpoints (`bge-reranker-v2-m3`, `bge-m3`) at build; they live in config alongside the model roles.

---

## 4. Vector store & Space corpora [CONTRACT]

A `VectorStore` backs both ad-hoc RAG and Space corpora, with **namespaced collections** so each Space is an isolated silo (BoD §8.3, principle 8).

```python
class VectorStore(Protocol):
    """[CONTRACT] Namespaced vector storage. Each namespace is an isolated
    corpus; queries are scoped to explicit namespaces — no cross-namespace leak.
    pgvector or a dedicated local engine [OPEN §24-D1]; behind this interface."""
    async def upsert(self, namespace: str, passages: list[Passage],
                     vectors: list[list[float]]) -> None: ...
    async def query(self, namespace: str, vector: list[float], *, top_k: int) -> list[Passage]: ...
    async def delete_namespace(self, namespace: str) -> None: ...

class CorpusService(Protocol):
    """[CONTRACT] Manages Space corpora: ingest documents/URLs/repos into a
    namespaced corpus that becomes an EXCLUSIVE retrieval silo for that Space."""
    async def ingest(self, corpus_id: str, *, owner_id: str,
                     docs: list[ExtractedDoc]) -> None: ...
    async def remove(self, corpus_id: str) -> None: ...
```

**[CONTRACT] rules:**
- **Namespace = corpus = Space silo.** A `RetrievalRequest.corpus_ids` scopes vector queries to exactly those namespaces. A query never retrieves a namespace it didn't ask for (principle 8; the anti-cross-contamination guarantee — a coding query never pulls an unrelated uploaded PDF).
- **Ownership:** corpora carry `owner_id` (BoD §4.1); never shared across owners.
- **Hybrid retrieval** [INTERIOR]: the engine may combine BM25 + dense + RRF (BoD §9.3) over a corpus; the contract requires only that results are `Passage`s with provenance.
- **Ingestion** reuses the `ExtractionProvider` (§2) to turn uploaded URLs/docs into `Passage`s, then `Embedder` + `VectorStore.upsert`. Indexing of *agent-produced* results runs as an event-callback (event contract §7.6), off the hot path.

---

## 5. The grounding & citation pipeline [CONTRACT] — faithfulness is enforced, not asked

This is BoD §14 made executable. Generation is constrained; verification is a real stage; failures are surfaced. The `RAG_ANSWERER` role (router) generates; the `NLIVerifier` (router interface, **implemented here**) verifies.

```python
class Claim(BaseModel):
    """[CONTRACT] An atomic assertion extracted from a generated answer, with
    the passage ids the generator cited for it."""
    model_config = ConfigDict(frozen=True)
    text: str
    cited_passage_ids: list[str]                # ids from RetrievalResult.passages

class GroundedAnswer(BaseModel):
    """[CONTRACT] The output of the grounding pipeline — what the UI renders."""
    model_config = ConfigDict(frozen=True)
    answer_markdown: str                        # prose with inline [n] citations
    claims: list["VerifiedClaim"]               # per-claim verification verdicts
    passages: list[Passage]                     # the cited sources (provenance)
    all_hits: list[SearchHit]                   # for the "All Searched" tab
    unsupported_count: int                      # claims that failed verification

class VerifiedClaim(BaseModel):
    model_config = ConfigDict(frozen=True)
    claim: Claim
    verdict: Literal["supported", "weak", "unsupported"]
    best_passage_id: str | None                 # the strongest entailing passage
    entailment_score: float                     # from the NLI verifier
```

**[CONTRACT] the pipeline** (BoD §14.1), in order:
1. **Retrieve with provenance** (§3) → ranked `Passage`s.
2. **Constrained generation.** The `RAG_ANSWERER` (router; a local **8–14B**) is given numbered passages and must end each factual claim with its `[passage_ids]`. Temperature low. Citations are **forced by the prompt/format**, but that is not the guarantee — verification is. (The two-stage "emit a JSON `Claim` list first, then render prose" is `[INTERIOR]`; the shipped default parses inline `text [ids].` claims from the generated prose.)
3. **Verification.** Split the answer into atomic `Claim`s; for each, run **NLI entailment** (premise = cited passage(s); hypothesis = claim) via the `NLIVerifier`. Produce a `verdict` per claim with this pinned mapping (v1.1): `entail` → **supported**; `contradict` → **unsupported**; `neutral` (unentailed) → **weak**. Unentailed is distinct from contradicted: a claim the evidence neither supports nor refutes is `weak`, not `unsupported`.
4. **Self-correction.** The **shipped default is drop-unsupported** (`strictness="drop"`): claims that fail entailment are dropped from the rendered answer (and still surfaced in `claims`, step 5). **Regenerate-from-supported** is the `[INTERIOR]` alternative, not required (BoD §14, tuned empirically).
5. **Surface honestly** (BoD §13.3): `unsupported`/`weak` claims and failed sources are marked in the `GroundedAnswer` for the UI — never hidden.

### 5.1 The NLIVerifier — implementing the router's interface [CONTRACT]
The router contract declared `NLIVerifier` (a ~300M cross-encoder, **not** an LLM) and left it for this subsystem. Implemented here:

```python
class CrossEncoderNLIVerifier:
    """[CONTRACT] Satisfies the router contract's NLIVerifier protocol. A local
    ~300M cross-encoder (DeBERTa-v3-large-MNLI class; e.g. an HHEM-class model).
    3-way entailment. Runs local, cheap, off the LLM path (router §9.2)."""
    def entail(self, premise: str, hypothesis: str) -> Literal["entail","neutral","contradict"]: ...
    def score(self, premise: str, hypothesis: str) -> float: ...   # entailment probability
```

**[CONTRACT]:**
- This is a **cross-encoder, not a chat model** — it does **not** go through the router's `complete()` (router §9.2). It runs as a local side-car.
- **Local-first, frontier-judge sparingly** (BoD §14.2): live verification is fully local (the cross-encoder is trivial next to the LLMs). A frontier LLM "judge" via OpenRouter is reserved for **periodic eval sampling** (§7 / BoD §20), not the live path.
- [VERIFY] the exact checkpoint (DeBERTa-v3-large-MNLI / HHEM-2.1-Open class) at build; it lives in config as the `NLI_VERIFIER` role's local model.

---

## 6. The Research surface flow [CONTRACT] — how it composes

This ties the subsystem to the loop and surfaces (BoD §8.1). The Research surface is the agent core with a retrieval-only tool scope and a synthesis prompt; this is the standard-answer and Deep-Research flows.

**Standard answer** (short loop):
1. User query → the loop (Research scope: `search`/`extract` tools only, `NeverConfirm`).
2. The agent calls `search`/`extract`, which call `RetrievalEngine.retrieve` (§3) → ranked `Passage`s with provenance.
3. The `RAG_ANSWERER` generates a constrained, cited answer (§5 step 2).
4. The grounding pipeline verifies + self-corrects (§5 steps 3–4) → a `GroundedAnswer`.
5. The UI renders the document-grade answer with the source panel, hover citations, and All/Cited tabs (BoD §13.3) from the `GroundedAnswer`'s provenance.

**Deep Research** (long-horizon, BoD §8.1):
- The loop runs in `LONG_HORIZON`/`PLANNING` mode with `depth="deep"`: many iterations of *search → read → refine plan → synthesize* across many sources, an externalized investigation plan (loop §9), and a structured report as output.
- **[CONTRACT]** Deep Research uses the *same* retrieval + grounding pipeline at higher `depth` and iteration count — it is not a separate engine (BoD §3.1 "one core, many surfaces"). The report's sections each carry grounded, verified citations; the UI renders the §13.5 report view (pinned ToC, export).
- `depth="deep"` is the cost lever the plan-preview gate (loop §5 / BoD §13.2) trims before the run.

**[CONTRACT] separation of concerns:** this subsystem produces `RetrievalResult`/`GroundedAnswer` (data + provenance); the loop drives *when* to retrieve/answer; the UI renders. No retrieval logic lives in the loop or the UI.

---

## 7. Evaluation hooks [CONTRACT boundary] (BoD §20)

The subsystem is built to be measured (BoD §1.1, §20):
- **Faithfulness/citation metrics** (RAGAS/DeepEval-class): faithfulness, answer relevance, context precision/recall, computed over a fixed question set against `GroundedAnswer`/`RetrievalResult`. The `unsupported_count` and per-claim verdicts are the primary live signal.
- **Frontier-judge sampling:** a sampled subset of answers is scored by a frontier LLM via OpenRouter (router overflow for `NLI_VERIFIER`, §5.1) — eval only, not the live path.
- **Retrieval metrics:** source counts, rerank-position-of-cited-passages, extraction failure rate (§2.2), latency per stage.
- [CONTRACT]: these hooks read the subsystem's outputs; they do not alter the live pipeline. Wired into the §20 eval harness.

---

## 8. Test plan [CONTRACT — defines correctness]

Headless. Providers (SearXNG/Firecrawl/APIs), the reranker, the embedder, and the LLM roles are **faked/local-stubbed** — no real network, no real provider keys, no real model required for the logic tests. The subsystem is done when these pass.

### 8.1 Providers & the two slots
- **Discovery vs extraction are distinct:** a `SearchProvider` returns `SearchHit`s (URLs, untrusted snippets); an `ExtractionProvider` returns `ExtractedDoc`s (content + provenance). A test asserts the pipeline never treats a `SearchHit.snippet` as cited content (§1.4) — citations only come from extracted `Passage`s.
- **Provider swap:** the pipeline runs identically against a fake SearXNG and a fake API provider behind `SearchProvider` (no provider leakage into the engine).
- **Explicit failure (§2.2):** a paywalled/blocked/404 URL yields `fetched_ok=False` + the right `status`; it appears in `all_hits` and is never silently dropped.

### 8.2 The quality pipeline
- **Multi-query + RRF (`deep`):** N paraphrases produce N result lists fused by RRF before rerank; a passage ranked high across multiple queries outranks one ranked high in only one (table-driven against a fake rewriter).
- **Rerank reduces to top_k** and reorders by the (fake) cross-encoder score; cited passages carry full provenance.
- **`depth` controls aggressiveness:** `shallow` issues one query, `deep` issues multi-query (assert `issued_queries`).
- **`all_hits` populated:** the result carries the full discovery set (for the UI's All/Cited tabs), distinct from the reranked `passages`.

### 8.3 Vector store & Space corpora (the isolation tests)
- **Namespace isolation (the headline corpus test):** a query scoped to corpus A never returns a passage from corpus B (inject distinct sentinel passages into two namespaces; query A; assert no B passage — the anti-cross-contamination guarantee, principle 8).
- **Ownership:** corpora carry `owner_id`; a different owner's corpus is never queried.
- **Ingest round-trip:** an ingested doc becomes queryable `Passage`s in its namespace with provenance intact; `delete_namespace` removes them.

### 8.4 Grounding & NLI (the faithfulness tests)
- **Constrained generation:** the fake `RAG_ANSWERER` output is split into atomic `Claim`s with their cited passage ids.
- **NLI verdicts:** with a fake `NLIVerifier`, the pinned mapping (v1.1) holds — `entail` → `supported`; `contradict` → `unsupported`; `neutral` (unentailed) → `weak` (unentailed is distinct from contradicted). Table-driven.
- **Self-correction:** unsupported claims are dropped/regenerated per the strictness knob; `unsupported_count` reflects what failed.
- **Honest surfacing (§13.3):** `weak`/`unsupported` claims and failed sources appear in the `GroundedAnswer` (not hidden) — assert they're present and flagged.
- **NLIVerifier is not the LLM path:** assert verification does **not** call the router's `complete()` (it uses the cross-encoder side-car, router §9.2).

### 8.5 Cross-contract integration (acceptance gate)
- End-to-end with fakes and the `process`/in-memory backends: Research-scope loop → agent calls `search`+`extract` (tool contract) → `RetrievalEngine.retrieve` returns ranked provenance → `RAG_ANSWERER` (fake) generates cited prose → NLI verification (fake) → `GroundedAnswer` with populated `passages`/`all_hits`/`unsupported_count`. Asserts the subsystem composes with the loop (via the search/extract tools), the router (RAG/rewriter roles + NLIVerifier), and produces exactly the provenance the UI (BoD §13.3) needs. Also asserts provider keys never appear in the sandbox/context (cross-check with tool contract §6).

---

## 9. What this contract hands to / expects from each subsystem

- **Tool/Sandbox (tool contract):** the `search`/`extract` tools **back onto** this subsystem's `SearchProvider`/`ExtractionProvider`; they reach key-bearing providers through the `CapabilitySet` (keys out of the sandbox). This contract owns what the tools *do*; the tool contract owns their registration/scoping.
  - **[CONTRACT] tool granularity (v1.1):** the `search` tool performs **discovery only** (returns `SearchHit`s); the `extract` tool performs **extraction only** (returns `ExtractedDoc`s). The full retrieve→rerank→ground pipeline is the **`research_answer` composition** (§6), invoked by the Research-surface flow — **not** a single tool call. An agent on the Agent surface may call `search`/`extract` individually; the Research surface composes them via `research_answer`. (The `search` tool surfaces snippets for the agent's judgment, but per §1.4 a snippet is never cited as content — citations come only from extracted `Passage`s.)
- **LLM Router (router contract):** this **implements** `NLIVerifier` (the cross-encoder), and **consumes** the `RAG_ANSWERER` and `QUERY_REWRITER` roles via `LLMRouter`. Frontier-judge eval uses `NLI_VERIFIER` overflow (router §9.2).
- **Agent loop (loop contract):** the loop drives *when* to retrieve/answer (via the tools); `depth` and surface scope are set per surface; the loop is unaware of pipeline internals.
- **UI (BoD §13.3):** renders `GroundedAnswer` — inline citations, the source panel (hover cards with the corroborating `Passage` span), All/Cited tabs (from `all_hits` vs cited `passages`), explicit failure states (§2.2), and re-scope controls (filter domains / drop weak sources / force re-synthesis — which re-invoke `retrieve` with adjusted `RetrievalRequest`).
- **Spaces (BoD §8.3):** a Space maps to a `corpus_id`/namespace via `CorpusService`; `RetrievalRequest.corpus_ids` scopes retrieval to it.
- **Eval/observability (BoD §20):** consumes the §7 hooks; the event log's indexing callback (event §7.6) populates corpora off the hot path.

## 10. Build order (this subsystem) & sequencing

This is the **Phase-2** subsystem (the Research surface, BoD §22) and the design that unblocks the first real UI. It depends on the now-closed core (loop/router/tools) and the `search`/`extract` tool seams.

1. **Provider interfaces + defaults** (§2): `SearchProvider`(searxng) + `ExtractionProvider`(firecrawl), `SearchHit`/`ExtractedDoc`/`Passage`, explicit-failure (§2.2); tests 8.1.
2. **The quality pipeline** (§3): query transformation (via `QUERY_REWRITER`), broad retrieval, RRF, **the cross-encoder reranker** + embedder; tests 8.2. (Reranking first — highest leverage.)
3. **Vector store + Space corpora** (§4) with namespace isolation; tests 8.3 (the isolation test is the headline).
4. **The grounding pipeline** (§5): constrained generation (`RAG_ANSWERER`), the **`CrossEncoderNLIVerifier`** (implementing the router's interface), verification + self-correction + honest surfacing; tests 8.4.
5. **Wire the `search`/`extract` tools** (tool contract §9) to `RetrievalEngine`, reaching providers via `CapabilitySet`; the Research-surface flow (§6).
6. **Eval hooks** (§7) — read-only over outputs.
7. The §8.5 cross-contract integration test — the acceptance gate.
8. **Then the UI** (BoD §13.3, Phase 2 frontend) renders `GroundedAnswer` — the first real UI; this is where your design taste lands.

**Parallel, unblocked:** the Phase-1 remainder (real condenser, live LLM adapters) and the tool/sandbox deferrals (e2b backend, browser tool). The live SearXNG/Firecrawl wiring needs your running instances ([VERIFY] endpoints) but the logic is testable against fakes first.

---

### Appendix — open items specific to this contract
- **[OPEN §24-D1] Vector store engine:** pgvector (one fewer service if Postgres is in) vs. a dedicated local vector DB. Resolve at step 3.
- **[VERIFY] checkpoints:** `bge-m3` (embeddings), `bge-reranker-v2-m3` (rerank), DeBERTa-v3-large-MNLI/HHEM-class (NLI) — pin at build; they live in config as the relevant model roles.
- **[VERIFY] owned-instance endpoints:** the SearXNG and Firecrawl URLs (yours) — config, reached via capability.
- **[OPEN] reranker source:** self-hosted cross-encoder (default) vs. API reranker (Cohere/Jina) — config; start self-hosted (BoD §9.3, in your GPU comfort zone).
- **[OPEN] self-correction strictness:** drop-unsupported vs. regenerate-from-supported (§5 step 4) — tune empirically (BoD §14).
- **[INTERIOR] hybrid retrieval:** BM25+dense+RRF over corpora — builder's choice as long as results are provenanced `Passage`s.
- **Relationship to the answer UI:** this contract produces `GroundedAnswer`; BoD §13.3 specifies its rendering. The UI is the next thing built after this subsystem (step 8), and is where the design language (BoD §13.7) first ships.
- **Next contracts:** the **Security analyzer detail** (the `SecurityAnalyzer` scoring internals — hardens the Agent surface) is the last core design contract. After that, the remaining work is build/capability (live adapters, e2b backend, browser tool, the UI), not architecture.
