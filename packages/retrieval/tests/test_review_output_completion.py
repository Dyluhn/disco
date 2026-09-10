"""A provider-cutoff prefix is not a completed review decision."""

import hashlib
import json

import pytest
from _writer_doubles import RecordingRouter, pool
from disco.retrieval.deep_research._output_ceiling import RESEARCH_CEILING_CAP
from disco.retrieval.deep_research._review_protocol import ReviewBudget
from disco.retrieval.deep_research._stop import ResearchStopped
from disco.retrieval.deep_research._writer_checkpoint import WriterCheckpoint
from disco.retrieval.deep_research._writer_parts import FinalReport
from disco.retrieval.deep_research._writer_review import _model_review

CLEAN = {"passes": True, "failures": []}


@pytest.mark.parametrize("prefix", [CLEAN, {"inspect": [{"source_id": "s1", "start": 0}]}])
async def test_cutoff_prefix_does_not_apply_verdict_or_inspection(prefix):
    router = RecordingRouter([(json.dumps(prefix), "length"), json.dumps(CLEAN)])
    budget = ReviewBudget(3)
    assessed = []

    def assess(payload):
        assessed.append(payload)
        return []

    verdict, attempts = await _model_review(
        router,
        "draft",
        "context",
        conversation_id=None,
        sources={"s1": pool(1)[0]},
        budget=budget,
        reserve=1,
        assess=assess,
    )

    assert verdict == CLEAN and attempts == 2
    assert assessed == [CLEAN]
    assert budget.remaining == 1 and budget.inspections == []
    assert "cut off" in router.last_message(1)
    assert "not applied" in router.last_message(1)
    assert [request.max_tokens for request in router.requests] == [28000, 32000]
    assert budget.output_ceiling == 32000
    assert "larger output allowance" in router.last_message(1)


async def test_final_cutoff_cannot_certify_report_or_start_extra_call():
    router = RecordingRouter([(json.dumps(CLEAN), "length"), json.dumps(CLEAN)])
    budget = ReviewBudget(1)

    verdict, attempts = await _model_review(
        router,
        "draft",
        "context",
        conversation_id=None,
        budget=budget,
    )

    assert verdict is None and attempts == 1
    assert budget.last_verdict is None and not budget.complete
    assert router.calls == 1 and budget.remaining == 0


async def test_review_cutoff_at_hard_cap_keeps_capacity_and_the_same_decision_bound():
    budget = ReviewBudget(2, output_ceiling=RESEARCH_CEILING_CAP)
    router = RecordingRouter([(json.dumps(CLEAN), "length"), json.dumps(CLEAN)])
    verdict, attempts = await _model_review(
        router, "draft", "context", conversation_id=None, budget=budget
    )
    assert verdict == CLEAN and attempts == 2 and budget.remaining == 0
    assert [request.max_tokens for request in router.requests] == [48000, 48000]
    assert budget.output_ceiling == RESEARCH_CEILING_CAP
    assert "hard cap" in router.last_message(1)
    assert "larger output allowance" not in router.last_message(1)


async def test_complete_malformed_review_does_not_grow_output_capacity():
    budget = ReviewBudget(2)
    router = RecordingRouter(["{}", json.dumps(CLEAN)])
    verdict, _ = await _model_review(
        router, "draft", "context", conversation_id=None, budget=budget
    )
    assert verdict == CLEAN and router.calls == 2
    assert [request.max_tokens for request in router.requests] == [28000, 28000]
    assert budget.output_ceiling == 28000


async def test_resume_after_cutoff_preserves_grown_capacity_and_reserved_decision():
    budget = ReviewBudget(3)
    final = FinalReport(title="", summary="draft", sections=())
    saved = []

    async def save():
        state = WriterCheckpoint(
            final=final,
            draft_sha256=hashlib.sha256(final.markdown.encode()).hexdigest(),
            stage="review",
            budget=budget,
        )
        saved.append(WriterCheckpoint.model_validate_json(state.model_dump_json()))

    first = RecordingRouter([("", "length")])
    with pytest.raises(ResearchStopped):
        await _model_review(
            first,
            final.markdown,
            "context",
            conversation_id=None,
            budget=budget,
            reserve=1,
            checkpoint=save,
            should_cancel=lambda: first.calls == 1,
        )
    resumed = saved[-1].budget
    assert resumed.output_ceiling == 32000 and resumed.used == 1
    second = RecordingRouter([json.dumps(CLEAN)])
    verdict, attempts = await _model_review(
        second, final.markdown, "context", conversation_id=None, budget=resumed, reserve=1
    )
    assert verdict == CLEAN and attempts == 2
    assert second.calls == 1 and second.requests[0].max_tokens == 32000
    assert resumed.used == 2 and resumed.remaining == 1
    assert "larger output allowance" in second.prompt(0)


def test_old_writer_checkpoint_defaults_to_unprovisioned_review_capacity():
    final = FinalReport(title="", summary="draft", sections=())
    state = WriterCheckpoint(
        final=final,
        draft_sha256=hashlib.sha256(final.markdown.encode()).hexdigest(),
        stage="review",
        budget=ReviewBudget(3),
    )
    data = json.loads(state.model_dump_json())
    del data["budget"]["output_ceiling"]
    restored = WriterCheckpoint.model_validate_json(json.dumps(data))
    assert restored.budget.output_ceiling == 0 and restored.budget.remaining == 3


@pytest.mark.parametrize("capacity", [-1, RESEARCH_CEILING_CAP + 1])
def test_writer_checkpoint_rejects_capacity_outside_the_host_bound(capacity):
    from pydantic import ValidationError

    final = FinalReport(title="", summary="draft", sections=())
    with pytest.raises(ValidationError, match="review output capacity"):
        WriterCheckpoint(
            final=final,
            draft_sha256=hashlib.sha256(final.markdown.encode()).hexdigest(),
            stage="review",
            budget=ReviewBudget(3, output_ceiling=capacity),
        )
