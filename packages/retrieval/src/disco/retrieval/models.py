"""Retrieval & grounding data types — retrieval-grounding-contract.md §2/§3/§5.

The unit of provenance is `Passage`: a citable span that survives retrieval →
generation → UI citation (principle 6). Field names/types are normative.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class SearchHit(BaseModel):
    """[CONTRACT] One discovery result — a candidate URL, not yet read."""

    model_config = ConfigDict(frozen=True)
    url: str
    title: str
    snippet: str = ""  # provider snippet — NOT trusted as content (§1.4)
    source_engine: str = ""  # which engine produced it (provenance)
    rank: int = 0  # provider's original rank
    # ``None`` means discovered but not attempted.  The retrieval engine fills
    # this after extraction so downstream report surfaces never have to guess
    # that a bare discovery hit was successfully read.
    status: Literal["ok", "paywalled", "blocked", "not_found", "error"] | None = None


class Passage(BaseModel):
    """[CONTRACT] A citable span of source content. The unit of provenance that
    survives retrieval -> generation -> UI citation."""

    model_config = ConfigDict(frozen=True)
    id: str  # stable id for citing (e.g. "src3_p7")
    source_url: str
    source_title: str
    text: str
    char_start: int | None = None  # location within the source (for the UI)
    char_end: int | None = None
    corpus_id: str | None = None  # set when from a Space corpus, not the live web


class ExtractedDoc(BaseModel):
    """[CONTRACT] Clean, LLM-ready content from one URL, with provenance."""

    model_config = ConfigDict(frozen=True)
    url: str
    title: str
    content: str  # cleaned main text (markdown/plain)
    passages: list[Passage] = Field(default_factory=list)
    fetched_ok: bool = True
    error: str | None = None  # populated iff fetch/extract failed (§2.2)
    status: Literal["ok", "paywalled", "blocked", "not_found", "error"] = "ok"


class RetrievalRequest(BaseModel):
    model_config = ConfigDict(frozen=True)
    query: str
    corpus_ids: frozenset[str] = frozenset()  # empty => live web only
    use_web: bool = True
    depth: Literal["shallow", "standard", "deep"] = "standard"
    top_k: int = 8  # final passages to return
    # Optional discovery/extraction bounds.  Ordinary Search keeps the engine
    # defaults; Deep Research supplies the selected tier's explicit limits.
    discover_limit: int | None = Field(default=None, ge=1)
    extract_cap: int | None = Field(default=None, ge=1)
    domains_allow: frozenset[str] | None = None
    domains_deny: frozenset[str] | None = None
    provider: str | None = None  # override default SearchProvider
    # DR-3 E2: recency window for time-filtered search + date prompt injection.
    # ``None`` (default) = no time filter, no date text injected → byte-identical
    # to a request without this field (the OFF path assertion in tests).
    recency_window: Literal["month", "week"] | None = None


class RetrievalResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    passages: list[Passage]  # the reranked top_k, with provenance
    all_hits: list[SearchHit]  # the full discovery set (incl. failed/blocked, §2.2)
    extracted: list[ExtractedDoc]
    issued_queries: list[str]  # which queries were issued (diagnostics)
    notes: dict[str, Any] = Field(default_factory=dict)


# ---- grounding (§5) ---------------------------------------------------------


class Claim(BaseModel):
    """[CONTRACT] An atomic assertion extracted from a generated answer, with
    the passage ids the generator cited for it."""

    model_config = ConfigDict(frozen=True)
    text: str
    cited_passage_ids: list[str]  # ids from RetrievalResult.passages


class VerifiedClaim(BaseModel):
    model_config = ConfigDict(frozen=True)
    claim: Claim
    verdict: Literal["supported", "weak", "unsupported"]
    best_passage_id: str | None  # the strongest entailing passage
    entailment_score: float  # from the NLI verifier


class GroundedAnswer(BaseModel):
    """[CONTRACT] The output of the grounding pipeline — what the UI renders."""

    model_config = ConfigDict(frozen=True)
    answer_markdown: str  # prose with inline [n] citations
    claims: list[VerifiedClaim]  # per-claim verification verdicts
    passages: list[Passage]  # the cited sources (provenance)
    all_hits: list[SearchHit]  # for the "All Searched" tab
    unsupported_count: int  # claims that failed verification
