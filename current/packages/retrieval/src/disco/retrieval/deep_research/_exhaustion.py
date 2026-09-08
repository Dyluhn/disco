"""Why a research run ended badly — named by the layer that actually failed.

Both of the run's terminal failure classes are built here: the empty-pool
exhaustion walls (turn budget, source budget, the host's own circuit breaker)
and the malformed-turn wall (the driver could not sustain the turn protocol).
Each produces a typed :class:`disco.core.events.RunFailure` — a class from a
closed set, plus the four parts every wall owes its reader — and the prose
``detail`` is rendered from those fields, so the sentence an operator reads and
the fields the UI renders are one thing assembled once.

THE EMPTY-POOL WALLS
--------------------

A research run that reaches its turn or source cap holding an empty evidence
pool raises. The cap is what STOPPED the run, and for a long time it was also
the whole error message: ``research exhausted its turn budget without usable
evidence``. That sentence is true and useless. It reads as "the model searched
and searched and found nothing", and an operator has to go digging through the
run's trace to discover that the extraction provider's browser driver was
wedged and 59 of 60 fetches came back ``upstream_http_500``.

The error must name why. Same exception, same funnel outcome, same terminal
status — a longer, specific sentence:

    research exhausted its turn budget without usable evidence — extraction
    provider failing: 59/60 attempts failed (upstream_http_500)

The clause is only added when the evidence for it is overwhelming, because a
run that genuinely found nothing must not be told that infrastructure ate it:
a dominant share of the attempts has to have failed AND there have to be enough
attempts for the share to mean anything. Below those bars the message stays
exactly what it was.

Search is checked before extraction, upstream first. The extraction layer can
only fail on URLs that discovery produced, so when the search provider is the
thing that is down, naming the extractor would send the operator to the wrong
service.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from disco.core.events import RunFailure, RunFailureClass

from .._transport_retry import (
    PROVIDER_DEGRADED,
    _extraction_error_class,
    degradation_markers,
)
from ..models import ExtractedDoc
from ._search_outcomes import DISCOVERED, name_engines

#: A failure share at or above this is a provider outage rather than a bad run.
DOMINANT_FAILURE_FRACTION = 0.8
#: …but only once there are enough attempts for the share to be evidence. Nine
#: failures out of ten URLs is a hard day; 48 out of 60 is an outage.
EXTRACTION_MIN_ATTEMPTS = 10
#: Searches are far coarser than per-URL fetches (at most three per turn), so
#: the count bar is correspondingly lower.
SEARCH_MIN_QUERIES = 3

_NO_MARKER = "no usable provider response"


@dataclass
class ExtractionOutcomes:
    """Per-URL extraction results for one run, counted by failure class.

    The classes are the retrieval layer's own (`_extraction_error_class`), so
    the name in the error is the same name the trace and the per-URL statuses
    already use — never a second vocabulary invented here.
    """

    attempted: int = 0
    failures: dict[str, int] = field(default_factory=dict)

    def record(self, docs: Iterable[ExtractedDoc]) -> None:
        """Count one retrieval's extracted documents, successes included."""
        for doc in docs:
            self.attempted += 1
            error_class = _extraction_error_class(doc)
            if error_class is not None:
                self.failures[error_class] = self.failures.get(error_class, 0) + 1

    @property
    def failed(self) -> int:
        return sum(self.failures.values())

    @property
    def dominant(self) -> bool:
        """Whether extraction failure is the reason this run has no evidence."""
        return (
            self.attempted >= EXTRACTION_MIN_ATTEMPTS
            and self.failed >= self.attempted * DOMINANT_FAILURE_FRACTION
        )

    @property
    def top_error_class(self) -> str:
        """The most common failure class; ties break by name, never by chance."""
        if not self.failures:
            return _NO_MARKER
        return max(self.failures.items(), key=lambda row: (row[1], row[0]))[0]


