"""RevisionOracle unit tests (guidelines §11.4, PR S2)."""

from __future__ import annotations

from _eventlog import action, agent_error, awaiting, msg, observation, plan, status

from harness.build_soak.events import normalize_events
from harness.build_soak.oracles.revision import RevisionOracle


def _run(events, scenario=None):
    return RevisionOracle().check(normalize_events(events), scenario=scenario)


def _initial_build():
    # user -> plan rev1 -> awaiting -> approved -> action -> obs -> finished
    return [
        msg(1, "user", "build a page"),
        plan(2, revision=1),
        awaiting(3, 2),
        status(4, "RUNNING", "plan_approved"),
        action(5, "shell", action_id="act5"),
        observation(6, "act5"),
        status(7, "FINISHED"),
    ]


def test_clean_replan_passes():
    # full revised chain: followup < revised plan(rev2) < AWAITING < approved < action
    events = _initial_build() + [
        msg(8, "user", "revise the heading"),
        status(9, "RUNNING", "planning"),
        plan(10, revision=2),
        awaiting(11, 10),
        status(12, "RUNNING", "plan_approved"),
        action(13, "file_write", args={"path": "index.html", "content": "x"}, action_id="act13"),
        observation(14, "act13"),
        status(15, "FINISHED"),
    ]
    results = _run(events)
    assert all(r.passed or r.skipped for r in results), [r.to_dict() for r in results]


def test_revised_approval_before_revised_plan_fails():
    # The out-of-order revised-approval false-pass: plan_approved appears BEFORE the
    # revised PlanEvent — a stale approval, not the revised one.
    events = _initial_build() + [
        msg(8, "user", "revise it"),
        status(9, "RUNNING", "plan_approved"),  # approval BEFORE the revised plan
        plan(10, revision=2),
        action(11, "file_write", args={"path": "x"}, action_id="act11"),
        observation(12, "act11"),
        status(13, "FINISHED"),
    ]
    results = _run(events)
    fail = next(r for r in results if r.failed)
    assert fail.code == "STALE_PLAN_USED_AFTER_FOLLOWUP"
    assert fail.first_broken_link == "followup_user_event -> revised_plan_approval"


def test_revised_plan_finished_without_approval_is_stale():
    # A revised plan that reached FINISHED but was never approved AFTER it.
    events = _initial_build() + [
        msg(8, "user", "revise it"),
        status(9, "RUNNING", "planning"),
        plan(10, revision=2),
        awaiting(11, 10),
        status(12, "FINISHED"),  # never approved the revised plan
    ]
    scenario = {"id": "s", "followups": [{"requires_plan_revision": True}]}
    results = _run(events, scenario)
    fail = next(r for r in results if r.failed)
    assert fail.code == "STALE_PLAN_USED_AFTER_FOLLOWUP"


def test_revised_approval_without_awaiting_interactive_fails():
    # Revised plan -> plan_approved with NO AWAITING gate between them (interactive).
    events = _initial_build() + [
        msg(8, "user", "revise"),
        status(9, "RUNNING", "planning"),
        plan(10, revision=2),
        status(11, "RUNNING", "plan_approved"),  # no awaiting before it
        action(12, "file_write", args={"path": "x"}, action_id="act12"),
        observation(13, "act12"),
        status(14, "FINISHED"),
    ]
    results = _run(events)
    fail = next(r for r in results if r.failed)
    assert fail.code == "PLAN_APPROVED_STATUS_MISSING"
    assert fail.first_broken_link == "revised_plan_event -> awaiting_plan_approval"


def test_autonomous_revised_chain_passes():
    # Autonomous revised build: inline approval, no AWAITING gate — legitimate.
    events = _initial_build() + [
        msg(8, "user", "revise"),
        plan(9, revision=2),
        status(10, "RUNNING", "plan_approved"),  # autonomous inline approve
        action(11, "file_write", args={"path": "x"}, action_id="act11"),
        observation(12, "act11"),
        status(13, "FINISHED"),
    ]
    scenario = {"id": "s", "autonomous": True, "followups": [{"requires_plan_revision": True}]}
    results = _run(events, scenario)
    assert all(r.passed or r.skipped for r in results), [r.to_dict() for r in results]


def test_no_replan_after_followup_with_freebuild_fails():
    # follow-up then a mutating action with NO new plan/planning entry -> stale plan.
    events = _initial_build() + [
        msg(8, "user", "also add a contact page"),
        action(9, "file_write", args={"path": "contact.html", "content": "x"}, action_id="act9"),
        observation(10, "act9"),
        status(11, "FINISHED"),
    ]
    results = _run(events)
    fail = next(r for r in results if r.failed)
    assert fail.code == "NO_REPLAN_AFTER_REVISION"
    assert fail.facts["followup_user_event_seq"] == 8
    assert fail.facts["write_before_revised_plan_approval"] is True


