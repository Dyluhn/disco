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
    KIND_ACTION,
    KIND_OBSERVATION,
    KIND_PLAN,
    has_action,
    has_observation_for_action,
    has_plan,
    has_user_message,
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

        # 3/4. plan event -> approval status -> execution action.
        #
        # FAIL-CLOSED: the chain is NOT gated on `plan_approved` already existing
        # (that is circular — a plan that finished WITHOUT ever being approved would
        # slip past). The rule keys on the durable facts: a PlanEvent exists and the
        # run reached a FINISHED terminal. A finished plan-gated build MUST show the
        # full approval+execution chain.
        approvals = plan_approved_seqs(events)
        has_plan_event = any(kind_of(e) == KIND_PLAN for e in events)
        term = terminal_status(events)
        # A run still parked at AWAITING_PLAN_APPROVAL (or otherwise non-terminal) is
        # legitimately incomplete, not a chain break — only judge a FINISHED run.
        if has_plan_event and term == "FINISHED":
            if not approvals:
                # The plan reached FINISHED with NO plan_approved status ever —
                # a false finish: the plan was never approved/executed.
                return [
                    failing(
                        _ORACLE,
                        fc.PLAN_APPROVED_STATUS_MISSING,
                        first_broken_link="plan_event -> approval_status",
                        facts={
                            "plan_approved_present": False,
                            "terminal_status": term,
                        },
                    )
                ]
            last_approval = approvals[-1]
            if not has_action(events, after_seq=last_approval):
                # The plan was approved and the run FINISHED, yet not a single
                # execution action appeared — a false finish of the approval gate.
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
        # Every ActionEvent must be answered by an Observation OR an AgentError.
        action_ids: set[str] = set()
        for e in events:
            if kind_of(e) != KIND_ACTION:
                continue
            action_id = str(e.get("id"))
            action_ids.add(action_id)
            if not has_observation_for_action(events, action_id):
                return failing(
                    _ORACLE,
                    fc.ACTION_NO_OBSERVATION,
                    first_broken_link="execution_action -> observation",
                    facts={
                        "action_id": action_id,
                        "action_seq": seq_of(e),
                        "tool_name": tool_name_of(e),
                    },
                )

        # Every Observation must reference an existing Action (no dangling obs).
        for e in events:
            if kind_of(e) != KIND_OBSERVATION:
                continue
            ref = str(e.get("action_id"))
            if ref not in action_ids:
                return failing(
                    _ORACLE,
                    fc.OBSERVATION_WITHOUT_ACTION,
                    first_broken_link="observation -> originating_action",
                    facts={"observation_seq": seq_of(e), "dangling_action_id": ref},
                )
        return None
