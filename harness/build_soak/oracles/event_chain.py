"""EventChainOracle (guidelines §11.1–11.3, §16).

Walks the canonical chain that the bare Build loop must satisfy, IN ORDER, and
stops at the first broken link (§16 "the first broken link wins"):

    submit -> user event
    user event -> plan event          (when the scenario requires the plan gate)
    plan event -> approval status
    approval status -> execution action
    execution action -> observation

Caveat (codex #5): a tool REJECTION is recorded as an AgentErrorEvent (or an
ObservationEvent with success=False). That IS visible feedback to the model — a
VALID pairing for the action — distinct from a genuinely-missing observation. So
`has_observation_for_action` treats an AgentErrorEvent as a valid pairing, and this
oracle only raises ACTION_NO_OBSERVATION when NEITHER an observation nor an error
references the action.
"""

from __future__ import annotations

from typing import Any

from .. import failure_codes as fc
from ..events import (
    EXECUTION_EXPECTED_TERMINALS,
    KIND_ACTION,
    KIND_AGENT_ERROR,
    KIND_OBSERVATION,
    KIND_PLAN,
    KIND_STATUS,
    PLAN_APPROVED_DETAIL,
    PLAN_VERIFICATION_PASSED_DETAIL,
    awaiting_approval_seqs,
    has_action,
    has_plan,
    has_user_message,
    is_autonomous,
    kind_of,
    plan_approved_seqs,
    seq_of,
    terminal_status,
    tool_name_of,
)
from .schema import OracleResult, failing, passing

_ORACLE = "EventChainOracle"


def _wants_plan_gate(scenario: dict[str, Any] | None) -> bool:
    if not scenario:
        return False
    ec = (scenario.get("assertions") or {}).get("event_chain") or {}
    return bool(ec.get("require_plan_before_execution"))


def _wants_observation_pairs(scenario: dict[str, Any] | None) -> bool:
    # Default ON: a Build run should always pair actions with observations. A
    # scenario may opt out explicitly with require_action_observation_pairs: false.
    if not scenario:
        return True
    ec = (scenario.get("assertions") or {}).get("event_chain") or {}
    return ec.get("require_action_observation_pairs", True) is not False


def _has_exact_verifier_repair_pass(events: list[dict[str, Any]], *, approval_seq: int) -> bool:
    """Whether host truth supplies the post-approval edge for a verifier-only repair.

    This is deliberately narrower than a generic status check. The latest approval
    must itself declare the typed verifier-repair reason, and a later RUNNING receipt
    must bind the exact approved plan id/revision/predicate fingerprints before the
    first terminal. Normal plans and malformed/mismatched receipts remain actionless.
    """
    approval = next(
        (
            event
            for event in events
            if kind_of(event) == KIND_STATUS
            and seq_of(event) == approval_seq
            and event.get("detail") == PLAN_APPROVED_DETAIL
        ),
        None,
    )
    if approval is None:
        return False
    transition = approval.get("plan_verification_transition")
    if not isinstance(transition, dict) or transition.get("reason") != (
        "approved_plan_verifier_repair"
    ):
        return False
    plan_id = transition.get("new_plan_event_id")
    revision = transition.get("new_plan_revision")
    fingerprints = transition.get("new_predicate_fingerprints")
    if (
        not isinstance(plan_id, str)
        or not plan_id
        or not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision < 1
        or not isinstance(fingerprints, list)
        or not fingerprints
        or not all(isinstance(value, str) and value for value in fingerprints)
    ):
        return False

    terminal_seq = min(
        (
            seq_of(event)
            for event in events
            if kind_of(event) == KIND_STATUS
            and seq_of(event) > approval_seq
            and str(event.get("status")) in EXECUTION_EXPECTED_TERMINALS
        ),
        default=None,
    )
    if terminal_seq is None:
        return False

    for event in events:
        seq = seq_of(event)
        if not approval_seq < seq < terminal_seq:
            continue
        if (
            kind_of(event) != KIND_STATUS
            or event.get("status") != "RUNNING"
            or event.get("detail") != PLAN_VERIFICATION_PASSED_DETAIL
        ):
            continue
        receipt = event.get("plan_verifier_pass")
        if not isinstance(receipt, dict):
            continue
        spec_fingerprint = receipt.get("spec_fingerprint")
        if (
            receipt.get("authority") == "plan"
            and receipt.get("plan_event_id") == plan_id
            and receipt.get("plan_revision") == revision
            and receipt.get("predicate_fingerprints") == fingerprints
            and isinstance(spec_fingerprint, str)
            and spec_fingerprint.startswith("sha256:")
            and len(spec_fingerprint) > len("sha256:")
        ):
            return True
    return False