def test_revision_not_incremented_fails():
    events = _initial_build() + [
        msg(8, "user", "revise"),
        status(9, "RUNNING", "planning"),
        plan(10, revision=1),  # BUG: revision did not increment
        awaiting(11, 10),
        status(12, "RUNNING", "plan_approved"),
        action(13, "shell", action_id="act13"),
        observation(14, "act13"),
        status(15, "FINISHED"),
    ]
    results = _run(events)
    fail = next(r for r in results if r.failed)
    assert fail.code == "PLAN_REVISION_NOT_INCREMENTED"


def test_write_before_revision_approval_fails():
    # new plan IS proposed, but a write lands before it is approved.
    events = _initial_build() + [
        msg(8, "user", "revise"),
        status(9, "RUNNING", "planning"),
        action(10, "file_write", args={"path": "x", "content": "y"}, action_id="act10"),
        observation(11, "act10"),
        plan(12, revision=2),
        awaiting(13, 12),
        status(14, "RUNNING", "plan_approved"),
        status(15, "FINISHED"),
    ]
    results = _run(events)
    fail = next(r for r in results if r.failed)
    assert fail.code == "WRITE_BEFORE_REVISION_APPROVAL"
    assert fail.facts["first_write_tool_after_followup_seq"] == 10


# The EXACT pre-execution gate/guard refusal texts the product emits (only the tool
# name is interpolated). Mirrors disco.core.loop.engine `_MIDSTEP_STEER_REFUSAL` /
# `_gate_planning_mode` so the fixtures carry the REAL marker the oracle keys on (a
# generic "rejected" string must NOT be treated as a gate rejection).
_MIDSTEP_STEER_REFUSAL = (
    "<system-reminder>\n"
    "REFUSED: `file_replace_lines` was not applied. A change request arrived while "
    "you were mid-step, so this action would have landed on the OLD, now-stale plan. "
    "The build has re-entered PLANNING. Fold the new request into a REVISED plan and "
    "call `submit_plan`; once it is approved you can apply the change.\n"
    "</system-reminder>"
)


def test_rejected_preapproval_write_then_clean_replan_passes():
    # Bug 13: after the follow-up the model ATTEMPTS a write that the mid-step steer
    # gate REJECTS BEFORE execution (the REAL _MIDSTEP_STEER_REFUSAL marker, no
    # observation) — the §11.3 tool-rejection-recovery contract working. It then
    # recovers (file_read), re-plans (rev2), gets approval, and the POST-approval write
    # EXECUTES. The gate-rejected attempt must NOT count as a write-before-approval, so
    # the whole revised chain holds → PASS (no WRITE_BEFORE_REVISION_APPROVAL).
    events = _initial_build() + [
        msg(8, "user", "revise the hero and add pricing"),
        status(9, "RUNNING", "planning"),
        action(10, "file_replace_lines", args={"path": "index.html"}, action_id="act10"),
        agent_error(11, "act10", error=_MIDSTEP_STEER_REFUSAL),
        action(12, "file_read", args={"path": "index.html"}, action_id="act12"),
        observation(13, "act12", tool="file_read"),
        plan(14, revision=2),
        awaiting(15, 14),
        status(16, "RUNNING", "plan_approved"),
        action(17, "file_write", args={"path": "index.html", "content": "x"}, action_id="act17"),
        observation(18, "act17", tool="file_write"),
        status(19, "FINISHED"),
    ]
    scenario = {"id": "s", "followups": [{"requires_plan_revision": True}]}
    results = _run(events, scenario)
    assert all(r.passed or r.skipped for r in results), [r.to_dict() for r in results]


def test_nongate_agent_error_preapproval_write_still_fails():
    # codex anti-false-PASS #2: a bare AgentErrorEvent is NOT proof of non-mutation.
    # A pre-approval write whose tool RAISED during execution (it may have mutated disk
    # then thrown) is recorded as an AgentErrorEvent with NO observation, but it is NOT
    # a recognized gate rejection — so it COUNTS as a possible mutation → STILL
    # WRITE_BEFORE_REVISION_APPROVAL. Only the recognized gate/guard markers are excluded.
    events = _initial_build() + [
        msg(8, "user", "revise the hero and add pricing"),
        status(9, "RUNNING", "planning"),
        action(10, "file_write", args={"path": "index.html", "content": "x"}, action_id="act10"),
        # tool raised mid-execution — generic error, no gate marker, no observation
        agent_error(11, "act10", error="ERROR: file_write raised OSError: disk full"),
        plan(12, revision=2),
        awaiting(13, 12),
        status(14, "RUNNING", "plan_approved"),
        status(15, "FINISHED"),
    ]
    scenario = {"id": "s", "followups": [{"requires_plan_revision": True}]}
    results = _run(events, scenario)
    fail = next(r for r in results if r.failed)
    assert fail.code == "WRITE_BEFORE_REVISION_APPROVAL"
    assert fail.facts["first_write_tool_after_followup_seq"] == 10


