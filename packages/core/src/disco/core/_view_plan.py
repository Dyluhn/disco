"""View plan-progress helpers — effective plan progress and summary validation.

Extracted from ``view.py`` so the plan-progress logic stays under the module
logical-LOC limit. These functions are pure over event lists.
"""

from __future__ import annotations

import re

from .events import (
    ActionEvent,
    Event,
    PlanEvent,
    StatusEvent,
)


def _latest_plan(events: list[Event]) -> PlanEvent | None:
    """The maximal-revision PlanEvent — the plan the agent is currently executing.
    A re-plan supersedes prior ones."""
    plan: PlanEvent | None = None
    for e in events:
        if isinstance(e, PlanEvent) and (plan is None or e.revision >= plan.revision):
            plan = e
    return plan


def _carry_completed_steps(
    events: list[Event],
    plan: PlanEvent,
    plan_pos: int,
) -> dict[int, str]:
    """Carry completed steps from a prior plan revision when the frozen step
    contract is unchanged at the same index."""
    states: dict[int, str] = {}
    approval = next(
        (
            event
            for event in events[plan_pos + 1 :]
            if isinstance(event, StatusEvent)
            and event.detail == "plan_approved"
            and event.plan_verification_transition is not None
            and event.plan_verification_transition.new_plan_event_id == plan.id
            and event.plan_verification_transition.new_plan_revision == plan.revision
        ),
        None,
    )
    if approval is None or approval.plan_verification_transition is None:
        return states
    transition = approval.plan_verification_transition
    prior_plan, prior_states = effective_plan_progress(events[:plan_pos])
    if (
        prior_plan is not None
        and prior_plan.id == transition.old_plan_event_id
        and prior_plan.revision == transition.old_plan_revision
    ):
        for index, (prior_step, current_step) in enumerate(
            zip(prior_plan.steps, plan.steps, strict=False),
            start=1,
        ):
            if prior_step == current_step and prior_states.get(index) == "done":
                states[index] = "done"
    return states


def _apply_one_progress_event(
    e: ActionEvent,
    plan_seq: int,
    plan_pos: int,
    i: int,
    total: int,
    states: dict[int, str],
) -> None:
    """Apply one ActionEvent's progress mark to ``states``."""
    eseq = e.seq or 0
    if eseq and plan_seq:
        if eseq < plan_seq:
            return
    elif i < plan_pos:
        return
    name = e.tool_call.tool_name
    args = e.tool_call.arguments or {}

    def _set(idx_raw: object, state_raw: object) -> None:
        idx = int(idx_raw)  # type: ignore[arg-type]
        if 1 <= idx <= total:
            states[idx] = str(state_raw)

    if name == "plan_step":
        try:
            _set(args.get("index"), args.get("state"))
        except (TypeError, ValueError):
            pass
    elif name == "update_plan_progress":
        for s in args.get("steps") or []:
            try:
                _set(s.get("index"), s.get("state"))
            except (TypeError, ValueError, AttributeError):
                continue


def _apply_progress_events(
    events: list[Event],
    plan: PlanEvent,
    plan_pos: int,
    total: int,
    states: dict[int, str],
) -> None:
    """Apply plan_step and update_plan_progress marks to ``states``."""
    plan_seq = plan.seq or 0
    for i, e in enumerate(events):
        if not isinstance(e, ActionEvent) or e.tool_call is None:
            continue
        _apply_one_progress_event(e, plan_seq, plan_pos, i, total, states)


def effective_plan_progress(
    events: list[Event],
) -> tuple[PlanEvent | None, dict[int, str]]:
    """The SINGLE source of truth for per-step plan completion. Merges BOTH progress
    channels: incremental ``plan_step(index, state)`` marks (small models) AND
    declarative ``update_plan_progress({steps:[{index,state}...]})`` full-state
    snapshots (capable models — the #3 redesign). Returns (latest_plan,
    {1-based index: effective state}).
    """
    plan: PlanEvent | None = None
    plan_pos = -1
    for i, e in enumerate(events):
        if isinstance(e, PlanEvent) and (plan is None or e.revision >= plan.revision):
            plan, plan_pos = e, i
    if plan is None or not plan.steps:
        return (None, {})
    states = _carry_completed_steps(events, plan, plan_pos)
    _apply_progress_events(events, plan, plan_pos, len(plan.steps), states)
    return (plan, states)


# Tool-call PROTOCOL residue markers and dressed-tag regex.
_PROTOCOL_MARKERS: tuple[str, ...] = (
    "<parameter",
    "</parameter>",
    "<function_calls>",
    "</function_calls>",
    "<invoke ",
    "</invoke>",
    "<tool_call",
    "</tool_call>",
    "<|tool_call",
    '"tool_calls":',
    '"tool_call_id":',
)

_PROTOCOL_TAG = re.compile(
    r"<\s*/?\s*(?:[|｜▁]+|DSML)*\s*(?:parameter|invoke|function_calls|tool[_▁]calls?)\b",
    re.IGNORECASE,
)

_SUMMARY_REPAIR_INSTRUCTION = (
    "Your previous summary contained tool-call protocol markup. Re-write it as "
    "plain prose describing what happened. Do not include ANY tool-call syntax, "
    "in any dialect: not <parameter>, <invoke>, <function_calls>, a tool_calls "
    "JSON payload, and not your own special tool-call delimiters even when they "
    "are written with unusual characters. Do not call a tool now — answer with "
    "the summary text itself. Ordinary code, HTML or shell snippets are fine "
    "when they are part of what you are describing."
)


def summary_rejection_reason(summary: str) -> str | None:
    """Why this summarizer output must not be persisted, or None if it is fine."""
    lowered = summary.lower()
    for marker in _PROTOCOL_MARKERS:
        if marker.lower() in lowered:
            return f"contains tool-call protocol markup: {marker!r}"
    dressed = _PROTOCOL_TAG.search(summary)
    if dressed is not None:
        return f"contains tool-call protocol markup: {dressed.group(0)!r}"
    return None


def _fallback_summary(start_seq: int, end_seq: int) -> str:
    """A truthful host-authored stand-in when the summarizer cannot be trusted."""
    return (
        f"[host summary] Events {start_seq}-{end_seq} were removed from context to "
        "stay within the model's window. The summarizer's output was rejected as "
        "unusable, so the work done in that span is NOT summarized here. Treat "
        "this range as unknown rather than as 'nothing happened': re-read files or "
        "re-check state before assuming any step in it was or was not completed."
    )
