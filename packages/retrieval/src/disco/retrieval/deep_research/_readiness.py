"""Readiness floors for research, including honest source-bounded completion."""

from ._agent_state import _AgentState
from ._turn_context import _supported_themes, _unadmitted_ids
from .depth import DepthBound
from .source_identity import distinct_work_count


def _readiness_rejection(state: _AgentState, bound: DepthBound, *, fresh: bool) -> str:
    """Why the host will not let this run start writing yet — "" when it will.

    Every floor names the number it wants and the number the run has, in the
    order a run reaches them. Lifted out of `_handle_parsed_turn` unchanged so
    the loop body stays a loop; the chain itself is the same one it always was.
    """
    if state.budget.remaining <= 0 and state.pool:
        return ""  # Bounded completion preserves open gaps; no more evidence can be acquired.
    unknown_ids = _unadmitted_ids(state)
    supported_themes = _supported_themes(state)
    if state.turns_completed < bound.minimum_research_turns:
        if not fresh:
            return (
                "Readiness rejected: issue at least one new research query as a fresh "
                "pivot before trying again."
            )
        return (
            f"Continue researching: at least {bound.minimum_research_turns} research turns "
            f"are required; {state.turns_completed} completed."
        )
    if len(state.pool) < bound.minimum_useful_sources:
        return (
            f"Continue researching: at least {bound.minimum_useful_sources} useful sources "
            f"are required; {len(state.pool)} admitted."
        )
    if distinct_work_count(state.pool) < bound.min_evidence_sources:
        return (
            f"Continue researching: at least {bound.min_evidence_sources} distinct works "
            f"are required; {distinct_work_count(state.pool)} admitted. Multiple passages "
            "or mirrors of one work do not count as independent evidence."
        )
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
            "Before marking readiness, run a targeted search for the strongest "
            "published objection, negative result, or conflicting evidence and record "
            "what was checked in contradictions_checked."
        )
    return ""
