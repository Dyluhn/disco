"""Grounding & NLI — retrieval-grounding-contract.md §8.4 (the faithfulness tests)."""

from __future__ import annotations

from disco.retrieval import (
    CrossEncoderNLIVerifier,
    GroundingPipeline,
    Passage,
    RetrievalResult,
    extract_claims,
)
from research_fakes import FakeNLI, FakeRouter


def _passages():
    return [
        Passage(id="p1", source_url="u1", source_title="t1", text="Cats are mammals."),
        Passage(id="p2", source_url="u2", source_title="t2", text="The sky is blue."),
    ]


def _retrieval():
    return RetrievalResult(passages=_passages(), all_hits=[], extracted=[], issued_queries=["q"])


# ---- claim extraction -------------------------------------------------------


def test_extract_claims_splits_on_citations():
    claims = extract_claims("Cats are mammals [p1]. The moon is cheese [p2].")
    assert [c.text for c in claims] == ["Cats are mammals", "The moon is cheese"]
    assert claims[0].cited_passage_ids == ["p1"]


def test_case_name_v_is_not_a_sentence_boundary():
    """``Bartz v. Anthropic`` stays one sentence; the verifier must never hand
    the writer a fragment like ``In Bartz v`` to cite (phase-6 run-02)."""
    from disco.retrieval.grounding import _split_sentences

    text = (
        "In Bartz v. Anthropic the court found fair use [[s1]]. "
        "Kadrey v. Meta went the same way [[s2]]."
    )
    assert [s.strip() for s in _split_sentences(text)] == [
        "In Bartz v. Anthropic the court found fair use [[s1]].",
        "Kadrey v. Meta went the same way [[s2]].",
    ]
    assert _split_sentences("| Kadrey v. Meta | dismissed [[s3]] |") == [
        "| Kadrey v. Meta | dismissed [[s3]] |"
    ]


# ---- NLI verdicts + self-correction -----------------------------------------


async def test_verdicts_and_self_correction_drop_unsupported():
    router = FakeRouter(answerer_text="Cats are mammals [p1]. The moon is cheese [p2].")
    nli = FakeNLI({"Cats are mammals": "entail", "The moon is cheese": "contradict"})
    gp = GroundingPipeline(router, nli, strictness="drop")
    ans = await gp.answer("tell me facts", _retrieval())

    verdicts = {v.claim.text: v.verdict for v in ans.claims}
    assert verdicts["Cats are mammals"] == "supported"
    assert verdicts["The moon is cheese"] == "unsupported"
    assert ans.unsupported_count == 1
    # self-correction: the unsupported claim is dropped from the rendered answer…
    assert "moon is cheese" not in ans.answer_markdown
    assert "Cats are mammals" in ans.answer_markdown
    # …but is still SURFACED honestly in claims (not hidden, §13.3).
    assert any(v.verdict == "unsupported" for v in ans.claims)


async def test_weak_verdict_for_borderline():
    router = FakeRouter(answerer_text="Grass is sometimes green [p1].")
    nli = FakeNLI({"Grass is sometimes green": "neutral"})
    ans = await GroundingPipeline(router, nli).answer("q", _retrieval())
    assert ans.claims[0].verdict == "weak"
    assert ans.unsupported_count == 0


async def test_verification_is_not_the_llm_path():
    """Generation calls the router once; verification uses the NLI side-car —
    NOT the router's complete() (router §9.2)."""
    router = FakeRouter(answerer_text="A [p1]. B [p2].")
    nli = FakeNLI({"A": "entail", "B": "entail"})
    await GroundingPipeline(router, nli).answer("q", _retrieval())
    assert router.complete_calls == 1  # only generation
    assert nli.calls >= 2  # one entailment per claim, off the LLM path


# ---- the CrossEncoderNLIVerifier stub itself --------------------------------


def test_cross_encoder_nli_buckets():
    v = CrossEncoderNLIVerifier()
    assert v.entail("cats are mammals and pets", "cats are mammals") == "entail"
    assert v.entail("entirely unrelated text here", "zzz qqq www") == "contradict"
    assert v.score("cats are mammals", "cats are mammals") == 1.0
