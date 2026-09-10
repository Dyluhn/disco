"""Whose work did this research turn spend?

``max_research_turns`` is a budget for MODEL work — the turns the lead spends
deciding what to investigate next. For a long time the loop charged it for every
iteration, including the ones where nothing the model decided ever reached the
world: 2,547 of 4,753 recorded searches came back ``provider_degraded`` and 384
``extraction_failure``, and a dead search pool could therefore eat a whole
sixteen-turn budget and end the run with ``exhausted its turn budget``. The
model was billed for the infrastructure's downtime.

The rule here is one sentence: **a turn whose every issued query died in
infrastructure is not a turn the model spent.** Anything else is. In particular

* a turn with at least one query that really reached the world — including an
  honest zero-hit — is model work, because a clean zero IS a research result and
  pivoting off it is the model's job;
* a turn that issued no query at all (a queryless decision, or one whose every
  query the freshness wall refused) is model work, because the model chose it;
* a malformed turn is model work, because the protocol is the model's half of
  the contract and the re-ask has already been spent on it.

The decision is a pure function of the turn's per-query outcomes so the audit
trail can carry both the verdict (``turn_counted``) and the reason that produced
it, and so a test can pin the rule without driving a whole run.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .._transport_retry import PROVIDER_DEGRADED
from ._search_outcomes import EXTRACTION_FAILURE, QUERY_REJECTED_KIND

#: The per-query outcome for a retrieval call that raised — it blew up in
#: transport, so like the two provider outages it never tested the query.
TRANSPORT_FAILED = "transport_failed"

#: The per-query outcome for a query that reached the world and admitted
#: evidence. Named here so the producer and this rule cannot drift apart.
ADMITTED = "admitted"

#: Every outcome that means the host, not the world, answered the query.
INFRASTRUCTURE_OUTCOMES = frozenset({PROVIDER_DEGRADED, EXTRACTION_FAILURE, TRANSPORT_FAILED})

_REASON_MALFORMED = "malformed turn: the turn protocol is the model's half"
_REASON_NO_QUERIES = "no query was issued: the turn was the model's to spend"
_REASON_MODEL_WORK = "at least one query reached the world"
_REASON_INFRASTRUCTURE = "every issued query died in infrastructure, untested"


@dataclass(frozen=True)
class TurnCharge:
    """Whether one research turn consumes the model's turn budget, and why."""

    counted: bool
    reason: str

    def trail_fields(self) -> dict[str, object]:
        """The two audit fields every turn row carries."""
        return {"turn_counted": self.counted, "turn_counted_reason": self.reason}


def charge_for_search_turn(outcomes: Sequence[str]) -> TurnCharge:
    """The turn-budget verdict for one search turn's per-query outcomes.

    ``outcomes`` holds exactly one entry per query the turn ISSUED: the query's
    zero-yield class, :data:`TRANSPORT_FAILED` if the retrieval call raised, or
    :data:`ADMITTED` when it admitted evidence.
    """
    if not outcomes:
        return TurnCharge(counted=True, reason=_REASON_NO_QUERIES)
    if all(outcome in INFRASTRUCTURE_OUTCOMES for outcome in outcomes):
        return TurnCharge(counted=False, reason=_REASON_INFRASTRUCTURE)
    return TurnCharge(counted=True, reason=_REASON_MODEL_WORK)


def charge_for_malformed_turn() -> TurnCharge:
    """A malformed turn always consumes the budget — see the module docstring."""
    return TurnCharge(counted=True, reason=_REASON_MALFORMED)


#: One audit row per MODEL turn, carrying that turn's verdict. The per-search
#: ``search_turn_summary`` carries the same two fields, but only for turns that
#: issued a query — a turn whose every query the walls refused issues none and
#: would be invisible. This row is the exhaustive one, and it is what the report
#: rollup counts.
TURN_CHARGE_KIND = "turn_charge"


def turn_charge_trail_row(turn: int, charge: TurnCharge) -> dict[str, Any]:
    """The audit row for one model turn's turn-budget verdict."""
    return {"kind": TURN_CHARGE_KIND, "turn": turn, **charge.trail_fields()}


def turn_accounting_rollup(
    trail: Sequence[Mapping[str, Any]], *, total_turns: int
) -> dict[str, int]:
    """How the run's turn budget was actually spent, for the finished report.

    The trail rides on a Stop CHECKPOINT; the report does not, and a reader of a
    finished report still has to be able to tell "the run spent every turn on
    research" from "the run's turns were eaten by a rate-limited pool". Derived
    from the trail rather than from a live counter so it stays cumulative across
    a resume, exactly as the trail is.

    ``refused_turns`` is the third number, and it is a SUBSET of ``model_turns``:
    a turn whose every proposed query the walls refused issued nothing, and this
    module charges it to the model on purpose (``_REASON_NO_QUERIES``). Without
    it a report could say "every turn went to a search that came back with
    results" a few lines under a trace reading "Not searched ×3". A turn that
    proposed nothing at all carries the same reason and is NOT counted here —
    the refusal rows for the turn are what separate the two.
    """
    rows = [row for row in trail if row.get("kind") == TURN_CHARGE_KIND]
    counted = sum(1 for row in rows if row.get("turn_counted"))
    refused_on = {row.get("turn") for row in trail if row.get("kind") == QUERY_REJECTED_KIND}
    result = {
        "model_turns": counted,
        "degraded_turns": len(rows) - counted,
        "refused_turns": sum(
            1
            for row in rows
            if row.get("turn_counted_reason") == _REASON_NO_QUERIES
            and row.get("turn") in refused_on
        ),
        "of": total_turns,
    }
    inspected_on = {row.get("turn") for row in trail if row.get("kind") == "source_inspection"}
    if inspected_on:
        result["inspection_turns"] = sum(row.get("turn") in inspected_on for row in rows)
    return result


__all__ = [
    "ADMITTED",
    "TURN_CHARGE_KIND",
    "INFRASTRUCTURE_OUTCOMES",
    "TRANSPORT_FAILED",
    "TurnCharge",
    "charge_for_malformed_turn",
    "charge_for_search_turn",
    "turn_accounting_rollup",
    "turn_charge_trail_row",
]