def test_executed_preapproval_write_still_fails():
    # Anti-false-PASS guard: the SAME shape, but the pre-approval write EXECUTES
    # (success observation, file actually mutated) before the revised plan is
    # approved → STILL WRITE_BEFORE_REVISION_APPROVAL. Only REJECTED attempts are
    # excluded; an executed mutation before approval is always the violation.
    events = _initial_build() + [
        msg(8, "user", "revise the hero and add pricing"),
        status(9, "RUNNING", "planning"),
        action(10, "file_replace_lines", args={"path": "index.html"}, action_id="act10"),
        observation(11, "act10", tool="file_replace_lines"),  # EXECUTED (success)
        plan(12, revision=2),
        awaiting(13, 12),
        status(14, "RUNNING", "plan_approved"),
        status(15, "FINISHED"),
    ]
    scenario = {"id": "s", "followups": [{"requires_plan_revision": True}]}
    results = _run(events, scenario)
    fail = next(r for r in results if r.failed)
    assert fail.code == "WRITE_BEFORE_REVISION_APPROVAL"
    assert fail.facts["first_write_tool_after_followup_seq"] == 10


def test_mutate_then_fail_preapproval_write_still_fails():
    # codex anti-false-PASS: the pre-approval write REACHED the executor and produced
    # a tool_result observation with success=False — a write that may have MUTATED disk
    # and THEN failed (partial/failed-after-mutation). The signal is gate-rejection
    # (agent_error + NO observation), NOT the success flag, so an observed-but-failed
    # write STILL counts → STILL WRITE_BEFORE_REVISION_APPROVAL. (Treating success=False
    # as "not executed" would wrongly exclude a real mutation → a false-PASS.)
    events = _initial_build() + [
        msg(8, "user", "revise the hero and add pricing"),
        status(9, "RUNNING", "planning"),
        action(10, "file_replace_lines", args={"path": "index.html"}, action_id="act10"),
        observation(11, "act10", tool="file_replace_lines", success=False),  # ran, then failed
        plan(12, revision=2),
        awaiting(13, 12),
        status(14, "RUNNING", "plan_approved"),
        status(15, "FINISHED"),
    ]
    scenario = {"id": "s", "followups": [{"requires_plan_revision": True}]}
    results = _run(events, scenario)
    fail = next(r for r in results if r.failed)
    assert fail.code == "WRITE_BEFORE_REVISION_APPROVAL"
    assert fail.facts["first_write_tool_after_followup_seq"] == 10


def test_no_followup_skips():
    results = _run(_initial_build())
    assert all(r.skipped for r in results)


def test_scenario_requires_revision_even_without_write():
    # A declared follow-up requiring a revision, but no new plan and no write.
    events = _initial_build() + [msg(8, "user", "thanks, also tweak it")]
    scenario = {"id": "s", "followups": [{"requires_plan_revision": True}]}
    results = _run(events, scenario)
    fail = next(r for r in results if r.failed)
    assert fail.code == "NO_REPLAN_AFTER_REVISION"


# ---- revision-anchor metadata: declared follow-ups only, injected turns excluded --------
# Regression: the AWAITING_USER_DECISION / clarification auto-answers the harness injects are
# user messages after the approval boundary. Without anchor metadata the oracle anchored on them
# (they didn't re-plan) → false NO_REPLAN_AFTER_REVISION / WRITE_BEFORE_REVISION_APPROVAL. The
# fix anchors revision checks on the DECLARED follow-ups only and EXCLUDES the injected turns.


def _real_replan_then_injected_answer():
    """An initial build, a REAL declared follow-up (seq 8) that re-planned cleanly (rev2,
    approved, wrote, finished), then a HARNESS-INJECTED user answer (seq 16) after which the
    build kept mutating (seq 17) with NO new plan — the exact shape that mis-anchored live."""
    return _initial_build() + [
        msg(8, "user", "revise the heading"),  # REAL declared follow-up
        status(9, "RUNNING", "planning"),
        plan(10, revision=2),
        awaiting(11, 10),
        status(12, "RUNNING", "plan_approved"),
        action(13, "file_write", args={"path": "index.html", "content": "x"}, action_id="act13"),
        observation(14, "act13"),
        status(15, "FINISHED"),
        msg(16, "user", "use your best judgment and proceed"),  # HARNESS-INJECTED auto-answer
        action(17, "file_write", args={"path": "index.html", "content": "y"}, action_id="act17"),
        observation(18, "act17"),
        status(19, "FINISHED"),
    ]


