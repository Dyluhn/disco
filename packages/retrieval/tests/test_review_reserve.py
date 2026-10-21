"""An unresolved original draft cannot consume the changed-draft review reserve."""

import hashlib
import json

import pytest
from _writer_doubles import RecordingRouter, pool
from disco.retrieval.deep_research._citation_aliases import citation_aliases
from disco.retrieval.deep_research._stop import ResearchStopped
from disco.retrieval.deep_research._writer_checkpoint import WriterCheckpoint
from test_writer_recovery import CLEAN, FAILURE, FINAL, REPAIR, arc, assessed_verdict

SUMMARY_REPAIR = "## Executive summary\nThe measured evidence covers an observed window [[s1]]."


@pytest.mark.parametrize(
    "boundary", ["before_repair", "after_repair", "final_reserved", "legacy_final_check"]
)
async def test_initial_assessment_overflow_preserves_changed_draft_review_across_resume(boundary):
    saved = []

    def stop(state):
        if boundary in {"before_repair", "legacy_final_check"}:
            reached = state.stage == "repair" and not state.repair_started
        else:
            reached = state.stage == "final_check" and state.repair_started
            if boundary == "final_reserved":
                reached = reached and state.budget.used == 3
        if reached:
            raise ResearchStopped(boundary)

    initial = ['{"inspect":[{"source_id":"s1"}]}', assessed_verdict(uncertain=True)]
    first = RecordingRouter([*initial, SUMMARY_REPAIR, ("", "length"), CLEAN])
    with pytest.raises(ResearchStopped):
        await arc(first, saved, on_saved=stop)
    retained = WriterCheckpoint.model_validate_json(saved[-1].model_dump_json())
    assert retained.budget.limit == 4 and retained.budget.final_reserve == 2
    if boundary == "legacy_final_check":
        # The old arc entered final_check before repairing. That label must not
        # let a resumed original draft steal the two changed-draft decisions.
        retained.stage = "final_check"
        retained = WriterCheckpoint.model_validate_json(retained.model_dump_json())
    if boundary in {"before_repair", "legacy_final_check"}:
        assert retained.budget.used == 2 and retained.final == FINAL
        remainder = [SUMMARY_REPAIR, ("", "length"), CLEAN]
    elif boundary == "after_repair":
        assert retained.budget.used == 2 and retained.final != FINAL
        remainder = [("", "length"), CLEAN]
    else:
        # The first final-check reservation is durable even though it never
        # reached the provider. Resume may use only the one remaining decision.
        assert retained.budget.used == 3 and retained.final != FINAL
        assert first.stages == ["report_review", "report_review_reask", "report_rework"]
        remainder = [CLEAN]
    second = RecordingRouter(remainder)
    final, review, trail = await arc(second, saved, resume=retained)
    assert final.summary != FINAL.summary and final.sections == FINAL.sections
    assert review.outcome == "verdict" and not review.trail["assessment_errors"]
    assert saved[-1].budget.used == saved[-1].budget.limit == 4
    assert [state.budget.used for state in saved] == sorted(state.budget.used for state in saved)
    stages = first.stages + second.stages
    assert stages[:3] == ["report_review", "report_review_reask", "report_rework"]
    assert stages.count("report_rework") == 1
    assert stages[3:] == ["report_final_check"] * (1 if boundary == "final_reserved" else 2)
    final_context = second.prompt(second.calls - 1)
    assert citation_aliases(pool(1)).to_alias(final.body_markdown) in final_context
    assert review.trail["draft_sha256"] == hashlib.sha256(final.markdown.encode()).hexdigest()
    assert review.trail["draft_sha256"] != hashlib.sha256(FINAL.markdown.encode()).hexdigest()
    assert len([row for row in trail if row["kind"] == "report_rework"]) == 1
    replay = RecordingRouter([CLEAN])
    repeated, _, _ = await arc(replay, [], resume=saved[-1])
    assert replay.calls == 0 and repeated == final


@pytest.mark.parametrize("stage", ["repair", "final_check"])
async def test_legacy_exhausted_reserve_never_creates_unreviewable_repair(stage):
    saved = []
    await arc(RecordingRouter([FAILURE, REPAIR, CLEAN]), saved)
    before_repair = next(state for state in saved if state.stage == "repair")
    data = json.loads(before_repair.model_dump_json())
    data["stage"] = stage
    data["budget"]["used"] = data["budget"]["limit"]
    del data["budget"]["final_reserve"]
    legacy = WriterCheckpoint.model_validate(data)
    assert legacy.budget.final_reserve == 1
    router = RecordingRouter([REPAIR, CLEAN])
    retained = []
    final, review, trail = await arc(router, retained, resume=legacy)
    assert router.calls == 0 and final == FINAL
    assert review.outcome == "incomplete" and review.findings
    assert not retained[-1].repair_started and retained[-1].stage == "complete"
    assert retained[-1].budget.used == retained[-1].budget.limit == 4
    assert retained[-1].budget.final_reserve == 1
    assert any(row["kind"] == "report_rework_exhausted" for row in trail)
    assert not any(row["kind"] == "report_rework" for row in trail)
