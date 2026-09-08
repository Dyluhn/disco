"""Terminal run failures as STRUCTURE — the product's own class, never a guess.

The harness has always kept ``errors``: the sentences the product wrote. A
batch tally that wanted to count outages apart from research results had to
bucket those sentences, and bucketing prose by pattern silently re-buckets
every time a message is reworded. It also cannot tell a run that searched the
world and found nothing from a run whose extractor was down — the two say very
different things in a report and used to say nearly the same thing in a tally.

So the product now puts its own classification on the wire
(``ErrorEvent.failure``, ``disco.core.events.RunFailure``): the class chosen by
the code path that RAISED, plus the four parts every wall owes its reader. This
module is the whole of the harness's side of that: read the payload, count the
classes, render them. Nothing here inspects a message.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

#: The wire shape of ``ErrorEvent.failure``. Named here so the artifact's
#: columns are fixed rather than whatever a payload happened to carry.
FAILURE_FIELDS = ("failure_class", "why", "state", "next", "allowed")


def failure_row(event: Mapping[str, Any]) -> dict[str, Any] | None:
    """The structured failure one terminal event declared, or None.

    None is the honest answer for an event with no ``failure`` — an older run,
    or a path that could not name a class. A zero count downstream then means
    "the product did not classify it", which is itself the finding, rather than
    a bucket this harness invented.
    """
    failure = event.get("failure")
    if not isinstance(failure, Mapping) or not failure.get("failure_class"):
        return None
    return {key: failure.get(key) for key in FAILURE_FIELDS}


def failure_telemetry(failures: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The classes the PRODUCT named, deduped, for the run's telemetry block."""
    return {
        "failure_classes": sorted(
            {str(row["failure_class"]) for row in failures if row.get("failure_class")}
        )
    }


def failures_markdown(failures: Sequence[Mapping[str, Any]]) -> list[str]:
    """The failure section of a run's summary, beside the prose and not instead.

    Each part on its own line, because that is the shape a reader grading a
    batch needs: what failed, where the run stands, what to go do, what is
    still available.
    """
    if not failures:
        return []
    lines = ["", "## Failure classes", ""]
    for row in failures:
        lines.extend(
            [
                f"- **{row.get('failure_class')}** — {row.get('why')}",
                f"  - state: {row.get('state')}",
                f"  - next: {row.get('next')}",
                f"  - still available: {row.get('allowed')}",
            ]
        )
    return lines


__all__ = ["FAILURE_FIELDS", "failure_row", "failure_telemetry", "failures_markdown"]
