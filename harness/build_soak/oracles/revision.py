"""RevisionOracle (guidelines §11.4, §16).

For follow-up turns after a completed/approved plan, the loop must RE-plan rather
than free-build against the stale plan:

    new user event exists
    new plan revision exists (revision = previous + 1)   [unless explicit direct-patch]
    no write action before the revised plan is approved

Semantics (codex #2):
  * Plan revision is `1 + (count of prior PlanEvents)` (plans.py:147); the latest
    PlanEvent's stored `revision` equals the running plan count, so
    `latest_plan_revision` is the comparison basis.
  * A REVISION (re-entering planning via request_plan -> enter_planning) DOES emit
    StatusEvent(detail="planning") + a one-shot RE-PLANNING framing message — unlike
    the very first build turn (a plain RUNNING with no "planning" detail). So a
    follow-up that produced NEITHER a new plan revision NOR a `planning` re-entry,
    yet went on to mutate the workspace, used a stale plan.

Follow-up user events are identified as USER messages after the first plan approval
(the initial request precedes it). A follow-up "requires" a revision when the
scenario declares `requires_plan_revision` for it, OR when the log shows the agent
mutated the workspace after the follow-up (work happened — it needed a fresh plan).
"""

from __future__ import annotations

from typing import Any

from .. import failure_codes as fc
from ..events import (
    KIND_ACTION,
    KIND_MESSAGE,
    KIND_PLAN,
    PLANNING_DETAIL,
    SRC_USER,
    awaiting_approval_seqs,
    has_action,
    has_status,
    is_autonomous,
    kind_of,
    latest_plan_revision,
    plan_approved_seqs,
    seq_of,
    source_of,
    terminal_status,
    tool_name_of,
)
from .schema import OracleResult, failing, passing, skipping
from .tool_scope import PLANNING_SAFE_TOOLS

_ORACLE = "RevisionOracle"


def _followup_user_seqs(events: list[dict[str, Any]], boundary: int) -> list[int]:
    """USER message seqs strictly after `boundary` (the first plan approval)."""
    return [
        seq_of(e)
        for e in events
        if kind_of(e) == KIND_MESSAGE and source_of(e) == SRC_USER and seq_of(e) > boundary
    ]


def _first_mutating_action_after(events: list[dict[str, Any]], after_seq: int) -> int | None:
    for e in events:
        if kind_of(e) != KIND_ACTION or seq_of(e) <= after_seq:
            continue
        name = tool_name_of(e)
        if name is not None and name not in PLANNING_SAFE_TOOLS:
            return seq_of(e)
    return None


def _first_approval_after(events: list[dict[str, Any]], after_seq: int) -> int | None:
    for s in plan_approved_seqs(events):
        if s > after_seq:
            return s
    return None


def _first_plan_after(events: list[dict[str, Any]], after_seq: int) -> tuple[int, int] | None:
    """(seq, revision) of the FIRST PlanEvent strictly after `after_seq`, else None."""
    for e in events:
        if kind_of(e) == KIND_PLAN and seq_of(e) > after_seq:
            return seq_of(e), int(e.get("revision", 1))
    return None


