"""Angles the research could never reach, and what the writer is told about them.

The measured defect this exists for: when retrieval failed to find something,
the report asserted the thing does not exist. A live run stated that no FDA
approval existed for three indications — all three approvals were real, and
every search for them had been rate-limited to zero results and never
semantically tested. The run's own trail knew that. The writer did not, so
"nothing in front of me covers X" became "X does not exist", and whole research
areas were graded on evidence that was never allowed to arrive.

The rule that fixes it is a fact about the host, not a judgment about the
subject: a query the search or extraction layer never put to the world produces
the same empty pool as a subject with genuinely nothing written about it, and
the two are indistinguishable downstream unless the writer is told which is
which. So the writer is told — verbatim queries, plus one prohibition.

The untested classes are exactly the ones :func:`_search_outcomes.
semantically_tested_queries` already treats as re-issuable — `provider_degraded`
(which covers a rate limit), `extraction_failure`, and a transport-level
`failed` — because the same fact drives both rules. A query is an untested
ANGLE only when EVERY issue of it ended that way: one real result about a query
is a real result, and the pool's silence after it is a finding.

Nothing here may hint that a report with an unfixed finding can still ship, and
nothing here may teach the model to narrate its own plumbing (rubric R7) — the
block says what may not be concluded, and forbids naming the queries or the
research in the report.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ._search_outcomes import UNTESTED_YIELD_REASONS, normalize_query, query_tokens

#: Bound on the queries rendered into the prompt. A starved run can have
#: hundreds; the block has to stay readable beside a 220k-character evidence
#: block, and the overflow is counted rather than dropped silently.
MAX_RENDERED_ANGLES = 40


def untested_angles(trail: list[dict[str, Any]]) -> tuple[str, ...]:
    """The verbatim queries whose EVERY issue died in a host outage.

    Deterministic and derived only from the trail the run already recorded: a
    `search` row whose `yield_reason` is an untested class, or whose `result`
    is ``"failed"``, never reached the world. A query with any other outcome
    was tested, and one tested issue removes it from this set entirely.

    `query_rejected` rows are deliberately ignored. A refused query was never
    issued at all — it is a near-duplicate of one that was, and that one's
    outcome already governs the angle.
    """
    verbatim: dict[str, str] = {}
    tested: set[str] = set()
    for entry in trail:
        if entry.get("kind") != "search" or not entry.get("query"):
            continue
        normalized = normalize_query(str(entry["query"]))
        if not normalized:
            continue
        if entry.get("yield_reason") in UNTESTED_YIELD_REASONS or entry.get("result") == "failed":
            verbatim.setdefault(normalized, str(entry["query"]))
        else:
            tested.add(normalized)
    return tuple(query for key, query in verbatim.items() if key not in tested)


# The prompt block. It states one host fact and one prohibition, and it stays
# out of the report's own vocabulary: it never uses the host words rubric R7
# forbids in prose, and it closes by forbidding the report from naming the
# queries or the research at all.
UNTESTED_ANGLES_HEADER = (
    "UNTESTED ANGLES — the queries below were put to the search layer during "
    "this research and never reached the world: the search or extraction "
    "layer failed on every attempt, so nothing about them could enter the "
    "evidence you were given.\n"
)

UNTESTED_ANGLES_RULE = (
    "These angles were never reachable during this research. The silence of "
    "the evidence you were given about them is an infrastructure fact, not a "
    "finding. You MAY treat the question each one covers as open and say so "
    "in the subject's own terms. You may NOT assert that their subject matter "
    "is absent, unapproved, nonexistent, unstudied, or hype on the basis of "
    "that silence, and you may NOT grade, rank, or score an area on it. Never "
    "name these queries, this list, or the research itself in the report."
)

_OVERFLOW_LINE = "- …and {count} further queries in the same state"


def format_untested_angles(angles: Sequence[str]) -> str:
    """The block appended to the writer's prompt context — empty when the run
    tested every query it issued."""
    if not angles:
        return ""
    lines = [f'- "{angle}"' for angle in angles[:MAX_RENDERED_ANGLES]]
    overflow = len(angles) - MAX_RENDERED_ANGLES
    if overflow > 0:
        lines.append(_OVERFLOW_LINE.format(count=overflow))
    return f"{UNTESTED_ANGLES_HEADER}" + "\n".join(lines) + f"\n{UNTESTED_ANGLES_RULE}\n\n"


def untested_angle_tokens(
    angles: Sequence[str],
) -> tuple[tuple[str, frozenset[str]], ...]:
    """Each angle beside the token set a sentence is compared against.

    The SAME tokenizer the query-freshness wall uses, for the same reason it
    exists there: this is host mechanics — does this sentence talk about the
    query the host could not run — never evidence judgment. An angle whose
    token set is empty is dropped rather than matched by fallback; it could
    only produce noise.
    """
    return tuple((angle, tokens) for angle in angles if (tokens := query_tokens(angle)))


def untested_angles_trail_entry(count: int) -> dict[str, Any]:
    """The run's audit-trail row naming how many angles research never reached.

    Mirrors the `verifier_degraded` / `report_unverified` rows: the trace and
    the report metadata tell the same story. Report-path trail rows are never
    replayed into a prompt.
    """
    return {"kind": "untested_angles", "angles": count}


__all__ = [
    "MAX_RENDERED_ANGLES",
    "UNTESTED_ANGLES_HEADER",
    "UNTESTED_ANGLES_RULE",
    "format_untested_angles",
    "untested_angle_tokens",
    "untested_angles",
    "untested_angles_trail_entry",
]