class EventChainOracle:
    def check(
        self, events: list[dict[str, Any]], *, scenario: dict[str, Any] | None = None
    ) -> list[OracleResult]:
        # 1. submit -> user event
        if not has_user_message(events):
            return [
                failing(
                    _ORACLE,
                    fc.NO_USER_EVENT_AFTER_SUBMIT,
                    first_broken_link="submit -> user_event",
                    facts={"user_event_present": False},
                )
            ]

        # 2. user event -> plan event (only when the scenario requires the gate)
        if _wants_plan_gate(scenario) and not has_plan(events):
            return [
                failing(
                    _ORACLE,
                    fc.NO_PLAN_AFTER_USER_TURN,
                    first_broken_link="user_event -> plan_event",
                    facts={"plan_event_present": False},
                )
            ]

        # 3/4. The FULL ordered approval chain (§16). For a plan-gated run that
        # reached a FINISHED terminal the durable facts must show, IN SEQ ORDER:
        #
        #   PlanEvent (A) < AWAITING_PLAN_APPROVAL (B) < RUNNING/plan_approved (C)
        #                 < execution action (D)
        #
        # FAIL-CLOSED + non-circular: the chain is NOT gated on `plan_approved`
        # already existing (a plan that finished WITHOUT approval would slip past),
        # and the human-approval gate (B) must PRECEDE the approval (C) — a
        # `plan_approved` with no preceding AWAITING is a forged/skipped gate.
        #
        # AUTONOMOUS EXCEPTION: an autonomous build auto-approves INLINE (engine.py
        # ~L855) emitting RUNNING/plan_approved with NO awaiting status — that is a
        # LEGITIMATE chain, so the B link is required only for interactive runs.
        #
        # STUCK is judged too (codex #1): the loop's approve-but-never-execute exit
        # stamps StatusEvent(STUCK/approve_plan_no_execution). That run approved a
        # plan and emitted no action, so the C->D link below breaks and it classifies
        # as APPROVE_PLAN_NO_EXECUTION — the FAIL it is, NOT a false PASS (which is
        # what happened while STUCK read as non-terminal/incomplete).
        approvals = plan_approved_seqs(events)
        awaiting = awaiting_approval_seqs(events)
        plan_seqs = [seq_of(e) for e in events if kind_of(e) == KIND_PLAN]
        term = terminal_status(events)
        autonomous = is_autonomous(scenario)
        # A run still parked at AWAITING_PLAN_APPROVAL (or otherwise non-terminal) is
        # legitimately incomplete, not a chain break — only judge a run that reached a
        # terminal where the post-approval execution chain was expected (FINISHED or
        # the give-up STUCK).
        if plan_seqs and term in EXECUTION_EXPECTED_TERMINALS:
            first_plan = min(plan_seqs)

            # Link A->B: PlanEvent -> AWAITING_PLAN_APPROVAL (interactive only).
            if not autonomous and not any(s > first_plan for s in awaiting):
                return [
                    failing(
                        _ORACLE,
                        fc.PLAN_APPROVED_STATUS_MISSING,
                        first_broken_link="plan_event -> awaiting_plan_approval",
                        facts={
                            "first_plan_seq": first_plan,
                            "awaiting_plan_approval_present": False,
                            "terminal_status": term,
                        },
                    )
                ]

            # Link B->C: AWAITING -> RUNNING/plan_approved.
            if not approvals:
                return [
                    failing(
                        _ORACLE,
                        fc.PLAN_APPROVED_STATUS_MISSING,
                        first_broken_link="awaiting_plan_approval -> plan_approved",
                        facts={
                            "plan_approved_present": False,
                            "terminal_status": term,
                        },
                    )
                ]
            first_approval = approvals[0]

            # Ordering A < B < C: the awaiting gate must fall BETWEEN the plan and
            # its approval (interactive only). Catches an out-of-order forged chain.
            if not autonomous and not any(first_plan < s < first_approval for s in awaiting):
                return [
                    failing(
                        _ORACLE,
                        fc.PLAN_APPROVED_STATUS_MISSING,
                        first_broken_link="plan_event -> awaiting_plan_approval",
                        facts={
                            "first_plan_seq": first_plan,
                            "awaiting_seqs": awaiting,
                            "first_plan_approved_seq": first_approval,
                            "reason": "no AWAITING_PLAN_APPROVAL between the plan and its approval",
                        },
                    )
                ]

            # Link C->D: RUNNING/plan_approved -> execution action.
            last_approval = approvals[-1]
            if not has_action(events, after_seq=last_approval) and not (
                _has_exact_verifier_repair_pass(events, approval_seq=last_approval)
            ):
                return [
                    failing(
                        _ORACLE,
                        fc.APPROVE_PLAN_NO_EXECUTION,
                        first_broken_link="approval_status -> execution_action",
                        facts={
                            "plan_approved_seq": last_approval,
                            "action_after_approval": False,
                            "terminal_status": term,
                        },
                    )
                ]

        # 5. execution action -> observation (action/observation pairing).
        if _wants_observation_pairs(scenario):
            broken = self._check_pairing(events)
            if broken is not None:
                return [broken]

        return [
            passing(
                _ORACLE,
                facts={
                    "plan_approved_count": len(approvals),
                    "action_count": sum(1 for e in events if kind_of(e) == KIND_ACTION),
                },
            )
        ]

    def _check_pairing(self, events: list[dict[str, Any]]) -> OracleResult | None:
        # ORDER-AWARE pairing: a response must come AFTER the action it answers.
        # action_id -> the action's seq.
        action_seqs: dict[str, int] = {}
        for e in events:
            if kind_of(e) == KIND_ACTION:
                action_seqs[str(e.get("id"))] = seq_of(e)

        # Every ActionEvent must be answered by an Observation OR an AgentError that
        # appears LATER in the log (a response at/ before the action is not a valid
        # pairing — it is forged/out-of-order).
        for e in events:
            if kind_of(e) != KIND_ACTION:
                continue
            action_id = str(e.get("id"))
            aseq = seq_of(e)
            if not self._has_later_response(events, action_id, aseq):
                return failing(
                    _ORACLE,
                    fc.ACTION_NO_OBSERVATION,
                    first_broken_link="execution_action -> observation",
                    facts={
                        "action_id": action_id,
                        "action_seq": aseq,
                        "tool_name": tool_name_of(e),
                    },
                )

        # Every Observation must reference an Action that EXISTS and PRECEDES it.
        for e in events:
            if kind_of(e) != KIND_OBSERVATION:
                continue
            ref = str(e.get("action_id"))
            origin = action_seqs.get(ref)
            if origin is None or origin >= seq_of(e):
                return failing(
                    _ORACLE,
                    fc.OBSERVATION_WITHOUT_ACTION,
                    first_broken_link="observation -> originating_action",
                    facts={
                        "observation_seq": seq_of(e),
                        "dangling_action_id": ref,
                        "originating_action_seq": origin,
                    },
                )
        return None

    @staticmethod
    def _has_later_response(events: list[dict[str, Any]], action_id: str, action_seq: int) -> bool:
        """True iff an ObservationEvent OR AgentErrorEvent references `action_id`
        with seq > `action_seq` (a rejection/error IS a valid pairing, codex #5)."""
        for e in events:
            k = kind_of(e)
            if seq_of(e) <= action_seq:
                continue
            if k == KIND_OBSERVATION and str(e.get("action_id")) == action_id:
                return True
            if (
                k == KIND_AGENT_ERROR
                and e.get("action_id") is not None
                and (str(e.get("action_id")) == action_id)
            ):
                return True
        return False
