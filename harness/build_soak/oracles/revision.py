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
    action_executed,
    action_id_of,
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


def _resolve_anchors(
    events: list[dict[str, Any]],
    boundary: int,
    followups_spec: list[dict[str, Any]] | None,
    meta: dict[str, Any] | None,
) -> list[tuple[int, bool]]:
    """The revision-anchor follow-ups as ``(user_seq, declared_requires_revision)`` pairs in
    chronological order.

    METADATA PATH (the runner supplied harness-anchor metadata — every live run): anchor ONLY
    on the DECLARED scenario follow-ups the runner actually sent, EXCLUDING harness-INJECTED
    auto-answers (clarification answers / decision picks). This fixes the regression where an
    auto-answer (a user message after the boundary) was mis-anchored as a revision follow-up →
    false NO_REPLAN_AFTER_REVISION / WRITE_BEFORE_REVISION_APPROVAL. Each declared seq carries
    its OWN `requires_plan_revision` flag (parallel array, matched by send order — guarded to as
    many entries as were actually sent).

    FALLBACK PATH (no metadata — legacy fixtures / direct classify): the original behavior —
    ALL user messages after the boundary, with the flag matched by spec index."""
    if meta is not None:
        injected = {int(s) for s in (meta.get("harness_injected_user_seqs") or [])}
        seqs = meta.get("declared_followup_seqs") or []
        flags = meta.get("declared_followup_requires_revision") or []
        pairs: list[tuple[int, bool]] = []
        for i, raw in enumerate(seqs):
            s = int(raw)
            if s <= boundary or s in injected:
                continue
            flag = bool(flags[i]) if i < len(flags) else False
            pairs.append((s, flag))
        pairs.sort(key=lambda p: p[0])
        return pairs
    # Legacy fallback: every user message after the boundary, flag by spec index.
    out: list[tuple[int, bool]] = []
    for i, s in enumerate(_followup_user_seqs(events, boundary)):
        flag = (
            bool(followups_spec[i].get("requires_plan_revision"))
            if followups_spec and i < len(followups_spec)
            else False
        )
        out.append((s, flag))
    return out


def _first_mutating_action_after(events: list[dict[str, Any]], after_seq: int) -> int | None:
    """Seq of the first mutating action after `after_seq` that REACHED THE EXECUTOR.

    A mutating action is paired with its result by `action_id`; it counts unless it
    was GATE-REJECTED before execution — an agent_error pairing with NO tool_result
    observation (`action_executed` False). A call that reached the executor (ANY
    observation, success True OR False — a write can mutate then report failure)
    counts. A gate-rejected pre-approval write never wrote the workspace — that is the
    §11.3 tool-rejection-recovery contract WORKING, not a §11.4 write-before-revised-
    approval violation. Counting that rejected ATTEMPT as a write was the Bug-13
    false-FAIL. The anti-false-PASS half is preserved: any write that EXECUTED (incl.
    a mutate-then-fail success=False write) still counts → still drives
    WRITE_BEFORE_REVISION_APPROVAL.
    """
    for e in events:
        if kind_of(e) != KIND_ACTION or seq_of(e) <= after_seq:
            continue
        name = tool_name_of(e)
        if name is None or name in PLANNING_SAFE_TOOLS:
            continue
        aid = action_id_of(e)
        if aid is not None and action_executed(events, aid):
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


def _check_revised_plan(
    events: list[dict[str, Any]],
    *,
    fseq: int,
    rev_before: int,
    mutated_seq: int | None,
) -> tuple[OracleResult | None, tuple[int, int] | None]:
    """Check that a revised plan exists and is incremented. Returns (error, revised_plan)."""
    replanned = has_status(events, detail=PLANNING_DETAIL, after_seq=fseq)
    revised_plan = _first_plan_after(events, fseq)
    if revised_plan is None:
        return (
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
            ),
            None,
        )
    p_seq, p_rev = revised_plan
    if p_rev != rev_before + 1:
        return (
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
            ),
            None,
        )
    return None, revised_plan


