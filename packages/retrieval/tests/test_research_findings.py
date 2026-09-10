"""Research conclusions survive recovery and writing; review judges evidence independently."""

import json

import pytest
from disco.retrieval.deep_research._agent_state import _AgentState
from disco.retrieval.deep_research._budget import SourceBudget
from disco.retrieval.deep_research._recovery_state import AgentCheckpoint
from disco.retrieval.deep_research._review_context import quality_audit_context
from disco.retrieval.deep_research._turn_context import _coverage_feedback, _turn_user_message
from disco.retrieval.deep_research._turn_protocol import _parse_turn
from disco.retrieval.deep_research.agent import ResearchOutcome
from disco.retrieval.deep_research.depth import DepthBound
from disco.retrieval.deep_research.pool import load_pool, pool_document
from disco.retrieval.deep_research.writer import _report_instruction
from disco.retrieval.models import Passage

FINDING = (
    "The trial improved output in one greenhouse; the evidence does not establish "
    "a benefit in open fields or a global deployment total."
)


def turn_text(finding=FINDING):
    item = {"angle": "Measured benefit and applicability", "evidence_ids": ["p1"]}
    if finding is not None:
        item["finding"] = finding
    return json.dumps(
        {
            "brief": "Assess observed performance and its limits.",
            "decision_summary": "The field-performance question remains open.",
            "coverage": {
                "covered": [item],
                "open": ["Open-field performance"],
                "contradictions_checked": [],
            },
            "queries": ["open field trial performance"],
            "ready_to_write": False,
        }
    )


def test_turn_preserves_substantive_finding_and_its_scope():
    parsed, error = _parse_turn(turn_text(), expect_brief=True)
    assert error is None
    assert parsed.coverage["covered"][0]["finding"] == FINDING


def test_finding_survives_recovery_and_writing_without_anchoring_the_reviewer():
    parsed, error = _parse_turn(turn_text(), expect_brief=True)
    assert error is None
    bound = DepthBound(
        max_sources=8,
        max_rounds_per_subq=2,
        max_research_turns=8,
        max_subquestions=4,
        discover_limit=5,
        extract_cap=4,
        rerank_top_k=4,
    )
    passage = Passage(
        id="p1",
        source_url="https://example.test/trial",
        source_title="Trial",
        text="One greenhouse trial measured improved output.",
    )
    state = _AgentState(budget=SourceBudget(8), coverage=parsed.coverage, brief=parsed.brief)
    state.admit_exempt([passage])
    saved = AgentCheckpoint.capture(state)
    restored = AgentCheckpoint.model_validate_json(saved.model_dump_json()).restore(bound)
    outcome = ResearchOutcome(
        brief=restored.brief,
        passages=restored.pool,
        all_hits=[],
        trail=[],
        bounded_by=None,
        coverage=restored.coverage,
    )
    loaded = load_pool(
        pool_document(
            "findings",
            query="Assess benefits",
            depth_tier="quick",
            recency_window=None,
            outcome=outcome,
        )
    ).outcome
    contexts = [
        _turn_user_message(
            "Assess benefits", restored, [], turns_left=3, total_turns=8, bound=bound
        ),
        _report_instruction("Assess benefits", loaded, None),
    ]
    for context in contexts:
        assert FINDING in context
        assert "Open-field performance" in context
        assert "p1" in context
    assert saved.coverage == loaded.coverage == parsed.coverage
    review = quality_audit_context(
        [],
        [],
        {"p1": passage},
        query="Assess benefits",
        coverage={**loaded.coverage, "contradictions_checked": ["Provisional dispute resolved."]},
        trail=[{"kind": "steer", "text": "Distinguish laboratory and field results."}],
    )
    assert FINDING not in review and "Provisional dispute resolved." not in review
    assert "Measured benefit and applicability" in review and "Open-field performance" in review
    assert "p1" in review and passage.text in review
    assert "Assess benefits" in review and "Distinguish laboratory and field results." in review


@pytest.mark.parametrize("bad_finding", [17, ["a finding"], {"claim": "a finding"}])
def test_malformed_finding_is_reported_instead_of_silently_dropped(bad_finding):
    parsed, error = _parse_turn(turn_text(bad_finding), expect_brief=True)
    assert parsed is None
    assert "finding" in error


def test_legacy_coverage_remains_readable_and_requests_missing_analysis():
    parsed, error = _parse_turn(turn_text(None), expect_brief=True)
    assert error is None
    assert parsed.coverage["covered"] == [
        {"angle": "Measured benefit and applicability", "evidence_ids": ["p1"]}
    ]
    state = _AgentState(budget=SourceBudget(8), coverage=parsed.coverage)
    state.seen_ids.add("p1")
    feedback = _coverage_feedback(state)
    assert "finding" in feedback.lower()
    assert "Measured benefit and applicability" in feedback
