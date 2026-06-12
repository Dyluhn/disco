"""Eval harness — the scoring + baseline-gate logic (pure, fast). The full-pipeline
`--replay` eval needs the captured research cassette (harness/_capture_research_demo);
this file covers the scoring contract deterministically.

Run: PYTHONPATH=. uv run pytest harness/tests/test_eval_runner.py
"""

from __future__ import annotations

from disco.retrieval.models import (
    Claim,
    GroundedAnswer,
    Passage,
    VerifiedClaim,
)

from harness.eval_runner import diff_baseline, score_research


def _claim(text: str, verdict: str) -> VerifiedClaim:
    return VerifiedClaim(
        claim=Claim(text=text, cited_passage_ids=[]),
        verdict=verdict,
        best_passage_id=None,
        entailment_score=0.5,
    )


def _answer(verdicts: list[str], urls: list[str]) -> GroundedAnswer:
    return GroundedAnswer(
        answer_markdown="an answer",
        claims=[_claim(f"c{i}", v) for i, v in enumerate(verdicts)],
        passages=[
            Passage(id=f"p{i}", source_url=u, source_title="t", text="x")
            for i, u in enumerate(urls)
        ],
        all_hits=[],
        unsupported_count=sum(1 for v in verdicts if v == "unsupported"),
    )


def test_faithfulness_gate():
    ans = _answer(["supported", "supported", "weak", "unsupported"], ["https://x.test"])
    # 2/4 supported → faithfulness 0.5
    assert score_research(ans, {"min_faithfulness": 0.5})["faithfulness"] == 0.5
    assert score_research(ans, {"min_faithfulness": 0.5})["passed"] is True
    assert score_research(ans, {"min_faithfulness": 0.7})["passed"] is False  # below the gate


def test_must_cite_domains():
    ans = _answer(["supported"], ["https://github.com/kevin1024/vcrpy"])
    assert score_research(ans, {"min_faithfulness": 0.0, "must_cite_domains": ["github.com"]})[
        "domains_ok"
    ]
    assert not score_research(ans, {"min_faithfulness": 0.0, "must_cite_domains": ["nope.org"]})[
        "domains_ok"
    ]


def test_empty_answer_never_passes():
    # zero claims must NOT silently pass the gate (faithfulness defaults to 1.0 but
    # total_claims==0 fails the gate — an empty answer isn't a good answer)
    ans = _answer([], ["https://x.test"])
    assert score_research(ans, {"min_faithfulness": 0.0})["passed"] is False


def test_baseline_catches_faithfulness_regression(tmp_path, monkeypatch):
    import harness.eval_runner as er

    monkeypatch.setattr(er, "_EVALS", tmp_path)
    (tmp_path / "baselines").mkdir()
    (tmp_path / "baselines" / "scorecard.json").write_text(
        '{"t": {"faithfulness": 0.9, "passed": true}}'
    )
    # a current score well below baseline → flagged as a regression
    ok, regressions = diff_baseline({"t": {"faithfulness": 0.7, "passed": True}})
    assert not ok and any("dropped" in r for r in regressions)
