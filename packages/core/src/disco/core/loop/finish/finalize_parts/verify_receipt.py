"""Verify-receipt reuse eligibility for the finish gate.

Sole owner of "can an immediately preceding executor-backed shell result
stand in for re-running the finish verify command" policy. Extracted from
``_FinalizeMixin._reuse_finish_verify_receipt`` in ``finalize.py``, which
now delegates here.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Protocol

from ....events import AgentErrorEvent, WorkspaceMutationEvent, latest_workspace_run_intent
from ..common import (
    ActionEvent,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    StatusEvent,
)

if TYPE_CHECKING:
    from ...ports import LoopEventPort

    class _LoopFacet(LoopEventPort, Protocol):
        """The loop capability this module uses: the event log."""


# The essential clause of the reuse escalation, given a name so a test can DERIVE
# it instead of retyping a slice of it (F61, 2026-08-07t; the Attestation-Binding
# Invariant, F58). It was previously a FRAGMENT of a multi-part concatenation whose
# whole value is an f-string, so no symbol owned it and the gate was silent about it
# by construction. Composition below is byte-identical; only the ownership moved.
_RECEIPT_REUSE_NOT_BLOCKING = "the verify command is NOT what is blocking the finish"


def _latest_action_event(events: list[Event]) -> ActionEvent | None:
    """Return the most recent ActionEvent of any tool, or None."""
    for ev in reversed(events):
        if isinstance(ev, ActionEvent):
            return ev
    return None


def _single_correlated_observation(
    events: list[Event], action_index: int, action_id: str
) -> ObservationEvent | None:
    """Return the one-and-only ObservationEvent correlated to an action.

    An earlier, duplicate, or mismatched record is not an executor receipt
    for this action and cannot authorize reuse.
    """
    correlated = [
        ev
        for ev in events
        if isinstance(ev, (ObservationEvent, AgentErrorEvent)) and ev.action_id == action_id
    ]
    if (
        len(correlated) != 1
        or not isinstance(correlated[0], ObservationEvent)
        or events.index(correlated[0]) <= action_index
    ):
        return None
    return correlated[0]


def _prior_reuse_count(events: list[Event], fingerprint: str) -> int:
    """How many times THIS command's receipt has already been reused, plus this one.

    GROUNDED FEEDBACK constraint 4 (the twice-rule), F51 (2026-08-07d/e). This
    surface fired 2× byte-identically in `canary/000` and 2× in `pilota/001`: the
    reuse notice names the command and the prior observation id, and when `finish`
    is attempted twice with no intervening action BOTH are unchanged, so the two
    bodies are identical.

    Ledger-derived, and the ledger already exists — the reuse notice has stamped
    ``finish_verify_receipt_reused`` plus ``command_fingerprint_sha256`` into its
    own ``meta`` since the seam was written. Nothing read it back. Keying on the
    COMMAND fingerprint rather than the observation id is deliberate: the question
    the agent needs answered is "how many times has this run leaned on the same
    verify result", which is a property of the command, not of which observation
    happened to carry it.
    """
    return 1 + sum(
        1
        for ev in events
        if isinstance(ev, MessageEvent)
        and ev.meta.get("finish_verify_receipt_reused") is True
        and ev.meta.get("command_fingerprint_sha256") == fingerprint
    )


def _plan_verifier_failed_after(events: list[Event], since_index: int) -> bool:
    """True when a ``plan_verifier_failure`` marker followed the given index."""
    for ev in events[since_index + 1 :]:
        if isinstance(ev, StatusEvent) and ev.plan_verifier_failure is not None:
            return True
    return False


def _synthetic_or_failed_receipt(observation: ObservationEvent) -> bool:
    """Cached, foreign, and failed observations cannot suppress an explicit request."""
    result = observation.tool_result
    check = observation.meta.get("check", {})
    structured = result.structured or {}
    return (
        observation.source is not EventSource.ENVIRONMENT
        or (isinstance(check, dict) and "memo_of" in check)
        or "memo_of_seq" in structured
        or result.error is not None
        or structured.get("timed_out") is True
        or ("exit_code" in structured and structured["exit_code"] != 0)
    )


def _receipt_invalidated(events: list[Event], observation: ObservationEvent) -> bool:
    """Only real executor evidence with no later execution/authority boundary."""
    if _synthetic_or_failed_receipt(observation):
        return True
    observation_index = events.index(observation)
    intent = latest_workspace_run_intent(events[: observation_index + 1])
    for event in events[observation_index + 1 :]:
        if isinstance(event, MessageEvent):
            if event.source is EventSource.USER:
                return True
            continue
        if isinstance(event, WorkspaceMutationEvent) and event.operation == "agent.view-admitted":
            if intent is not None and event.run_intent_id == intent.id:
                continue  # Same-run model view bookkeeping changes no executor state.
            return True
        return True  # New run, runtime/source mutation, receipt, or lifecycle boundary.
    return False


async def reuse_finish_verify_receipt(
    loop: _LoopFacet, verify_cmd: str, events: list[Event]
) -> bool:
    """Reuse one exact, immediately preceding executor-backed shell result.

    When the most recent ActionEvent of any tool matches the resolved
    verify command exactly and its correlated ObservationEvent succeeded,
    and no ``plan_verifier_failure`` intervened after that observation,
    skip re-execution and emit an audit marker.
    """
    most_recent_action = _latest_action_event(events)
    if most_recent_action is None:
        return False

    if most_recent_action.tool_call.tool_name != "shell":
        return False
    if most_recent_action.tool_call.arguments.get("command") != verify_cmd:
        return False

    action_index = events.index(most_recent_action)
    observation = _single_correlated_observation(events, action_index, most_recent_action.id)
    if observation is None or _receipt_invalidated(events, observation):
        return False
    result = observation.tool_result
    if not (
        result.call_id == most_recent_action.tool_call.call_id
        and result.tool_name == "shell"
        and result.success
    ):
        return False

    observation_index = events.index(observation)
    if _plan_verifier_failed_after(events, observation_index):
        return False

    fingerprint = hashlib.sha256(verify_cmd.encode()).hexdigest()
    reuses = _prior_reuse_count(events[observation_index + 1 :], fingerprint)
    # Constraint 4 (F51). A repeat here is a real signal and the agent was never
    # given it: the verify command is passing and being carried forward, so
    # whatever is refusing the finish is something ELSE. Restating the reuse
    # verbatim invites the agent to keep re-running the one thing that already
    # works.
    escalation = (
        ""
        if reuses <= 1
        else (
            f"This is reuse {reuses} of the same receipt in this run — "
            f"{_RECEIPT_REUSE_NOT_BLOCKING}. Read the refusal that "
            "follows this message and address that instead; re-running the verify "
            "would duplicate an explicitly requested action, not add acceptance authority.\n"
        )
    )
    await loop._emit(
        MessageEvent(
            source=EventSource.ENVIRONMENT,
            message=LLMMessage(
                role="user",
                content=(
                    "<system-reminder>\n"
                    f"Finish verify receipt for `{verify_cmd}` reused from the prior "
                    f"successful shell execution recorded at {observation.id} — that "
                    "historical executor evidence is cited without re-executing the command. "
                    "This does not certify current runtime state or required acceptance.\n"
                    f"{escalation}"
                    "</system-reminder>"
                ),
            ),
            meta={
                "finish_verify_receipt_reused": True,
                "prior_action_id": most_recent_action.id,
                "command_fingerprint_sha256": fingerprint,
                "receipt_reuses": reuses,
            },
        )
    )
    return True
