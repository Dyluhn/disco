"""A user steer IS the owner weakening decision the guard demands.

Counted-promotion failure 2026-07-27 (`p4_ff_react_steer` seed 700022,
FALSE_FINISH_NO_OUTPUT). A mid-run steer changed the build's direction, which
legitimately obsoleted `file_exists:package.json`, `file_exists:src/App.tsx` and
`command:test -f dist/index.html`. Every revision dropping them was blocked —
SIXTEEN times, driving 29 planning turns against an all-time observed maximum of
14 — until the agent carried `package.json` into a plan whose new direction never
produces it, guaranteeing failure at the output-truth gate.

The refusal said "removing an acceptance condition requires a separate explicit
owner weakening decision" while offering the agent no way to obtain one. The
steer was that decision.

The guard itself is right and stays: an agent must never silently lower its own
bar. These tests pin that the allowance is narrow enough to keep it.
"""

from __future__ import annotations

import pytest
from disco.core import (
    ConversationStatus,
    EventSource,
    PlanEvent,
    PlanStep,
    StatusEvent,
)
from disco.core.dod import FileExistsPredicate
from disco.core.loop.plan_revisions import (
    PlanRevisionWeakeningError,
    assert_plan_revision_approvable,
    user_steer_authorizes_weakening,
)

_PKG = FileExistsPredicate(kind="file_exists", path="package.json")
_APP = FileExistsPredicate(kind="file_exists", path="src/App.tsx")


def _status(seq: int, detail: str) -> StatusEvent:
    return StatusEvent(
        seq=seq,
        source=EventSource.SYSTEM,
        status=ConversationStatus.RUNNING,
        detail=detail,
    )


def _plan(seq: int, revision: int, predicates: list[FileExistsPredicate]) -> PlanEvent:
    return PlanEvent(
        seq=seq,
        source=EventSource.AGENT,
        revision=revision,
        summary=f"r{revision}",
        steps=[
            PlanStep(title=f"s{i}", detail="d", done_condition=p)
            for i, p in enumerate(predicates, start=1)
        ],
    )


# ---- the signal itself -----------------------------------------------------


def test_a_steer_after_the_latest_approval_authorizes():
    events = [
        _plan(1, 1, [_PKG, _APP]),
        _status(2, "plan_approved"),
        _status(3, "revision_steer_pending"),
    ]
    assert user_steer_authorizes_weakening(events) is True


def test_a_steer_survives_the_planning_marker_that_consumes_it():
    # `pending_revision_steer` is cleared by the `planning` marker; this signal
    # must NOT be, or it would be gone by the time the revision is submitted.
    events = [
        _plan(1, 1, [_PKG, _APP]),
        _status(2, "plan_approved"),
        _status(3, "revision_steer_pending"),
        _status(4, "planning"),
    ]
    assert user_steer_authorizes_weakening(events) is True


def test_no_steer_at_all_does_not_authorize():
    events = [_plan(1, 1, [_PKG, _APP]), _status(2, "plan_approved")]
    assert user_steer_authorizes_weakening(events) is False


def test_a_steer_BEFORE_the_latest_approval_does_not_authorize():
    # The steer was already answered by that approval; it cannot license a
    # second, later drop. One steer authorizes one revision cycle.
    events = [
        _status(1, "revision_steer_pending"),
        _plan(2, 1, [_PKG, _APP]),
        _status(3, "plan_approved"),
    ]
    assert user_steer_authorizes_weakening(events) is False


def test_an_empty_history_does_not_authorize():
    assert user_steer_authorizes_weakening([]) is False


# ---- the guard, end to end -------------------------------------------------


def test_a_post_steer_revision_may_drop_an_obsoleted_predicate():
    events = [
        _plan(1, 1, [_PKG, _APP]),
        _status(2, "plan_approved"),
        _status(3, "revision_steer_pending"),
        _status(4, "planning"),
    ]
    candidate = _plan(5, 2, [_APP])  # package.json obsoleted by the steer
    assert_plan_revision_approvable(events, candidate)  # must not raise


def test_the_SAME_drop_without_a_steer_is_STILL_blocked():
    # The invariant the guard exists for. A spontaneous drop stays refused.
    events = [_plan(1, 1, [_PKG, _APP]), _status(2, "plan_approved")]
    candidate = _plan(3, 2, [_APP])
    with pytest.raises(PlanRevisionWeakeningError):
        assert_plan_revision_approvable(events, candidate)


def test_a_second_drop_after_the_steer_was_consumed_is_blocked_again():
    # Steer -> revision -> approval. A LATER drop has no fresh owner decision
    # behind it, so the guard closes again.
    events = [
        _plan(1, 1, [_PKG, _APP]),
        _status(2, "plan_approved"),
        _status(3, "revision_steer_pending"),
        _plan(4, 2, [_PKG, _APP]),
        _status(5, "plan_approved"),
    ]
    candidate = _plan(6, 3, [_APP])
    with pytest.raises(PlanRevisionWeakeningError):
        assert_plan_revision_approvable(events, candidate)


def test_a_monotonic_revision_is_unaffected_either_way():
    # Adding a predicate never needed an allowance and still does not.
    events = [_plan(1, 1, [_PKG]), _status(2, "plan_approved")]
    candidate = _plan(3, 2, [_PKG, _APP])
    assert_plan_revision_approvable(events, candidate)
