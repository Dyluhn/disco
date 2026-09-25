"""Resume committed writer work without losing evidence or resetting allowances."""

import hashlib
import json

import pytest
from _writer_doubles import RecordingRouter, collect_emits, outcome, pool
from disco.retrieval.deep_research._citation_aliases import citation_aliases
from disco.retrieval.deep_research._review_protocol import ReviewBudget
from disco.retrieval.deep_research._stop import ResearchStopped
from disco.retrieval.deep_research._writer_checkpoint import WriterCheckpoint
from disco.retrieval.deep_research._writer_parts import FinalReport
from disco.retrieval.deep_research._writer_review import _model_review
from disco.retrieval.deep_research.depth import bounds_for
from disco.retrieval.deep_research.report_compiler import ReportCompilationError
from disco.retrieval.deep_research.writer import _review_arc, write_report
from pydantic import TypeAdapter, ValidationError
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
FINAL = FinalReport(
    title="",
    summary="Measured evidence documents figures [[p1]].",
    sections=(("Findings", "Collected data frame the observed window [[p1]]."),),
)
FAILURE = json.dumps(
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
REPAIR = "## Findings\nMeasured evidence and reported figures document the observed window [[s1]]."


def reload_budget(budget):
    adapter = TypeAdapter(ReviewBudget)
    return adapter.validate_json(adapter.dump_json(budget))


async def arc(
    router, saved, *, resume=None, stop=None, review_decisions=4, events=None, on_saved=None
):
    async def save(state):
        saved.append(WriterCheckpoint.model_validate_json(state.model_dump_json()))
        if on_saved is not None:
            on_saved(saved[-1])

    captured, capture = collect_emits()

    async def emit(kind, payload):
        await capture(kind, payload)
        if events is not None:
            events.append(captured[-1])

    return await _review_arc(
        router,
        [],
        FINAL,
        query="Compare measured evidence.",
        coverage={},
        research_trail=[],
        by_id={p.id: p for p in pool(1)},
        nli=_FakeNLI(),
        aliases=citation_aliases(pool(1)),
        untested=[],
        max_tokens=4000,
        conversation_id="writer-recovery-test",
        emit=emit,
        should_cancel=stop,
        checkpoint=save,
        resume=resume,
        review_decisions=review_decisions,
    )


@pytest.mark.parametrize("selector", [{}, {"find": "Measured"}])
async def test_resume_after_inspection_keeps_exact_observation_and_remaining_budget(selector):
    budget = ReviewBudget(3)
    snapshots = []

    async def save():
        snapshots.append(reload_budget(budget))

    first = RecordingRouter([json.dumps({"inspect": [{"source_id": "s1", **selector}]})])
    with pytest.raises(ResearchStopped):
        await _model_review(
            first,
            "draft",
            "context",
            conversation_id="writer-recovery-test",
            sources={"s1": pool(1)[0]},
            budget=budget,
            reserve=1,
            checkpoint=save,
            should_cancel=lambda: first.calls == 1,
        )
    resumed = snapshots[-1]
    second = RecordingRouter([CLEAN])
    verdict, attempts = await _model_review(
        second,
        "draft",
        "context",
        conversation_id=None,
        sources={"s1": pool(1)[0]},
        budget=resumed,
        reserve=1,
    )
    assert verdict == json.loads(CLEAN) and attempts == 2
    assert resumed.used == 2 and resumed.remaining == 1 and second.calls == 1
    assert resumed.inspections == budget.inspections
    assert pool(1)[0].text in second.prompt(0)


async def test_crash_after_reserved_decision_does_not_reset_it():
    budget = ReviewBudget(3)
    snapshots = []

    async def crash():
        snapshots.append(reload_budget(budget))
        raise RuntimeError("process ended after commit")

    first = RecordingRouter([CLEAN])
    with pytest.raises(RuntimeError, match="after commit"):
        await _model_review(
            first,
            "draft",
            "context",
            conversation_id=None,
            budget=budget,
            reserve=1,
            checkpoint=crash,
        )
    assert first.calls == 0 and snapshots[-1].used == 1
    second = RecordingRouter(["{}", CLEAN])
    verdict, _ = await _model_review(
        second, "draft", "context", conversation_id=None, budget=snapshots[-1], reserve=1
    )
    assert verdict is None and second.calls == 1 and snapshots[-1].used == 2


async def test_committed_verdict_is_reused_without_another_call():
    saved = []
    first = RecordingRouter([CLEAN])
    with pytest.raises(ResearchStopped):
        await arc(first, saved, stop=lambda: first.calls == 1)
    assert saved[-1].stage == "review" and saved[-1].budget.complete
    second = RecordingRouter()
    _, review, _ = await arc(second, saved, resume=saved[-1])
    assert second.calls == 0 and saved[-1].stage == "complete"
    # A complete, evidence-assessed verdict remains reusable after resume.
    assert review.outcome == "verdict" and not review.trail["assessment_errors"]


async def test_exhausted_cached_invalid_assessment_cannot_become_clean():
    budget = ReviewBudget(1)
    router = RecordingRouter(['{"passes":true,"failures":[],"checks":[]}'])
    errors = []

    def assess(payload):
        errors.append("invalid checks")
        return ["invalid checks"]

    await _model_review(
        router, "draft", "context", conversation_id=None, budget=budget, assess=assess
    )
    errors.clear()
    replay = RecordingRouter()
    await _model_review(
        replay,
        "draft",
        "context",
        conversation_id=None,
        budget=reload_budget(budget),
        assess=assess,
    )
    assert errors == ["invalid checks"] and replay.calls == 0


async def test_resume_after_repair_reviews_saved_changed_draft_once():
    saved = []
    first = RecordingRouter([FAILURE, REPAIR])
    with pytest.raises(ResearchStopped):
        await arc(first, saved, stop=lambda: first.calls == 2)
    boundary = saved[-1]
    assert boundary.stage == "final_check" and boundary.final.markdown != FINAL.markdown
    second = RecordingRouter([CLEAN])
    events = []
    final, _, trail = await arc(second, saved, resume=boundary, events=events)
    assert [p for kind, p in events if kind == "review"] == [
        {"k": 1, "of": 1, "after_rework": True}
    ]
    assert second.stages == ["report_final_check"]
    context = second.prompt(0)
    assert "PRIOR REVIEW OBSERVATIONS" in context
    assert "Clarify the comparison." in context
    assert CHECKS[0]["reason"] in context
    record = next(row for row in trail if row["kind"] == "report_rework")
    assert record["requested_findings"][0]["fix"] == "Clarify the comparison."
    assert final.markdown == boundary.final.markdown
    assert trail[-1]["draft_sha256"] == hashlib.sha256(final.markdown.encode()).hexdigest()
    assert saved[-1].budget.used == 2
    third = RecordingRouter()
    repeated, _, repeated_trail = await arc(third, [], resume=saved[-1])
    assert third.calls == 0 and repeated == final and repeated_trail == trail


async def test_interrupted_repair_keeps_findings_and_does_not_retry():
    saved = []

    async def crash(state):
        saved.append(WriterCheckpoint.model_validate_json(state.model_dump_json()))
        if state.repair_started:
            raise RuntimeError("repair request reservation committed")

    _, emit = collect_emits()
    first = RecordingRouter([FAILURE])
    with pytest.raises(RuntimeError):
        await _review_arc(
            first,
            [],
            FINAL,
            query="Compare measured evidence.",
            coverage={},
            research_trail=[],
            by_id={p.id: p for p in pool(1)},
            nli=_FakeNLI(),
            aliases=citation_aliases(pool(1)),
            untested=[],
            max_tokens=4000,
            conversation_id="writer-recovery-test",
            emit=emit,
            should_cancel=None,
            checkpoint=crash,
        )
    second = RecordingRouter()
    final, review, trail = await arc(second, [], resume=saved[-1])
    assert first.stages == ["report_review"] and second.calls == 0
    assert final == FINAL and review.findings and review.outcome == "incomplete"
    assert trail[-1]["kind"] == "report_rework_interrupted"


async def test_entrypoint_resumes_without_redrafting_and_rejects_changed_source():
    saved = []

    async def save(state):
        saved.append(WriterCheckpoint.model_validate_json(state.model_dump_json()))

    _, emit = collect_emits()
    options = dict(nli=_FakeNLI(), bound=bounds_for("quick"), emit=emit, checkpoint=save)
    first = RecordingRouter([FINAL.markdown, CLEAN])
    with pytest.raises(ResearchStopped):
        await write_report(
            "Compare measured evidence.",
            outcome(pool(1)),
            router=first,
            should_cancel=lambda: first.calls == 2,
            **options,
        )
    second = RecordingRouter()
    written = await write_report(
        "Compare measured evidence.", outcome(pool(1)), router=second, resume=saved[-1], **options
    )
    assert second.calls == 0 and written.summary == FINAL.summary
    changed = pool(1)[0].model_copy(update={"text": "Different source bytes."})
    with pytest.raises(ReportCompilationError, match="inputs differ"):
        await write_report(
            "Compare measured evidence.",
            outcome([changed]),
            router=second,
            resume=saved[-1],
            **options,
        )
    assert second.calls == 0


@pytest.mark.parametrize(
    "field,value", [("draft_sha256", "0" * 64), ("budget", {"limit": 2, "used": 3})]
)
def test_invalid_checkpoint_refuses_incoherent_boundary(field, value):
    state = WriterCheckpoint(
        final=FINAL,
        draft_sha256=hashlib.sha256(FINAL.markdown.encode()).hexdigest(),
        stage="review",
        budget=ReviewBudget(3),
    )
    data = json.loads(state.model_dump_json())
    data[field] = value
    with pytest.raises(ValidationError):
        WriterCheckpoint.model_validate(data)


@pytest.mark.parametrize("spent", [1, bounds_for("quick").review_decisions])
async def test_new_user_guidance_revises_draft_without_resetting_review_budget(spent):
    saved = []

    async def save(state):
        saved.append(WriterCheckpoint.model_validate_json(state.model_dump_json()))

    _, emit = collect_emits()
    options = dict(nli=_FakeNLI(), bound=bounds_for("quick"), emit=emit, checkpoint=save)
    first = RecordingRouter([FINAL.markdown, CLEAN])
    await write_report("Compare measured evidence.", outcome(pool(1)), router=first, **options)
    previous = saved[-1]
    previous.budget.used = spent
    revised = RecordingRouter([FINAL.markdown, CLEAN])
    result = await write_report(
        "Compare measured evidence. Focus on the observed window.",
        outcome(pool(1)),
        router=revised,
        resume=previous,
        **options,
    )
    assert "Focus on the observed window" in revised.prompt(0)
    assert revised.calls == (2 if spent == 1 else 1)
    assert saved[-1].budget.used == (2 if spent == 1 else bounds_for("quick").review_decisions)
    assert any(row["kind"] == "report_task_revision" for row in result.trail)
    assert saved[-1].research_sha256 == previous.research_sha256
    assert saved[-1].input_sha256 != previous.input_sha256
    if spent == 3:
        assert result.review_outcome == "unavailable"


def assessed_verdict(*, uncertain=False):
    return json.dumps(
        {
            "passes": True,
            "failures": [],
            "checks": [
                {
                    "claim_id": "c1",
                    "judgment": "qualified" if uncertain else "supported",
                    "evidence_standard": "uncertain" if uncertain else "met",
                    "evidence": [{"source_id": "s1", "start": 0, "end": len(pool(1)[0].text)}],
                    "reason": "The source supplies the measured evidence.",
                }
            ],
        }
    )


async def test_unused_repair_reserve_resolves_conflicting_verdict_without_new_budget():
    """The retained reserve follows a repair, not another unchanged-draft retry."""
    saved = []
    repair = "## Executive summary\nThe measured evidence covers an observed window [[s1]]."
    router = RecordingRouter(
        [
            '{"inspect":[{"source_id":"s1"}]}',
            assessed_verdict(uncertain=True),
            repair,
            assessed_verdict(),
        ]
    )
    final, review, _ = await arc(router, saved, review_decisions=3)
    assert final.summary != FINAL.summary and final.sections == FINAL.sections
    assert review.outcome == "verdict"
    assert router.stages == [
        "report_review",
        "report_review_reask",
        "report_rework",
        "report_final_check",
    ]
    assert saved[-1].budget.used == saved[-1].budget.limit == 3
    assert "observed window" in router.prompt(3)
    assert "REVIEW WORK REMAINING: 1 decision(s)" in router.last_message(3)
    assert "Inspection is no longer available" in router.last_message(3)
    assert saved[-1].repair_started
    assert review.trail["draft_sha256"] == hashlib.sha256(final.markdown.encode()).hexdigest()


async def test_stop_before_released_reserve_preserves_feedback_and_one_remaining_call():
    saved = []
    first = RecordingRouter(
        [
            '{"inspect":[{"source_id":"s1"}]}',
            assessed_verdict(uncertain=True),
        ]
    )
    with pytest.raises(ResearchStopped):
        await arc(first, saved, review_decisions=3, stop=lambda: first.calls == 2)
    assert saved[-1].budget.remaining == 1 and not saved[-1].repair_started
    repair = "## Executive summary\nThe measured evidence covers an observed window [[s1]]."
    second = RecordingRouter([repair, assessed_verdict()])
    final, review, _ = await arc(second, saved, resume=saved[-1])
    assert final.summary != FINAL.summary and review.outcome == "verdict"
    assert second.stages == ["report_rework", "report_final_check"]
    assert saved[-1].budget.used == 3
    assert "PRIOR SOURCE INSPECTIONS" in second.prompt(0)
    assert review.trail["draft_sha256"] == hashlib.sha256(final.markdown.encode()).hexdigest()
    repeated = RecordingRouter()
    await arc(repeated, [], resume=saved[-1])
    assert repeated.calls == 0


@pytest.mark.parametrize("stop_before_repair", [False, True])
async def test_exhausted_conflict_feedback_keeps_one_scoped_repair_and_no_false_pass(
    stop_before_repair,
):
    saved = []
    responses = [
        '{"inspect":[{"source_id":"s1"}]}',
        assessed_verdict(uncertain=True),
    ]
    repair = "## Executive summary\nThe measured evidence is limited to an observed window [[s1]]."
    after = [repair, assessed_verdict(uncertain=True)]
    first = RecordingRouter(responses if stop_before_repair else [*responses, *after])
    if stop_before_repair:
        with pytest.raises(ResearchStopped):
            await arc(first, saved, review_decisions=3, stop=lambda: first.calls == 2)
        assert saved[-1].stage == "review" and not saved[-1].repair_started
        assert saved[-1].budget.used == 2 and saved[-1].budget.last_verdict
        second = RecordingRouter(after)
        final, review, trail = await arc(second, saved, resume=saved[-1])
        stages = first.stages + second.stages
    else:
        final, review, trail = await arc(first, saved, review_decisions=3)
        stages = first.stages
    assert stages == ["report_review", "report_review_reask", "report_rework", "report_final_check"]
    assert final.summary != FINAL.summary and final.sections == FINAL.sections
    assert saved[-1].budget.used == saved[-1].budget.limit == 3
    assert saved[-1].repair_started
    assert review.outcome == "incomplete", "The changed prose still has an unresolved assessment."
    assert review.trail["attempts"] == 1
    assert review.trail["draft_sha256"] == hashlib.sha256(final.markdown.encode()).hexdigest()
    repairs = [row for row in trail if row["kind"] == "report_rework"]
    assert len(repairs) == 1 and repairs[0]["applied"] == ["executive summary"]
    repeated = RecordingRouter()
    resumed, _, _ = await arc(repeated, [], resume=saved[-1])
    assert repeated.calls == 0 and resumed == final


async def test_repaired_claim_does_not_inherit_its_prior_semantic_judgment():
    saved = []
    original = json.loads(FAILURE)
    changed_check = {
        **CHECKS[0],
        "claim_id": "c2",
        "reason": "This judgment describes only the unrepaired body sentence.",
    }
    original["checks"].append(changed_check)
    first = RecordingRouter([json.dumps(original), REPAIR])
    with pytest.raises(ResearchStopped):
        await arc(first, saved, stop=lambda: first.calls == 2)
    second = RecordingRouter([CLEAN])
    await arc(second, saved, resume=saved[-1])
    context = second.prompt(0)
    assert changed_check["reason"] not in context
    assert CHECKS[0]["reason"] in context
    assert "Clarify the comparison." in context
    assert second.stages == ["report_final_check"]


async def test_review_progress_marks_only_checking_changed_prose():
    router = RecordingRouter([FAILURE, REPAIR, CLEAN])
    saved, events = [], []
    await arc(router, saved, events=events)
    assert [payload for kind, payload in events if kind == "review"] == [
        {"k": 1, "of": 1},
        {"k": 1, "of": 1, "after_rework": True},
    ]
    assert [payload for kind, payload in events if kind == "rework"] == [{"k": 1, "of": 1}]
    assert router.stages == ["report_review", "report_rework", "report_final_check"]
    assert saved[-1].budget.used == 2


async def test_unresolved_initial_verdict_is_not_labelled_as_checking_revisions():
    router = RecordingRouter(["{}", "{}", CLEAN])
    saved, events = [], []
    await arc(router, saved, events=events)
    assert router.stages == ["report_review", "report_review_reask"]
    assert saved[-1].stage == "complete" and saved[-1].review_outcome == "unavailable"
    assert saved[-1].budget.remaining == 2
    assert not saved[-1].repair_started
    assert [payload for kind, payload in events if kind == "review"] == [
        {"k": 1, "of": 1},
    ]
    assert not any(kind == "rework" for kind, _ in events)
