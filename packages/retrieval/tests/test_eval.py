"""Evaluation hooks — retrieval-grounding-contract.md §7 (read-only metrics)."""

from __future__ import annotations

from disco.retrieval import (
    Claim,
    ExtractedDoc,
    GroundedAnswer,
    Passage,
    RetrievalResult,
    SearchHit,
    VerifiedClaim,
    cited_passage_rerank_positions,
    grounding_metrics,
    retrieval_metrics,
    select_for_judging,
)


def _vc(text, verdict, pid="p1", score=1.0):
    return VerifiedClaim(
        claim=Claim(text=text, cited_passage_ids=[pid]),
        verdict=verdict,
        best_passage_id=pid,
        entailment_score=score,
    )


def test_retrieval_metrics_extraction_failure_rate():
    result = RetrievalResult(
        passages=[Passage(id="p1", source_url="u1", source_title="t", text="x")],
        all_hits=[SearchHit(url="u1", title="t"), SearchHit(url="u2", title="t")],
        extracted=[
            ExtractedDoc(url="u1", title="t", content="x"),
            ExtractedDoc(url="u2", title="t", content="", fetched_ok=False, status="paywalled"),
        ],
        issued_queries=["q"],
    )
    m = retrieval_metrics(result)
    assert m.source_count == 2
    assert m.extraction_failures == 1
    assert m.extraction_failure_rate == 0.5
    assert m.passages_returned == 1


def test_grounding_metrics_faithfulness():
    grounded = GroundedAnswer(
        answer_markdown="...",
        claims=[
            _vc("a", "supported"),
            _vc("b", "supported"),
            _vc("c", "weak"),
            _vc("d", "unsupported"),
        ],
        passages=[],
        all_hits=[],
        unsupported_count=1,
    )
    m = grounding_metrics(grounded)
    assert m.total_claims == 4 and m.supported == 2 and m.weak == 1 and m.unsupported == 1
    assert m.faithfulness == 0.5  # supported / total


def test_cited_passage_rerank_positions():
    result = RetrievalResult(
        passages=[
            Passage(id="p1", source_url="u", source_title="t", text="x"),
            Passage(id="p2", source_url="u", source_title="t", text="y"),
        ],
        all_hits=[],
        extracted=[],
        issued_queries=["q"],
    )
    grounded = GroundedAnswer(
        answer_markdown="...",
        claims=[],
        passages=[result.passages[1]],  # cited p2
        all_hits=[],
        unsupported_count=0,
    )
    assert cited_passage_rerank_positions(result, grounded) == {"p2": 1}


def test_select_for_judging_is_deterministic():
    keys = [f"q{i}" for i in range(10)]
    assert select_for_judging(keys, every=5) == ["q0", "q5"]
    assert select_for_judging(keys, every=1) == keys  # sample everything