def _check_revised_approval_chain(
    events: list[dict[str, Any]],
    *,
    fseq: int,
    p_seq: int,
    revised_approval: int | None,
    mutated_seq: int | None,
    autonomous: bool,
    awaiting: list[int],
    term: str | None,
) -> OracleResult | None:
    """Check the revised approval chain (write-before, awaiting, execution)."""
    if mutated_seq is not None and (revised_approval is None or mutated_seq < revised_approval):
        return failing(
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

    if revised_approval is None:
        if term == "FINISHED":
            return failing(
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
        return None  # incomplete — not a failure

    if not autonomous and not any(p_seq < s < revised_approval for s in awaiting):
        return failing(
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

    if term == "FINISHED" and not has_action(events, after_seq=revised_approval):
        return failing(
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
    return None


def _check_one_followup_revision(
    events: list[dict[str, Any]],
    *,
    fseq: int,
    declared: bool,
    autonomous: bool,
    awaiting: list[int],
    term: str | None,
) -> OracleResult | None:
    """Check one follow-up's revision chain. Returns a failure or None (ok/incomplete)."""
    rev_before = _rev_upto(events, fseq)
    mutated_seq = _first_mutating_action_after(events, fseq)
    requires_revision = declared or mutated_seq is not None
    if not requires_revision:
        return None

    plan_error, revised_plan = _check_revised_plan(
        events, fseq=fseq, rev_before=rev_before, mutated_seq=mutated_seq
    )
    if plan_error is not None:
        return plan_error
    assert revised_plan is not None
    p_seq, _p_rev = revised_plan

    stale_approval = next((s for s in plan_approved_seqs(events) if fseq < s < p_seq), None)
    if stale_approval is not None:
        return failing(
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

    revised_approval = _first_approval_after(events, p_seq)
    return _check_revised_approval_chain(
        events,
        fseq=fseq,
        p_seq=p_seq,
        revised_approval=revised_approval,
        mutated_seq=mutated_seq,
        autonomous=autonomous,
        awaiting=awaiting,
        term=term,
    )


def _check_expected_final_revision(
    events: list[dict[str, Any]],
    scenario: dict[str, Any] | None,
) -> OracleResult | None:
    """Cross-check the declared expected final revision."""
    if not scenario:
        return None
    expected = ((scenario.get("assertions") or {}).get("revisions") or {}).get(
        "expected_final_plan_revision"
    )
    actual = latest_plan_revision(events)
    if expected is not None and actual < int(expected):
        return failing(
            _ORACLE,
            fc.PLAN_REVISION_NOT_INCREMENTED,
            first_broken_link="followups -> expected_final_plan_revision",
            facts={"expected_final_plan_revision": int(expected), "actual": actual},
        )
    return None


class RevisionOracle:
    def check(
        self,
        events: list[dict[str, Any]],
        *,
        scenario: dict[str, Any] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> list[OracleResult]:
        approvals = plan_approved_seqs(events)
        if not approvals:
            return [skipping(_ORACLE, reason="no initial plan approval — no revisions to judge")]

        boundary = approvals[0]
        followups_spec = scenario.get("followups") if scenario else None
        anchors = _resolve_anchors(events, boundary, followups_spec, meta)
        if not anchors:
            return [skipping(_ORACLE, reason="no follow-up user turns after the first approval")]

        autonomous = is_autonomous(scenario)
        awaiting = awaiting_approval_seqs(events)
        term = terminal_status(events)

        for fseq, declared in anchors:
            failure = _check_one_followup_revision(
                events,
                fseq=fseq,
                declared=declared,
                autonomous=autonomous,
                awaiting=awaiting,
                term=term,
            )
            if failure is not None:
                return [failure]

        expected_failure = _check_expected_final_revision(events, scenario)
        if expected_failure is not None:
            return [expected_failure]

        return [
            passing(
                _ORACLE,
                facts={
                    "followup_count": len(anchors),
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