def test_injected_answer_after_real_followup_is_excluded_passes():
    events = _real_replan_then_injected_answer()
    # WITHOUT metadata (legacy) the injected seq-16 answer mis-anchors → false NO_REPLAN.
    legacy = RevisionOracle().check(normalize_events(events))
    assert any(r.failed and r.code == "NO_REPLAN_AFTER_REVISION" for r in legacy), (
        "legacy path should reproduce the regression"
    )
    # WITH metadata: anchor ONLY the declared (re-planned) follow-up; exclude the injected turn.
    meta = {
        "declared_followup_seqs": [8],
        "declared_followup_requires_revision": [False],
        "harness_injected_user_seqs": [16],
    }
    results = RevisionOracle().check(normalize_events(events), meta=meta)
    assert all(r.passed or r.skipped for r in results), [r.to_dict() for r in results]
    # the surviving anchor is the declared one, NOT the injected seq
    passed = next(r for r in results if r.passed)
    assert passed.facts["followup_count"] == 1


def test_only_injected_answer_no_declared_skips_not_false_fail():
    # The live shape: the only post-boundary user turn is an INJECTED answer (the build then
    # mutated). Legacy flags it; with metadata it is excluded → SKIP (no revision to judge),
    # never a false NO_REPLAN.
    events = _initial_build() + [
        msg(8, "user", "use your best judgment and proceed"),  # injected
        action(9, "file_write", args={"path": "x", "content": "y"}, action_id="act9"),
        observation(10, "act9"),
        status(11, "FINISHED"),
    ]
    legacy = RevisionOracle().check(normalize_events(events))
    assert any(r.failed for r in legacy)  # legacy mis-flags it
    meta = {
        "declared_followup_seqs": [],
        "declared_followup_requires_revision": [],
        "harness_injected_user_seqs": [8],
    }
    results = RevisionOracle().check(normalize_events(events), meta=meta)
    assert all(r.skipped for r in results), [r.to_dict() for r in results]


def test_declared_followup_no_replan_still_flagged_with_metadata():
    # No false NEGATIVE: a genuine declared revision follow-up that did NOT re-plan (free-built)
    # is STILL flagged even on the metadata path.
    events = _initial_build() + [
        msg(8, "user", "also add a contact page"),
        action(9, "file_write", args={"path": "contact.html", "content": "x"}, action_id="act9"),
        observation(10, "act9"),
        status(11, "FINISHED"),
    ]
    meta = {
        "declared_followup_seqs": [8],
        "declared_followup_requires_revision": [True],
        "harness_injected_user_seqs": [],
    }
    results = RevisionOracle().check(normalize_events(events), meta=meta)
    fail = next(r for r in results if r.failed)
    assert fail.code == "NO_REPLAN_AFTER_REVISION"
    assert fail.facts["followup_user_event_seq"] == 8


def test_metadata_flag_matched_to_seq_by_send_order():
    # Two declared follow-ups; the SECOND requires a revision but neither re-planned nor wrote.
    # The flag must map to the right seq (send order) so the no-write declared revision is caught.
    events = _initial_build() + [
        msg(8, "user", "small note, no change needed"),  # declared, requires=False, no work
        msg(9, "user", "now actually revise the hero"),  # declared, requires=True, no replan
    ]
    meta = {
        "declared_followup_seqs": [8, 9],
        "declared_followup_requires_revision": [False, True],
        "harness_injected_user_seqs": [],
    }
    results = RevisionOracle().check(normalize_events(events), meta=meta)
    fail = next(r for r in results if r.failed)
    assert fail.code == "NO_REPLAN_AFTER_REVISION"
    assert fail.facts["followup_user_event_seq"] == 9  # the requires=True one, by send order


def test_metadata_partial_flag_list_guarded():
    # GUARD: fewer flags than seqs (a short/partial run) must not raise — the missing flag
    # defaults to False (and the unflagged declared turn with no work is simply not required).
    events = _initial_build() + [msg(8, "user", "thanks")]
    meta = {
        "declared_followup_seqs": [8],
        "declared_followup_requires_revision": [],  # shorter than seqs
        "harness_injected_user_seqs": [],
    }
    results = RevisionOracle().check(normalize_events(events), meta=meta)
    assert all(r.passed or r.skipped for r in results), [r.to_dict() for r in results]
