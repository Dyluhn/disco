"""Evaluation hooks — retrieval-grounding-contract.md §7 (BoD §20).

Read-only metrics over the subsystem's outputs (`RetrievalResult` /
`GroundedAnswer`). [CONTRACT] these hooks READ outputs; they never alter the
live pipeline. The frontier-judge path is eval-only (a sampled subset scored by
a frontier LLM via OpenRouter — router NLI_VERIFIER overflow, §5.1), never the
live verification path.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from .models import GroundedAnswer, RetrievalResult


class RetrievalEvalMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)
    source_count: int  # everything discovered (all_hits)
    extracted_count: int
    extraction_failures: int  # fetched_ok == False (§2.2)
    extraction_failure_rate: float
    passages_returned: int  # the reranked top_k
    issued_query_count: int  # 1 for shallow/standard, several for deep


class GroundingEvalMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)
    total_claims: int
    supported: int
    weak: int
    unsupported: int
    faithfulness: float  # supported / total_claims (the primary live signal)


def retrieval_metrics(result: RetrievalResult) -> RetrievalEvalMetrics:
    """[CONTRACT] Read-only retrieval metrics (§7)."""
    failures = sum(1 for d in result.extracted if not d.fetched_ok)
    extracted = len(result.extracted)
    return RetrievalEvalMetrics(
        source_count=len(result.all_hits),
        extracted_count=extracted,
        extraction_failures=failures,
        extraction_failure_rate=(failures / extracted) if extracted else 0.0,
        passages_returned=len(result.passages),
        issued_query_count=len(result.issued_queries),
    )


def grounding_metrics(grounded: GroundedAnswer) -> GroundingEvalMetrics:
    """[CONTRACT] Read-only faithfulness/citation metrics (§7). `faithfulness`
    is the fraction of claims that verified as supported."""
    total = len(grounded.claims)
    supported = sum(1 for v in grounded.claims if v.verdict == "supported")
    weak = sum(1 for v in grounded.claims if v.verdict == "weak")
    unsupported = sum(1 for v in grounded.claims if v.verdict == "unsupported")
    return GroundingEvalMetrics(
        total_claims=total,
        supported=supported,
        weak=weak,
        unsupported=unsupported,
        faithfulness=(supported / total) if total else 1.0,
    )


def cited_passage_rerank_positions(
    result: RetrievalResult, grounded: GroundedAnswer
) -> dict[str, int]:
    """[CONTRACT] For each cited passage, its 0-based position in the reranked
    list (a retrieval-quality signal; -1 if not present)."""
    order = {p.id: i for i, p in enumerate(result.passages)}
    return {p.id: order.get(p.id, -1) for p in grounded.passages}


@runtime_checkable
class FrontierJudge(Protocol):
    """[CONTRACT boundary] Eval-only: a frontier LLM scores an answer's quality
    (router NLI_VERIFIER overflow). NOT the live verification path (§5.1)."""

    async def judge(self, query: str, answer_markdown: str) -> float: ...


def select_for_judging(keys: list[str], *, every: int = 10) -> list[str]:
    """Deterministic sampling for periodic frontier-judge eval (every Nth item),
    so a fixed question set samples reproducibly. [INTERIOR] sampling shape."""
    if every <= 1:
        return list(keys)
    return [k for i, k in enumerate(keys) if i % every == 0]
