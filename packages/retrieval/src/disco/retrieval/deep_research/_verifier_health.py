"""Honest reporting of a partially-degraded grounding check.

The NLI verifier returns *neutral* when its transport fails, and that fallback
is correct: grounding feedback must never invent a contradiction out of an
outage. What is not correct is doing it silently — a report whose grounding
check partly did not run looks exactly like one that fully passed.

So the verifier counts its own no-ops and this module reads that count across
the writing phase. The delta (not the absolute) is what a run reports, because
one verifier instance may be shared by several runs.
"""

from __future__ import annotations

from typing import Any, Protocol


class _Degradable(Protocol):
    """The written-report surface this module annotates."""

    verifier_failures: int
    review_notes: list[str]


def verifier_failure_count(nli: Any) -> int:
    """Failed verification calls reported by ``nli``, or 0 when it does not
    track them."""
    value = getattr(nli, "verifier_failures", 0)
    return value if isinstance(value, int) and value >= 0 else 0


def verifier_degraded_note(failures: int) -> str:
    """The operator-visible line naming how much grounding did not run."""
    return (
        f"Grounding verification degraded: {failures} verifier call(s) failed "
        "or could not execute and returned neutral, so the grounding check did not run "
        "for those claims. Their confidence and unsupported counts understate "
        "what was actually verified."
    )


def apply_verifier_degradation(report: _Degradable, *, before: int, nli: Any) -> None:
    """Record this run's share of verifier no-ops on the finished report."""
    degraded = max(0, verifier_failure_count(nli) - before)
    if not degraded:
        return
    report.verifier_failures = degraded
    report.review_notes = [*report.review_notes, verifier_degraded_note(degraded)]
