"""Host-side readiness gates for the research agent."""

from __future__ import annotations

from ._agent_messages import upstream_zero_yield_warning
from ._agent_parsing import Turn
from ._agent_state import AgentState
from .depth import DepthBound
from .source_identity import distinct_work_count


def _coverage_ids(state: AgentState) -> tuple[list[str], int]:
    covered = state.coverage.get("covered", [])
    unknown = sorted(
        {
            evidence_id
            for item in covered
            for evidence_id in item.get("evidence_ids", [])
            if evidence_id not in state.seen_ids
        }
    )
    supported = sum(
        bool(item.get("angle"))
        and bool(item.get("evidence_ids"))
        and all(evidence_id in state.seen_ids for evidence_id in item["evidence_ids"])
        for item in covered
    )
    return unknown, supported


def _effort_failure(state: AgentState, bound: DepthBound, fresh: list[str]) -> str | None:
    if state.turns_completed >= bound.minimum_research_turns:
        return None
    if not fresh:
        return (
            "Readiness rejected: issue at least one new research query as a fresh "
            "pivot before trying again."
        )
    return (
        f"Continue researching: at least {bound.minimum_research_turns} research turns "
        f"are required; {state.turns_completed} completed."
    )


def _evidence_failure(state: AgentState, bound: DepthBound) -> str | None:
    if len(state.pool) < bound.minimum_useful_sources:
        return (
            f"Continue researching: at least {bound.minimum_useful_sources} useful sources "
            f"are required; {len(state.pool)} admitted."
        )
    works = distinct_work_count(state.pool)
    if works < bound.min_evidence_sources:
        return (
            f"Continue researching: at least {bound.min_evidence_sources} distinct works "
            f"are required; {works} admitted. Multiple passages or mirrors of one work "
            "do not count as independent evidence."
        )
    return None


def _coverage_failure(
    state: AgentState, bound: DepthBound, unknown_ids: list[str], supported_themes: int
) -> str | None:
    if unknown_ids:
        return (
            "Coverage references evidence ids that are not admitted: "
            + ", ".join(unknown_ids)
            + ". Cite only admitted ids."
        )
    if supported_themes < bound.min_evidence_themes:
        return (
            f"Continue the top-down coverage pass: at least {bound.min_evidence_themes} "
            f"evidence-backed major angles are required at this depth; {supported_themes} "
            "are recorded. Add only natural angles for this question and cite their "
            "admitted evidence ids."
        )
    if state.coverage.get("open"):
        return "Resolve these critical open coverage gaps before marking readiness: " + "; ".join(
            str(gap) for gap in state.coverage["open"]
        )
    if not state.coverage.get("contradictions_checked"):
        return (
            "Before marking readiness, run a targeted search for the strongest published "
            "objection, negative result, or conflicting evidence and record what was "
            "checked in contradictions_checked."
        )
    return None


def readiness_failure(
    state: AgentState, parsed: Turn, bound: DepthBound, fresh: list[str]
) -> str | None:
    """Return the first host-gate failure, preserving the original order."""
    effort = _effort_failure(state, bound, fresh)
    if effort:
        return effort
    evidence = _evidence_failure(state, bound)
    if evidence:
        return evidence
    unknown_ids, supported_themes = _coverage_ids(state)
    return _coverage_failure(state, bound, unknown_ids, supported_themes)


def apply_readiness_feedback(
    state: AgentState, parsed: Turn, bound: DepthBound, fresh: list[str], turn: int
) -> bool:
    """Apply readiness feedback and audit the decision; return whether ready."""
    if not parsed.ready_to_write:
        warning = upstream_zero_yield_warning(state)
        if warning:
            state.feedback = f"{state.feedback} {warning}".strip()
        return False
    failure = readiness_failure(state, parsed, bound, fresh)
    if failure is None:
        state.trail.append({"kind": "ready", "turn": turn})
        return True
    state.feedback = failure
    warning = upstream_zero_yield_warning(state)
    if warning:
        state.feedback = f"{state.feedback} {warning}".strip()
    if not fresh:
        state.feedback += (
            " Readiness also requires at least one new research query as a fresh "
            "pivot before trying again."
        )
    state.trail.append({"kind": "ready_rejected", "turn": turn, "reason": state.feedback})
    return False
