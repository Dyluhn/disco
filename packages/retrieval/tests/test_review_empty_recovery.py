"""Empty reviews recover without losing evidence, replaying blanks, or resetting limits."""

import json

import pytest
from _writer_doubles import RecordingRouter
from disco.retrieval.deep_research._review_protocol import ReviewBudget
from disco.retrieval.deep_research._stop import ResearchStopped
from disco.retrieval.deep_research._writer_review import _model_review
from disco.retrieval.deep_research.depth import bounds_for
from test_writer_recovery import CLEAN, FAILURE, REPAIR, arc, reload_budget


@pytest.mark.parametrize("empty", ["", " \n", "<think>unfinished review</think>"])
async def test_empty_review_preserves_context_without_an_empty_assistant_turn(empty):
    router = RecordingRouter([(empty, "length"), CLEAN])
    budget = ReviewBudget(3)
    verdict, attempts = await _model_review(
        router, "exact draft", "exact evidence", conversation_id=None, budget=budget, reserve=1
    )
    assert verdict == json.loads(CLEAN) and attempts == 2
    assert budget.concise_review
    assert all(m.role != "assistant" for m in router.requests[1].messages)
    assert "exact draft" in router.prompt(1) and "exact evidence" in router.prompt(1)
    assert "Do not rehearse" in router.prompt(1)
    assert budget.used == 2 and budget.remaining == 1


async def test_empty_first_review_and_final_check_both_recover_in_one_bounded_arc():
    router = RecordingRouter([("", "length"), FAILURE, REPAIR, ("", "length"), CLEAN])
    saved = []
    _, review, _ = await arc(router, saved)
    assert review.outcome == "verdict"
    assert router.stages == [
        "report_review",
        "report_review_reask",
        "report_rework",
        "report_final_check",
        "report_final_check",
    ]
    assert saved[-1].budget.used == saved[-1].budget.limit == 4
    assert bounds_for("quick").review_decisions == 4
    assert all("Do not rehearse" in router.prompt(i) for i in (1, 3, 4))
    assert all(m.content.strip() for m in router.requests[-1].messages)


async def test_final_empty_retry_survives_stop_and_checkpoint_resume():
    saved = []
    first = RecordingRouter([("", "length"), FAILURE, REPAIR, ("", "length")])
    with pytest.raises(ResearchStopped):
        await arc(first, saved, stop=lambda: first.calls == 4)
    boundary = saved[-1]
    assert boundary.budget.used == 3 and boundary.budget.concise_review
    second = RecordingRouter([CLEAN])
    _, review, _ = await arc(second, saved, resume=boundary)
    assert review.outcome == "verdict" and second.calls == 1
    assert saved[-1].budget.used == 4
    assert "Do not rehearse" in second.prompt(0)
    third = RecordingRouter([CLEAN])
    await arc(third, [], resume=saved[-1])
    assert third.calls == 0


async def test_repeated_final_empty_exhausts_bound_without_certifying_or_repairing_again():
    router = RecordingRouter(
        [("", "length"), FAILURE, REPAIR, ("", "length"), ("", "length"), CLEAN]
    )
    saved = []
    _, review, _ = await arc(router, saved)
    assert review.outcome == "unavailable"
    assert router.calls == 5 and saved[-1].budget.used == 4
    assert router.stages.count("report_rework") == 1


async def test_legacy_allowance_is_not_expanded_on_resume():
    budget = reload_budget(ReviewBudget(1))
    router = RecordingRouter([("", "length"), CLEAN])
    verdict, _ = await _model_review(
        router, "draft", "evidence", conversation_id=None, budget=budget, final_check=True
    )
    assert verdict is None and router.calls == 1 and budget.limit == budget.used == 1


async def test_visible_malformed_reply_is_retained_for_protocol_repair():
    router = RecordingRouter(["{bad json", CLEAN])
    budget = ReviewBudget(2)
    await _model_review(router, "draft", "evidence", conversation_id=None, budget=budget)
    assert not budget.concise_review
    assert any(
        m.role == "assistant" and m.content == "{bad json" for m in router.requests[1].messages
    )