class RevisionOracle:
    def check(
        self, events: list[dict[str, Any]], *, scenario: dict[str, Any] | None = None
    ) -> list[OracleResult]:
        approvals = plan_approved_seqs(events)
        if not approvals:
            # No initial approval ⇒ no "follow-up after a completed plan" to judge.
            return [skipping(_ORACLE, reason="no initial plan approval — no revisions to judge")]

        boundary = approvals[0]
        followups = _followup_user_seqs(events, boundary)
        if not followups:
            return [skipping(_ORACLE, reason="no follow-up user turns after the first approval")]

        followups_spec = scenario.get("followups") if scenario else None
        autonomous = is_autonomous(scenario)
        awaiting = awaiting_approval_seqs(events)
        term = terminal_status(events)

        for i, fseq in enumerate(followups):
            rev_before = _rev_upto(events, fseq)
            mutated_seq = _first_mutating_action_after(events, fseq)
            declared = (
                bool(followups_spec[i].get("requires_plan_revision"))
                if followups_spec and i < len(followups_spec)
                else False
            )
            requires_revision = declared or mutated_seq is not None
            if not requires_revision:
                continue

            replanned = has_status(events, detail=PLANNING_DETAIL, after_seq=fseq)

            # LINK F->P: a revised PlanEvent must appear AFTER the follow-up.
            revised_plan = _first_plan_after(events, fseq)
            if revised_plan is None:
                return [
                    failing(
                        _ORACLE,
                        fc.NO_REPLAN_AFTER_REVISION,
                        first_broken_link="followup_user_event -> revised_plan_event",
                        facts={
                            "followup_user_event_seq": fseq,
                            "latest_plan_revision_before_followup": rev_before,
                            "latest_plan_revision_after_followup": _rev_after(events, fseq),
                            "replanning_status_present": replanned,
                            "first_write_tool_after_followup_seq": mutated_seq,
                            "write_before_revised_plan_approval": mutated_seq is not None,
                        },
                    )
                ]
            p_seq, p_rev = revised_plan

            # LINK P-revision: the revised plan's revision must be previous + 1.
            if p_rev != rev_before + 1:
                return [
                    failing(
                        _ORACLE,
                        fc.PLAN_REVISION_NOT_INCREMENTED,
                        first_broken_link="revised_plan_event -> incremented_revision",
                        facts={
                            "followup_user_event_seq": fseq,
                            "revised_plan_seq": p_seq,
                            "latest_plan_revision_before_followup": rev_before,
                            "revised_plan_revision": p_rev,
                            "expected_revision": rev_before + 1,
                        },
                    )
                ]

            # ORDERING F < (stale approval) < P: a plan_approved BETWEEN the follow-up
            # and the revised plan is a STALE approval — it cannot be approving the
            # revised plan (which does not exist yet). This is the out-of-order
            # revised-approval false-pass.
            stale_approval = next(
                (s for s in plan_approved_seqs(events) if fseq < s < p_seq), None
            )
            if stale_approval is not None:
                return [
                    failing(
                        _ORACLE,
                        fc.STALE_PLAN_USED_AFTER_FOLLOWUP,
                        first_broken_link="followup_user_event -> revised_plan_approval",
                        facts={
                            "followup_user_event_seq": fseq,
                            "revised_plan_seq": p_seq,
                            "stale_plan_approved_seq": stale_approval,
                            "reason": "plan_approved precedes the revised plan (stale approval)",
                        },
                    )
                ]

            # The ONLY valid revised approval is one strictly AFTER the revised plan.
            revised_approval = _first_approval_after(events, p_seq)

            # LINK write -> revised approval: no mutation before the revised plan is
            # approved (a write before a valid post-plan approval used the stale plan).
            if mutated_seq is not None and (
                revised_approval is None or mutated_seq < revised_approval
            ):
                return [
                    failing(
                        _ORACLE,
                        fc.WRITE_BEFORE_REVISION_APPROVAL,
                        first_broken_link="followup_write -> revised_plan_approval",
                        facts={
                            "followup_user_event_seq": fseq,
                            "revised_plan_seq": p_seq,
                            "first_write_tool_after_followup_seq": mutated_seq,
                            "revised_plan_approval_seq": revised_approval,
                            "write_before_revised_plan_approval": True,
                        },
                    )
                ]

            # LINK P < [AWAITING] < C < D — the revised approval chain.
            if revised_approval is None:
                # No approval AFTER the revised plan. A run that FINISHED here used a
                # stale plan (never approved the revision); a non-terminal run is just
                # parked at the revised AWAITING gate (legitimately incomplete).
                if term == "FINISHED":
                    return [
                        failing(
                            _ORACLE,
                            fc.STALE_PLAN_USED_AFTER_FOLLOWUP,
                            first_broken_link="revised_plan_event -> revised_plan_approval",
                            facts={
                                "followup_user_event_seq": fseq,
                                "revised_plan_seq": p_seq,
                                "revised_plan_approval_seq": None,
                                "terminal_status": term,
                            },
                        )
                    ]
                continue  # incomplete — not a failure

            # LINK P -> AWAITING -> C: interactive runs require an AWAITING gate
            # BETWEEN the revised plan and its approval (autonomous auto-approves
            # inline with no gate — legitimate).
            if not autonomous and not any(p_seq < s < revised_approval for s in awaiting):
                return [
                    failing(
                        _ORACLE,
                        fc.PLAN_APPROVED_STATUS_MISSING,
                        first_broken_link="revised_plan_event -> awaiting_plan_approval",
                        facts={
                            "followup_user_event_seq": fseq,
                            "revised_plan_seq": p_seq,
                            "revised_plan_approval_seq": revised_approval,
                            "awaiting_seqs": awaiting,
                            "reason": "no AWAITING gate between the revised plan and its approval",
                        },
                    )
                ]

            # LINK C -> D: a FINISHED revised build must show an execution action
            # after the revised approval.
            if term == "FINISHED" and not has_action(events, after_seq=revised_approval):
                return [
                    failing(
                        _ORACLE,
                        fc.APPROVE_PLAN_NO_EXECUTION,
                        first_broken_link="revised_plan_approval -> execution_action",
                        facts={
                            "followup_user_event_seq": fseq,
                            "revised_plan_approval_seq": revised_approval,
                            "action_after_revised_approval": False,
                            "terminal_status": term,
                        },
                    )
                ]

        # All follow-ups re-planned cleanly. Optionally cross-check the declared
        # expected final revision.
        if scenario:
            expected = ((scenario.get("assertions") or {}).get("revisions") or {}).get(
                "expected_final_plan_revision"
            )
            actual = latest_plan_revision(events)
            if expected is not None and actual < int(expected):
                return [
                    failing(
                        _ORACLE,
                        fc.PLAN_REVISION_NOT_INCREMENTED,
                        first_broken_link="followups -> expected_final_plan_revision",
                        facts={"expected_final_plan_revision": int(expected), "actual": actual},
                    )
                ]

        return [
            passing(
                _ORACLE,
                facts={
                    "followup_count": len(followups),
                    "final_plan_revision": latest_plan_revision(events),
                },
            )
        ]


def _rev_upto(events: list[dict[str, Any]], seq: int) -> int:
    """Latest plan revision among plans at or before `seq`."""
    best = 0
    for e in events:
        if kind_of(e) == "plan" and seq_of(e) <= seq:
            best = max(best, int(e.get("revision", 1)))
    return best


def _rev_after(events: list[dict[str, Any]], seq: int) -> int:
    return latest_plan_revision(events, after_seq=seq)
