"""Review decisions spend one shared allowance and cannot certify changed prose."""

import hashlib
import json

import pytest
from _writer_doubles import RecordingRouter, collect_emits, pool
from disco.core.llm import LLMTransientError
from disco.retrieval.deep_research._citation_aliases import citation_aliases
from disco.retrieval.deep_research._review_protocol import ReviewBudget
from disco.retrieval.deep_research._stop import ResearchStopped
from disco.retrieval.deep_research._writer_parts import FinalReport
from disco.retrieval.deep_research._writer_review import _model_review
from disco.retrieval.deep_research.writer import _review_arc
from test_deep_research import _FakeNLI

CHECKS = [
    {
        "claim_id": "c1",
        "judgment": "supported",
        "evidence_standard": "met",
        "evidence": [{"source_id": "s1", "start": 0, "end": len(pool(1)[0].text)}],
        "reason": "The retained source establishes the measured evidence.",
    }
]
CLEAN = json.dumps({"passes": True, "failures": [], "checks": CHECKS})


async def test_inspection_reveals_qualification_and_preserves_reserved_final_decision():
    passage = pool(1)[0].model_copy(update={"text": "Intro. " * 1000 + "Only a design target."})
    budget = ReviewBudget(3)
    router = RecordingRouter(
        [
            json.dumps({"inspect": [{"source_id": "s1", "start": 7000}]}),
            CLEAN,
        ]
    )
    payload, attempts = await _model_review(
        router,
        "draft",
        "context",
        conversation_id=None,
        sources={"s1": passage},
        budget=budget,
        reserve=1,
    )
    assert payload == json.loads(CLEAN) and attempts == 2
    assert budget.remaining == 1
    assert "Only a design target." in router.last_message(1)
    row = budget.inspections[0]
    assert row["text"] == passage.text[row["start"] : row["end"]]
    assert row["source_sha256"] == hashlib.sha256(passage.text.encode()).hexdigest()
    assert "Inspection is no longer available" in router.last_message(1)
    assert '{"inspect":' not in router.last_message(1)


@pytest.mark.parametrize(
    "lookup,error",
    [
        ({"source_id": "missing"}, "not in the admitted pool"),
        ({"source_id": "s1", "start": 10000}, "outside the source"),
    ],
)
async def test_invalid_source_lookup_returns_actionable_observation(lookup, error):
    router = RecordingRouter([json.dumps({"inspect": [lookup]}), CLEAN])
    payload, _ = await _model_review(
        router,
        "draft",
        "context",
        conversation_id=None,
        sources={"s1": pool(1)[0]},
        budget=ReviewBudget(3),
        reserve=1,
    )
    assert payload == json.loads(CLEAN)
    assert error in router.last_message(1)


@pytest.mark.parametrize(
    "reply",
    [
        "{}",
        '{"passes":true,"failures":"none"}',
        '{"passes":true,"failures":[],"inspect":[]}',
        '{"inspect":[{"source_id":"s1"}]}',
    ],
)
async def test_invalid_or_repeated_inspection_cannot_spend_reserved_decision(reply):
    router = RecordingRouter([reply] * 5)
    budget = ReviewBudget(3)
    payload, _ = await _model_review(
        router,
        "draft",
        "context",
        conversation_id=None,
        sources={"s1": pool(1)[0]},
        budget=budget,
        reserve=1,
    )
    assert payload is None and router.calls == 2 and budget.remaining == 1


async def test_stop_after_inspection_does_not_make_another_provider_call():
    router = RecordingRouter(['{"inspect":[{"source_id":"s1"}]}', CLEAN])
    with pytest.raises(ResearchStopped):
        await _model_review(
            router,
            "draft",
            "context",
            conversation_id=None,
            sources={"s1": pool(1)[0]},
            budget=ReviewBudget(3),
            should_cancel=lambda: router.calls == 1,
        )
    assert router.calls == 1


@pytest.mark.parametrize(
    "final_reply,expected", [(CLEAN, "verdict"), (LLMTransientError("offline"), "unavailable")]
)
async def test_changed_report_has_its_own_verdict_and_one_repair(final_reply, expected):
    passages = pool(1)
    final = FinalReport(
        title="",
        summary="Measured evidence documents figures [[p1]].",
        sections=(("Findings", "Collected data frame the observed window [[p1]]."),),
    )
    failure = json.dumps(
        {
            "passes": False,
            "checks": CHECKS,
            "failures": [
                {
                    "rubric": "R9",
                    "section": "Findings",
                    "where": "Collected data frame the observed window",
                    "fix": "Clarify the comparison.",
                }
            ],
        }
    )
    router = RecordingRouter(
        [
            failure,
            "## Findings\nMeasured evidence and reported figures "
            "document the observed window [[s1]].",
            final_reply,
        ]
    )
    _, emit = collect_emits()
    written, review, trail = await _review_arc(
        router,
        [],
        final,
        query="Compare measured evidence.",
        coverage={},
        research_trail=[],
        by_id={p.id: p for p in passages},
        nli=_FakeNLI(),
        aliases=citation_aliases(passages),
        untested=[],
        max_tokens=4000,
        conversation_id="review-test",
        emit=emit,
        should_cancel=None,
    )
    assert written.markdown != final.markdown
    assert review.outcome == expected
    assert router.stages == ["report_review", "report_rework", "report_final_check"]
    assert "FINAL REPAIR VALIDATION" in router.prompt(2)
    assert '"applied": ["Findings"]' in router.prompt(2)
    assert "FINAL REPAIR VALIDATION" not in router.prompt(0)
    assert trail[-1]["draft_sha256"] == hashlib.sha256(written.markdown.encode()).hexdigest()
    assert trail[-1]["draft_sha256"] != trail[0]["draft_sha256"]
