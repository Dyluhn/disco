from __future__ import annotations

from disco.retrieval.deep_research.controller import ResearchController
from disco.retrieval.deep_research.depth import DepthBound, bounds_for
from disco.retrieval.deep_research.gather import SubQuestionResult


def _bound(*, max_subquestions: int = 6) -> DepthBound:
    return DepthBound(12, 2, 60, max_subquestions, 8, 4, 4, "standard")


def test_initial_plan_is_released_as_small_probe_batches() -> None:
    controller = ResearchController(_bound(), batch_size=2)
    controller.seed(["first", "second", "third", "fourth"])

    first = controller.next_batch()
    second = controller.next_batch()

    assert [probe.query for probe in first] == ["first", "second"]
    assert [probe.query for probe in second] == ["third", "fourth"]
    assert controller.next_batch() == []


def test_empty_evidence_pivots_once_within_probe_cap() -> None:
    controller = ResearchController(_bound(max_subquestions=2), batch_size=1)
    controller.seed(["question"])
    probe = controller.next_batch()[0]

    assessment = controller.assess(
        [SubQuestionResult(subq=probe.to_subquestion(), rounds_run=1)]
    )
    assert [action.kind for action in assessment.actions] == ["pivot"]
    follow_up = controller.next_batch()
    assert len(follow_up) == 1
    assert follow_up[0].origin == "pivot"


def test_overlapping_completed_probes_are_merged() -> None:
    controller = ResearchController(_bound(), batch_size=2)
    controller.seed(["shipping status of batteries", "shipping status batteries"])
    probes = controller.next_batch()
    results = [
        SubQuestionResult(subq=probe.to_subquestion(), rounds_run=1)
        for probe in probes
    ]

    assessment = controller.assess(results)
    assert any(action.kind == "merge" for action in assessment.actions)


def test_report_targets_are_part_of_the_depth_bound() -> None:
    assert bounds_for("quick").report_spec == {
        "min_words": 1500,
        "max_words": 2500,
        "reserved_writing_tokens": 4000,
    }
    assert bounds_for("standard_deep").report_spec == {
        "min_words": 4000,
        "max_words": 7000,
        "reserved_writing_tokens": 11200,
    }
    assert bounds_for("exhaustive").report_spec == {
        "min_words": 8000,
        "max_words": 12000,
        "reserved_writing_tokens": 19200,
    }


def test_unusable_passages_do_not_satisfy_evidence_capacity() -> None:
    bound = DepthBound(20, 2, 60, 4, 8, 4, 4, "standard", 100, 200, 400, 4, 2, 2)
    controller = ResearchController(bound, batch_size=1)
    controller.seed(["important topic"])
    probe = controller.next_batch()[0]
    from disco.retrieval.models import Passage

    result = SubQuestionResult(
        subq=probe.to_subquestion(),
        passages=[
            Passage(
                id="stub", text="Published: 2024", source_url="https://stub", source_title="stub"
            ),
            Passage(
                id="usable",
                text="Independent study reports a measurable result for this topic.",
                source_url="https://source",
                source_title="source",
            ),
        ],
        rounds_run=1,
    )
    controller.assess([result])
    capacity = controller.capacity()
    assert capacity.passages == 1
    assert not capacity.sufficient


def test_adaptive_pivots_never_overschedule_the_hard_cap() -> None:
    controller = ResearchController(_bound(max_subquestions=3), batch_size=2)
    controller.seed(["one", "two", "three"])
    first = controller.next_batch()
    controller.assess(
        [SubQuestionResult(subq=probe.to_subquestion(), rounds_run=1) for probe in first]
    )

    second = controller.next_batch()
    assert len(second) == 1
    assert controller.next_batch() == []
    assert controller.scheduled == 3


def test_pending_queries_include_interrupted_and_queued_pivots() -> None:
    controller = ResearchController(_bound(max_subquestions=3), batch_size=1)
    controller.seed(["one", "two"])
    first = controller.next_batch()[0]
    controller.assess([SubQuestionResult(subq=first.to_subquestion(), rounds_run=1)])

    assert "one primary sources evidence" in controller.pending_queries
    assert "two" in controller.pending_queries