def _search_rows(trail: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [entry for entry in trail if entry.get("kind") == "search" and entry.get("query")]


def _row_is_degraded(entry: Mapping[str, Any]) -> bool:
    """A query the host could not put to the world, for either host reason."""
    return entry.get("yield_reason") == PROVIDER_DEGRADED or entry.get("result") == "failed"


def _row_markers(entry: Mapping[str, Any]) -> list[str]:
    """What this search entry says broke: an exception type, or named engines."""
    provider_error = entry.get("provider_error")
    if isinstance(provider_error, str) and provider_error:
        return [provider_error]
    trace = entry.get("retrieval_trace")
    if not isinstance(trace, Mapping):
        return []
    engines, errors = degradation_markers(trace.get("provider_diagnostics"))
    return [*engines, *errors]


def _top_marker(rows: Iterable[Mapping[str, Any]]) -> str:
    counts: dict[str, int] = {}
    for entry in rows:
        for marker in _row_markers(entry):
            counts[marker] = counts.get(marker, 0) + 1
    if not counts:
        return _NO_MARKER
    return max(counts.items(), key=lambda row: (row[1], row[0]))[0]


def _search_finding(trail: Sequence[Mapping[str, Any]]) -> tuple[str, str] | None:
    """``(clause, top marker)`` when the search provider is what is down."""
    rows = _search_rows(trail)
    degraded = [entry for entry in rows if _row_is_degraded(entry)]
    if len(rows) < SEARCH_MIN_QUERIES or len(degraded) < len(rows) * DOMINANT_FAILURE_FRACTION:
        return None
    marker = _top_marker(degraded)
    return (
        f"search provider failing: {len(degraded)}/{len(rows)} queries degraded ({marker})",
        marker,
    )


def _search_clause(trail: Sequence[Mapping[str, Any]]) -> str | None:
    finding = _search_finding(trail)
    return finding[0] if finding is not None else None


def _extraction_clause(extraction: ExtractionOutcomes) -> str | None:
    if not extraction.dominant:
        return None
    return (
        f"extraction provider failing: {extraction.failed}/{extraction.attempted} "
        f"attempts failed ({extraction.top_error_class})"
    )


def starved_provider_clause(
    extraction: ExtractionOutcomes, trail: Sequence[Mapping[str, Any]]
) -> str:
    """The ` — <provider> failing: …` suffix for an empty-pool exhaustion error.

    Empty when no provider dominates the failures: a run that really did search
    the world and come back with nothing keeps the plain budget message, because
    blaming infrastructure for a genuine research result is the same defect in
    the other direction.
    """
    clause = _search_clause(trail) or _extraction_clause(extraction)
    return f" — {clause}" if clause else ""


# ---------------------------------------------------------------------------
# The four things the error has to radiate: why, state now, next action, and
# what is still allowed. Naming the fault was half the job; an operator reading
# "extraction provider failing" still had to work out what to go DO about it,
# and the run's own state already knows.
# ---------------------------------------------------------------------------

#: What a human actually has to change, per extraction failure class. Every key
#: is a class the retrieval layer really produces (`_extraction_error_class`),
#: so a message can never name a repair for a fault that did not happen.
_EXTRACTION_ACTIONS: dict[str, str] = {
    "auth_rejected": (
        "the extraction provider rejected our credential — correct its API key in "
        "the research source settings, then ask this question again"
    ),
    "rate_limited": (
        "the extraction provider refused for quota or rate, not for load — wait for "
        "its window to reset or raise the plan, then ask this question again"
    ),
    "anti_bot": (
        "every discovered source blocked automated reading — this is not an outage: "
        "point the run at a source set that publishes readable primary documents, or "
        "attach the material directly as an upload"
    ),
    "paywalled": (
        "every discovered source was paywalled — attach the material directly as an "
        "upload, or point the run at open-access sources"
    ),
    "not_found": (
        "every discovered URL was gone — the search provider is returning stale "
        "links; change the search provider in the research source settings and ask "
        "again"
    ),
    "timeout": (
        "the extraction service did not answer in time — restart it or restore its "
        "endpoint in the research source settings, then ask this question again"
    ),
    "empty_content": (
        "the extractor answered but returned nothing readable — check its rendering "
        "mode and endpoint in the research source settings, then ask again"
    ),
}


def _extraction_action(error_class: str) -> str:
    return _EXTRACTION_ACTIONS.get(
        error_class,
        f"the extraction service is failing with {error_class} — restore it (its "
        "endpoint is in the research source settings), then ask this question again",
    )


def _search_action(marker: str, cooling: Mapping[str, float]) -> str:
    if cooling:
        until = datetime.datetime.now(datetime.UTC) + datetime.timedelta(
            seconds=max(cooling.values())
        )
        verb = "is" if len(cooling) == 1 else "are"
        return (
            f"{name_engines(cooling)} {verb} cooling until {until.strftime('%H:%M UTC')}"
            " — ask again after that, or choose a different search provider in the "
            "research source settings"
        )
    return (
        f"the search provider returned no usable response ({marker}) — check its "
        "endpoint and key in the research source settings, then ask this question again"
    )


def _no_provider_action(trail: Sequence[Mapping[str, Any]]) -> str:
    if any(row.get("yield_reason") == DISCOVERED for row in trail):
        return (
            "search found source leads, but the run did not acquire usable evidence "
            "before its work limit. This does not establish that an answer is absent. "
            "Ask again with a specific original source URL or attach the source material"
        )
    return (
        "the searches reached the world and it had nothing usable — this is a result, "
        "not an outage: widen or re-angle the question, or attach the material you "
        "already have as an upload, and ask again"
    )


def _state_line(*, sources_retained: int, turns_used: int, turns_total: int, queued: int) -> str:
    pool = (
        f"{sources_retained} sources are in the evidence pool and travel with this run's trace"
        if sources_retained
        else "no sources were admitted; the conversation and research trace remain available"
    )
    queued_clause = (
        f"; {queued} queries were still queued for the host to retry and were never tested"
        if queued
        else ""
    )
    return f"{pool}; {turns_used} of {turns_total} research turns used{queued_clause}"


#: What stays available after ANY terminal research failure. One sentence, one
#: place: the conversation keeps the question and its settings, so the fix is
#: followed by asking again rather than by rebuilding the run.
_ALLOWED = (
    "the question and its depth, recency and source settings stay on this "
    "conversation, so asking again after the fix starts from them."
)


def exhaustion_failure(
    *,
    budget: Literal["turn", "source", "infrastructure"],
    extraction: ExtractionOutcomes,
    trail: Sequence[Mapping[str, Any]],
    sources_retained: int,
    turns_used: int,
    turns_total: int,
    cooling: Mapping[str, float] | None = None,
    queued: int = 0,
    streak: int = 0,
) -> RunFailure:
    """The empty-pool exhaustion failure: class, why, state, next action, allowed.

    ``cooling`` is the live per-engine cooldown registry, so the search class can
    name a real deadline instead of a shrug. ``budget="infrastructure"`` is the
    circuit breaker: the run never spent its turn budget because nothing it asked
    ever reached the world, so saying it "exhausted its turn budget" would be a
    lie in the direction that hides the fault.

    The CLASS comes from the branch taken here — which provider's failures
    dominated, or which wall raised — never from reading the sentence back. A
    run that genuinely searched the world and found nothing is
    ``no_usable_evidence``: a result, and the UI must be able to stop drawing it
    as an outage.
    """
    search = _search_finding(trail)
    clause = search[0] if search is not None else _extraction_clause(extraction)
    head = (
        f"research stopped after {streak} consecutive research turns whose every "
        "query died in infrastructure and never reached the world"
        if budget == "infrastructure"
        else f"research exhausted its {budget} budget without usable evidence"
    )
    if search is not None:
        action = _search_action(search[1], cooling or {})
        failure_class: RunFailureClass = "search_infrastructure"
    elif clause is not None:
        action = _extraction_action(extraction.top_error_class)
        failure_class = "extraction_infrastructure"
    else:
        action = _no_provider_action(trail)
        failure_class = "no_usable_evidence"
    if budget == "infrastructure":
        # The circuit breaker is the wall that raised; the provider clause above
        # still names what broke, inside `why`.
        failure_class = "host_circuit_breaker"
    return RunFailure(
        failure_class=failure_class,
        why=head + (f" — {clause}" if clause else ""),
        state=_state_line(
            sources_retained=sources_retained,
            turns_used=turns_used,
            turns_total=turns_total,
            queued=queued,
        ),
        next=action,
        allowed=_ALLOWED,
    )


def exhaustion_error(
    *,
    budget: Literal["turn", "source", "infrastructure"],
    extraction: ExtractionOutcomes,
    trail: Sequence[Mapping[str, Any]],
    sources_retained: int,
    turns_used: int,
    turns_total: int,
    cooling: Mapping[str, float] | None = None,
    queued: int = 0,
    streak: int = 0,
) -> str:
    """The exhaustion failure as the one sentence that has always shipped."""
    return exhaustion_failure(
        budget=budget,
        extraction=extraction,
        trail=trail,
        sources_retained=sources_retained,
        turns_used=turns_used,
        turns_total=turns_total,
        cooling=cooling,
        queued=queued,
        streak=streak,
    ).detail


# ---------------------------------------------------------------------------
# The other terminal class: the driver could not sustain the turn protocol.
# ---------------------------------------------------------------------------

#: A malformed-turn error used to be the one wall in this run with no angle at
#: all: "3 consecutive malformed research turns; last parse error: X". An
#: operator reading that knows something is wrong and has nothing to go do. The
#: two real levers are named below, and WHICH one is named depends on how the
#: replies actually failed — a reply that was cut off at the ceiling is fixed by
#: room, and a reply that arrived complete and off-schema is not.
_CEILING_SHAPES = frozenset({"ceiling_hit", "empty_response", "truncated_turn"})


def malformed_turn_failure(
    *,
    streak: int,
    reasks_per_turn: int,
    last_error: str | None,
    last_shape: str,
    ceiling_tokens: int,
    sources_retained: int,
    turns_used: int,
    turns_total: int,
) -> RunFailure:
    """The terminal malformed-turn failure, with the four parts.

    ``last_shape`` is the classification the call path already made
    (``_output_ceiling.turn_failure_shape``) — not a re-reading of the error
    text — so the next action names the lever that matches the failure.
    """
    calls = streak * (1 + reasks_per_turn)
    why = (
        f"the research driver could not sustain the turn protocol: {streak} "
        f"consecutive research turns were unusable across {calls} calls "
        f"({reasks_per_turn} re-ask each, with parser feedback); "
        f"the last reply failed as {last_shape}" + (f" — {last_error}" if last_error else "")
    )
    if last_shape in _CEILING_SHAPES:
        action = (
            "the replies were cut off at the output ceiling, which this run had "
            f"already raised to {ceiling_tokens} tokens — raise "
            "DISCO_RESEARCH_TURN_MAX_TOKENS above that, or pick a deep-research "
            "driver whose reasoning fits inside it in the model settings"
        )
    else:
        action = (
            "the replies arrived complete and did not match the turn schema — "
            "report this run with its model_io trace so the strict-JSON protocol, "
            "adapter response and repair feedback can be checked together; "
            "the trace retains each reply and the instructions sent for it. "
            "Do not repeat the unchanged run until that boundary is corrected"
        )
    return RunFailure(
        failure_class="model_protocol",
        why=why,
        state=_state_line(
            sources_retained=sources_retained,
            turns_used=turns_used,
            turns_total=turns_total,
            queued=0,
        ),
        next=action,
        allowed=_ALLOWED,
    )


__all__ = [
    "DOMINANT_FAILURE_FRACTION",
    "EXTRACTION_MIN_ATTEMPTS",
    "SEARCH_MIN_QUERIES",
    "ExtractionOutcomes",
    "exhaustion_error",
    "exhaustion_failure",
    "malformed_turn_failure",
    "starved_provider_clause",
]
