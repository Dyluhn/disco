"""Historical workspace-restore signal and advisory plan revisions.

Sibling of `test_steer_authorizes_weakening.py`, found the same way. That fix
taught the guard that a user steer legitimately obsoletes the conditions it
supersedes; this one covers the trigger it never learned.

Certified-lane evidence 2026-07-27 (`p4_ff_import_rollback` seed 900029, PASS at
36 planning turns against an all-time maximum of 8). An approved condition
`command:grep -q 'First revision 900029' index.html` survived a
`workspace_restored` that deleted the string it greps for. Every revision
dropping it was refused — 25 times, 23 of them byte-identical — because
"removing an acceptance condition requires a separate explicit owner weakening
decision", which an autonomous agent cannot obtain. Corroborated by
`p4_appkit_rollback` seed 900044 (18 planning turns vs max 12) blocking on a
`file_exists:` condition.

The restore signal remains available for compatibility and historical analysis.
Model-authored plan conditions are now advisory, so revising them no longer
requires this signal; external owner-authored acceptance remains separate.
"""

from __future__ import annotations

from disco.core import (
    ConversationStatus,
    EventSource,
    PlanEvent,
    PlanStep,
    StatusEvent,
    WorkspaceRestoredEvent,
)
from disco.core.dod import CommandExitPredicate, FileExistsPredicate
from disco.core.loop.plan_revisions import (
    assert_plan_revision_approvable,
    workspace_restore_authorizes_weakening,
)

_GREP = CommandExitPredicate(kind="command", cmd="grep -q 'First revision 900029' index.html")
_IDX = FileExistsPredicate(kind="file_exists", path="index.html")


def _status(seq: int, detail: str) -> StatusEvent:
    return StatusEvent(
        seq=seq,
        source=EventSource.SYSTEM,
        status=ConversationStatus.RUNNING,
        detail=detail,
    )


def _restore(seq: int) -> WorkspaceRestoredEvent:
    return WorkspaceRestoredEvent(seq=seq, version_seq=1, tree_digest="d" * 64)


def _plan(seq: int, revision: int, predicates: list[object]) -> PlanEvent:
    return PlanEvent(
        seq=seq,
        source=EventSource.AGENT,
        revision=revision,
        summary=f"r{revision}",
        steps=[
            PlanStep(title=f"s{i}", detail="d", done_condition=p)  # type: ignore[arg-type]
            for i, p in enumerate(predicates, start=1)
        ],
    )


# ---- the signal ------------------------------------------------------------


def test_a_restore_after_the_latest_approval_authorizes():
    events = [_plan(1, 1, [_GREP, _IDX]), _status(2, "plan_approved"), _restore(3)]
    assert workspace_restore_authorizes_weakening(events) is True


def test_a_restore_BEFORE_the_latest_approval_does_not():
    # The approval came after the rollback, so it already accounted for it.
    events = [_restore(1), _plan(2, 1, [_GREP, _IDX]), _status(3, "plan_approved")]
    assert workspace_restore_authorizes_weakening(events) is False


def test_no_restore_at_all_does_not_authorize():
    events = [_plan(1, 1, [_GREP, _IDX]), _status(2, "plan_approved")]
    assert workspace_restore_authorizes_weakening(events) is False


# ---- the guard end to end --------------------------------------------------


def test_the_deadlock_from_seed_900029_is_released():
    events = [_plan(1, 1, [_GREP, _IDX]), _status(2, "plan_approved"), _restore(3)]
    # dropping the condition the rollback made unsatisfiable
    assert_plan_revision_approvable(events, _plan(4, 2, [_IDX]))


def test_a_spontaneous_drop_with_no_restore_is_STILL_blocked():
    events = [_plan(1, 1, [_GREP, _IDX]), _status(2, "plan_approved")]
    assert_plan_revision_approvable(events, _plan(3, 2, [_IDX]))


def test_a_drop_after_a_restore_that_PREDATES_approval_is_still_blocked():
    events = [_restore(1), _plan(2, 1, [_GREP, _IDX]), _status(3, "plan_approved")]
    assert_plan_revision_approvable(events, _plan(4, 2, [_IDX]))


def test_one_restore_authorizes_one_revision_cycle_only():
    events = [
        _plan(1, 1, [_GREP, _IDX]),
        _status(2, "plan_approved"),
        _restore(3),
        _plan(4, 2, [_IDX]),
        _status(5, "plan_approved"),
    ]
    assert workspace_restore_authorizes_weakening(events) is False
    assert_plan_revision_approvable(events, _plan(6, 3, []))


def test_a_monotonic_revision_needs_no_authority_at_all():
    events = [_plan(1, 1, [_IDX]), _status(2, "plan_approved")]
    assert_plan_revision_approvable(events, _plan(3, 2, [_IDX, _GREP]))
