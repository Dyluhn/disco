"""What the host has actually banked for each angle the model named.

THE MEASURED DEFECT
-------------------
Smoke run 2026-09-02 (quick tier, US office CRE). Turn 1 issued "Trepp CMBS
office delinquency rate 2024 2025 2026 MBA delinquency" and banked two
passages. The model proposed the same query again on turns 3 and 5; the
freshness wall refused both, correctly, and the harness recorded three
``REPEATED_QUERY`` findings. The wall was not the problem — the model
re-proposed because the subquestion "loan delinquencies" stayed OPEN in its own
coverage list while the pool already held evidence for it, and nothing in its
context connected the two. The trail it saw is query-centric: a list of
``{turn, query, admitted, result}`` rows with no statement of which ANGLE each
query fed or which angles the pool now answers. So the model re-derived that
mapping from memory every turn, and re-derivation from memory is exactly what a
loop exists to remove.

Presence implies affordance, and its converse: what the model cannot see, it
re-invents. This module gives every turn a per-angle ledger — queries issued
for it and how each ended, passages and distinct domains banked, and the
HOST's verdict on its state.

WHERE THE FACTS COME FROM
-------------------------
Every number here is read from the run's audit trail — the rows the search path
wrote when it really issued a query and really admitted a passage — and from
the evidence pool. Nothing is read from the model's narration. The model
supplies only the angle TEXT (its own ``coverage.open`` / ``coverage.covered``
entries); the host supplies every fact attached to it.

TWO MATCHING RULES, BOTH HOST MECHANICS
---------------------------------------
1. **Angle identity across turns.** There is none, deliberately. The model
   rewords its own angles every turn (measured on the smoke run: consecutive
   restatements of one angle score 0.27-1.00 token-set Jaccard against each
   other, while different angles score 0.04-0.24 — separable, but not by a
   fixed threshold). Rather than carry a fragile cross-turn identity, the
   ledger is recomputed from scratch each turn against the angles as the model
   states them NOW. The trail does not move, so the same query attributes to
   the same angle through a rewording; an angle the model splits or merges
   simply gets the queries its current words claim.

2. **Query attribution.** A query belongs to the angle that contains most of
   its content words — ``|query ∩ angle| / |query|``, using the same tokenizer
   the freshness wall uses, for the same reason it exists there: this is host
   bookkeeping ("which angle did this query serve"), never evidence judgment.
   The rule against Jaccard-as-judgment in ``_search_outcomes`` governs
   evidence and theme clustering — deciding two passages say the same thing —
   and nothing here may ever be used for that. No passage is merged, moved, or
   hidden by this module; the writer's evidence is untouched.

   A query must clear :data:`MIN_QUERY_ATTRIBUTION` AND beat the runner-up
   outright. A tie means the query served several angles at once and the host
   will not guess: it is attributed to none and never refused by the
   exhaustion wall. That count is kept in the trail row and is deliberately NOT
   rendered into the prompt — while it was, the model read "counted for none of
   them" as a defect to repair and started writing queries shaped to attribute,
   which the freshness wall then refused (run-05 t4/t5, run-11 t6/t7, run-12 of
   batch B). A bookkeeping number the model cannot act on is an instruction it
   will try to act on anyway.

   A REFUSED query is attributed through the query it repeats, not through its
   own words. The wall's verdict is that the two are one query to the host, so
   the refusal belongs wherever the issue went — beside it, where the model
   reads the two as one attempt and its outcome. Its own words are the fallback
   only when the repeated query attributes to nothing. Own-words attribution of
   a reword is not stable: a reword drops and adds tokens, and the containment
   moves with them (post-lane run-03, 2026-09-02: the issued WoodMac LCOS query
   scored 0.44/0.31 against its two candidate angles, its refused rewords
   0.33/0.20 and 0.29/0.14), so a refusal can land under a different angle than
   the query it repeats, or under none, and the model reads the wall in the
   wrong place or not at all.

THREE STATES, AND ONLY ONE OF THEM IS A WALL
--------------------------------------------
* **open** — nothing decided yet.
* **banked** — the pool holds at least :data:`BANKED_MIN_PASSAGES` passages
  from at least :data:`BANKED_MIN_DOMAINS` distinct domains attributed to it,
  or the model declared it ``covered`` with ``evidence_ids`` that are all
  really in the pool. The banked floor is the DEPTH TIER's own — see
  :func:`banked_domain_floor` — so a 240-source survey is not told an angle
  has its evidence on the same two domains that suffice for a 30-source quick
  check.
* **exhausted** — :data:`MAX_TESTED_ZERO_YIELDS` distinct queries reached the
  world for it and admitted nothing. Untested queries (a search or extraction
  outage) never count: a query the host could not put to the world says
  nothing about the angle, and closing an angle on infrastructure would be the
  same category error the turn budget used to make.

**A BANKED ANGLE IS NOT A CLOSED ANGLE**, and :func:`exhausted_for_query`
refuses only ``exhausted``. This is the measured defect of batch B
(``~/AI-Work/disco-research-v2-2026-09-01/phase5/batchB-wave2-muse``, 13 hosted
runs, standard depth): 490 proposed queries, 104 never reached the world, and a
replay of these walls attributed 88 of the refusals to the banked floor being
used as a ceiling. The queries it refused were the ones the research prompt
orders — "CHASE CITATIONS UPSTREAM … search the exact title or DOI" (a NEJM DOI
verification, refused because the angle already held ten passages),
"CROSS-VALIDATE LOAD-BEARING CLAIMS" (the NREL, Lazard and EIA primary sources
for a cost question, refused because secondary coverage had met the floor).
Four of thirteen runs then spent decision text on getting queries PAST the wall
rather than on the question. A floor that says "there is enough here to write"
can never be read as "there is no more work here": cross-validation, upstream
chases and primary-source pivots begin exactly where the floor is met.

Exhaustion is the one state in which a further query is provably wasted — the
host put N distinct queries to the world and admitted nothing — so it is the
one state that refuses. It is a wall the model can see through: the ledger
shows the state and every attempt before any query is refused, so a refusal is
never a surprise. And the wall stands down entirely when EVERY named angle is
exhausted, because a wall across the whole board is a maze rather than a
funnel — see :func:`exhausted_for_query`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from ._search_outcomes import (
    DISCOVERED,
    NARROWING_HINT,
    UNTESTED_YIELD_REASONS,
    normalize_query,
    query_tokens,
)

#: The fraction of a planned query's content words that must appear in an
#: angle's own words before the host will say the query served that angle.
#:
#: Measured on the smoke run's 18 issued queries: every query that really
#: served one of the listed angles scored 0.27-0.64 against it, and the one
#: query that served no single listed angle scored 0.14 against its best
#: match — tied with a second angle, so the tie-break below would have
#: rejected it anyway. The floor sits under the real band and over the noise.
MIN_QUERY_ATTRIBUTION = 0.25

#: The smallest evidence body that is not one source repeating itself. Two
#: passages from two domains, because the run's own rules already say so
#: elsewhere: ``distinct_work_count`` counts works rather than passages, and
#: the research prompt states "multiple pages from one domain are one source,
#: not independent corroboration". One domain is one source, however many
#: pages it yielded, so on its own it never banks an angle. This is the FLOOR;
#: the depth tier raises it (see :func:`banked_domain_floor`).
BANKED_MIN_PASSAGES = 2
BANKED_MIN_DOMAINS = 2

#: How many distinct queries may reach the world for one angle and admit
#: nothing before the host closes it. Three, because the research prompt's own
#: dead-end rule ("after two distinct zero-yield attempts … pivot") is the
#: model's instruction for the same situation, and a host wall that fires
#: earlier than the instruction it replaces would refuse work the model was
#: told to do. The third attempt is the one the host stops.
MAX_TESTED_ZERO_YIELDS = 3

#: Rendering bounds. A starved run can carry dozens of angles and hundreds of
#: queries; the ledger sits beside a very large evidence digest and has to stay
#: readable. Overflow is COUNTED, never silently dropped.
MAX_LEDGER_ANGLES = 24
MAX_LEDGER_QUERIES = 6

_QUERY_CHARS = 92

#: The three ledger states. Only ``exhausted`` refuses a query; ``banked`` says
#: the angle can be written from what the pool already holds, and further
#: queries for it are welcome.
LedgerState = Literal["open", "banked", "exhausted"]

#: A refused row is a wall the model reads again on every later turn, so it
#: carries its own angle: not only that the query was refused, but what would
#: issue. Without it (measured, post-lane run-03) a query refused at turn 29
#: came back bare at turn 32 — the narrowing rule had been stated once, in the
#: turn-30 feedback, and was three turns behind the row the model was reading.
_REPEAT_OUTCOME = f"refused as a repeat — {NARROWING_HINT}"

_REFUSED_ROW_OUTCOMES = {
    "repeat": _REPEAT_OUTCOME,
    "near_duplicate": _REPEAT_OUTCOME,
    "queued": "queued for the host to re-run",
    "retry_budget_spent": "unreached — the host's retry budget is spent",
    "exhausted": "refused: this angle is a dead end",
    # A checkpoint written before the three-state ledger. Reading it as a plain
    # repeat would tell a resumed run the wrong reason for its own refusal.
    "retired": "refused: this angle is a dead end",
    "empty": "empty, never issued",
}


def _clip(text: str, limit: int = _QUERY_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def banked_domain_floor(min_evidence_sources: int, min_evidence_themes: int) -> int:
    """How many independent domains bank one angle at this depth.

    Derived from the tier's OWN declared floor rather than from a new number: a
    tier that asks for ``min_evidence_sources`` distinct works across
    ``min_evidence_themes`` major angles has already stated how much evidence
    one angle is worth there. Quick lands on 2, standard on 3, exhaustive on 3 —
    so a 240-source survey is not told an angle has its evidence on the same two
    domains that suffice for a 30-source quick check.

    Never below :data:`BANKED_MIN_DOMAINS`.
    """
    if min_evidence_sources <= 0 or min_evidence_themes <= 0:
        return BANKED_MIN_DOMAINS
    return max(BANKED_MIN_DOMAINS, -(-min_evidence_sources // min_evidence_themes))


@dataclass(frozen=True)
class LedgerQuery:
    """One query the host issued or refused for an angle, and how it ended."""

    query: str
    turn: int | None
    outcome: str
    admitted: int = 0
    #: True when the query never reached the world (search/extraction outage or
    #: a transport failure). Such a query is evidence about the host, not about
    #: the angle, so the exhaustion rule steps over it.
    untested: bool = False
    #: True when the host refused the query before issuing it.
    refused: bool = False
    #: For a refused row, the earlier query the wall said it repeats. The
    #: refusal is attributed through this query (see the module docstring).
    repeats: str | None = None
    #: The domains this query put into the pool, as the search row recorded
    #: them. Empty on a row written before the field existed (a checkpoint from
    #: an older build) — the admissions still count, only the domain roll-up
    #: is thinner, which errs toward keeping an angle open.
    domains: tuple[str, ...] = ()


@dataclass(frozen=True)
class Subquestion:
    """One angle the model named, with the host's facts and verdict attached."""

    angle: str
    declared: Literal["open", "covered"]
    state: LedgerState
    why: str
    queries: tuple[LedgerQuery, ...]
    admitted: int
    domains: tuple[str, ...]

    @property
    def exhausted(self) -> bool:
        """True only for a dead end — the one state that refuses a query."""
        return self.state == "exhausted"

    @property
    def evidence_line(self) -> str:
        """What the pool holds for this angle, in one clause."""
        if not self.admitted:
            return "nothing banked yet"
        shown = ", ".join(self.domains[:3])
        more = len(self.domains) - 3
        where = f" ({shown}{f', +{more} more' if more > 0 else ''})" if self.domains else ""
        passages = _plural(self.admitted, "passage")
        return f"{passages} from {_plural(len(self.domains), 'domain')}{where}"


# ---------------------------------------------------------------------------
# Reading the trail.
# ---------------------------------------------------------------------------


def _search_outcome_text(entry: Mapping[str, Any]) -> str:
    admitted = entry.get("admitted")
    if isinstance(admitted, int) and admitted > 0:
        return f"admitted {admitted}"
    reason = entry.get("yield_reason")
    if isinstance(reason, str) and reason:
        return reason
    if entry.get("result") == "failed":
        return "transport failed"
    return "admitted 0"


def _row_domains(entry: Mapping[str, Any]) -> tuple[str, ...]:
    raw = entry.get("admitted_domains")
    if not isinstance(raw, list):
        return ()
    return tuple(item for item in raw if isinstance(item, str) and item)


def _issued_query(entry: Mapping[str, Any]) -> LedgerQuery | None:
    query = entry.get("query")
    if not isinstance(query, str) or not query.strip():
        return None
    turn = entry.get("turn")
    admitted = entry.get("admitted")
    return LedgerQuery(
        query=query,
        turn=turn if isinstance(turn, int) else None,
        outcome=_search_outcome_text(entry),
        admitted=admitted if isinstance(admitted, int) and admitted > 0 else 0,
        untested=entry.get("yield_reason") in UNTESTED_YIELD_REASONS
        or entry.get("result") == "failed",
        domains=_row_domains(entry),
    )


def _refused_query(entry: Mapping[str, Any]) -> LedgerQuery | None:
    query = entry.get("query")
    if not isinstance(query, str) or not query.strip():
        return None
    turn = entry.get("turn")
    rejected = entry.get("rejected")
    repeats = entry.get("duplicates")
    return LedgerQuery(
        query=query,
        turn=turn if isinstance(turn, int) else None,
        outcome=_REFUSED_ROW_OUTCOMES.get(str(rejected), "refused"),
        untested=True,
        refused=True,
        repeats=repeats if isinstance(repeats, str) and repeats.strip() else None,
    )


def _trail_queries(trail: Sequence[Mapping[str, Any]]) -> list[LedgerQuery]:
    """Every query this run issued or refused, in trail order, deduped.

    Deduped on the normalized query because a system re-issue runs the SAME
    query again: the angle gained one attempt, not two, and its later outcome
    is the one that stands.
    """
    by_query: dict[str, LedgerQuery] = {}
    for entry in trail:
        kind = entry.get("kind")
        if kind == "search":
            row = _issued_query(entry)
        elif kind == "query_rejected":
            row = _refused_query(entry)
        else:
            continue
        if row is None:
            continue
        key = normalize_query(row.query)
        if not key:
            continue
        prior = by_query.get(key)
        # A refusal never overwrites a real issue: the issue is what happened.
        if prior is not None and row.refused and not prior.refused:
            continue
        by_query[key] = row
    return list(by_query.values())


# ---------------------------------------------------------------------------
# Attribution.
# ---------------------------------------------------------------------------


def _containment(query: frozenset[str], angle: frozenset[str]) -> float:
    if not query or not angle:
        return 0.0
    return len(query & angle) / len(query)


def attribute(query: str, angle_tokens: Sequence[tuple[int, frozenset[str]]]) -> int | None:
    """The index of the angle this query served, or None for none of them.

    None is a real answer, not a failure: a query that spans two angles or
    names none of them is the model's to run, and the exhaustion wall never
    touches it. See the module docstring for why a tie is refused rather than
    broken.
    """
    tokens = query_tokens(query)
    if not tokens:
        return None
    best_index, best, runner_up = None, 0.0, 0.0
    for index, angle in angle_tokens:
        score = _containment(tokens, angle)
        if score > best:
            best_index, best, runner_up = index, score, best
        elif score > runner_up:
            runner_up = score
    if best < MIN_QUERY_ATTRIBUTION or best <= runner_up:
        return None
    return best_index


# ---------------------------------------------------------------------------
# The host's verdict.
# ---------------------------------------------------------------------------


def _declared_angles(
    coverage: Mapping[str, Any],
) -> list[tuple[str, Literal["open", "covered"], tuple[str, ...]]]:
    """The angles the model named this turn, with any evidence ids it claimed."""
    angles: list[tuple[str, Literal["open", "covered"], tuple[str, ...]]] = []
    seen: set[str] = set()
    for item in coverage.get("covered") or []:
        if not isinstance(item, Mapping):
            continue
        angle = str(item.get("angle") or "").strip()
        ids = item.get("evidence_ids")
        if not angle or angle.casefold() in seen:
            continue
        seen.add(angle.casefold())
        angles.append(
            (
                angle,
                "covered",
                tuple(str(value) for value in ids if isinstance(value, str))
                if isinstance(ids, list)
                else (),
            )
        )
    for item in coverage.get("open") or []:
        angle = str(item).strip() if isinstance(item, str) else ""
        if not angle or angle.casefold() in seen:
            continue
        seen.add(angle.casefold())
        angles.append((angle, "open", ()))
    return angles


def _verdict(
    *,
    declared: Literal["open", "covered"],
    evidence_ids: tuple[str, ...],
    pool_ids: frozenset[str],
    admitted: int,
    domains: tuple[str, ...],
    tested_zero_yields: int,
    min_domains: int,
) -> tuple[LedgerState, str]:
    """The host's state for one angle, and the sentence that justifies it."""
    if declared == "covered" and evidence_ids and all(value in pool_ids for value in evidence_ids):
        return "banked", (
            f"you marked it covered and all {len(evidence_ids)} cited ids are in the pool"
        )
    if admitted >= BANKED_MIN_PASSAGES and len(domains) >= min_domains:
        return "banked", (
            f"the pool holds {_plural(admitted, 'passage')} from "
            f"{_plural(len(domains), 'independent domain')}"
        )
    if tested_zero_yields >= MAX_TESTED_ZERO_YIELDS:
        return "exhausted", (
            f"{tested_zero_yields} distinct queries reached the world for it and admitted nothing"
        )
    return "open", ""


def _tested_without_admission(rows: Sequence[LedgerQuery]) -> int:
    return sum(
        1
        for row in rows
        if not row.untested and not row.refused and row.admitted == 0 and row.outcome != DISCOVERED
    )


def build_ledger(
    coverage: Mapping[str, Any],
    trail: Sequence[Mapping[str, Any]],
    pool_ids: frozenset[str],
    *,
    min_domains: int = BANKED_MIN_DOMAINS,
) -> tuple[tuple[Subquestion, ...], int]:
    """The per-angle ledger for this turn, plus the unattributed query count.

    ``coverage`` is the model's own declaration (angle text only); ``trail``
    and ``pool_ids`` are the host's record of what really happened. Pure, and
    recomputed every turn — see the module docstring on why there is no
    cross-turn angle identity to drift.
    """
    declared = _declared_angles(coverage)
    angle_tokens = [
        (index, tokens)
        for index, (angle, _kind, _ids) in enumerate(declared)
        if (tokens := query_tokens(angle))
    ]
    buckets: dict[int, list[LedgerQuery]] = {index: [] for index, _ in enumerate(declared)}
    unattributed = 0
    for row in _trail_queries(trail):
        index = None
        if row.repeats is not None:
            # The wall said this IS the earlier query; it belongs where that went.
            index = attribute(row.repeats, angle_tokens)
        if index is None:
            index = attribute(row.query, angle_tokens)
        if index is None:
            unattributed += 1
            continue
        buckets[index].append(row)
    entries: list[Subquestion] = []
    for index, (angle, kind, evidence_ids) in enumerate(declared):
        rows = buckets[index]
        admitted = sum(row.admitted for row in rows)
        domains = sorted({domain for row in rows for domain in row.domains})
        state, why = _verdict(
            declared=kind,
            evidence_ids=evidence_ids,
            pool_ids=pool_ids,
            admitted=admitted,
            domains=tuple(domains),
            tested_zero_yields=_tested_without_admission(rows),
            min_domains=min_domains,
        )
        entries.append(
            Subquestion(
                angle=angle,
                declared=kind,
                state=state,
                why=why,
                queries=tuple(rows),
                admitted=admitted,
                domains=tuple(sorted(domains)),
            )
        )
    return tuple(entries), unattributed


def all_exhausted(entries: Sequence[Subquestion]) -> bool:
    """Whether every angle the model currently names is a proven dead end."""
    return bool(entries) and all(entry.exhausted for entry in entries)


def exhausted_for_query(query: str, entries: Sequence[Subquestion]) -> Subquestion | None:
    """The EXHAUSTED angle this planned query belongs to, if any.

    Exhausted only. A banked angle is not closed — the floor it met says the
    report can be written from what the pool holds, never that no further work
    on it is allowed, and the queries the loop asks for next (cross-validation,
    the upstream chase, the primary source behind a secondary summary) all land
    on angles whose floor is already met.

    The same attribution the ledger was built with, so the model is never
    refused for an angle whose ledger row it could not have read.

    The wall STANDS DOWN when every named angle is exhausted. A wall that
    blocks the entire board is a maze, not a funnel: the model would have no
    issuable query at all while the host's own readiness floors might still be
    unmet, and its only exit would be to guess that renaming an angle reopens
    it. In that state the ledger says so and the refusals stop; the run reaches
    a real wall (the readiness gate, or a hard budget) instead of an invisible
    one.
    """
    if all_exhausted(entries):
        return None
    angle_tokens = [
        (index, tokens)
        for index, entry in enumerate(entries)
        if (tokens := query_tokens(entry.angle))
    ]
    index = attribute(query, angle_tokens)
    if index is None:
        return None
    entry = entries[index]
    return entry if entry.exhausted else None


# ---------------------------------------------------------------------------
# Rendering.
# ---------------------------------------------------------------------------

LEDGER_HEADER = (
    "SUBQUESTION LEDGER — what this run has actually banked for each angle you "
    "named. Every count below is read from search results and the evidence "
    "pool, never from your notes. STATE is the host's record of the evidence, "
    "not a work order:\n"
    "  [OPEN] — nothing banked for it yet.\n"
    "  [BANKED] — the evidence floor for this angle is met: it can be written "
    "from the evidence already in the pool. It is NOT closed. Further queries "
    "for a banked angle are allowed and expected — cross-validating a load-bearing "
    "number against a second source, chasing a cited work upstream to the "
    "original paper, filing or DOI, or replacing a secondary summary with the "
    "primary source. They are judged only on being different from a query this "
    "run already put to the world.\n"
    "  [EXHAUSTED] — distinct queries for it reached the world and admitted "
    "nothing. This is the only state that refuses a query, because it is the "
    "only one where another query is provably wasted."
)


def _query_line(row: LedgerQuery) -> str:
    where = f"t{row.turn}" if row.turn is not None else "  "
    return f'    {where} "{_clip(row.query)}" → {row.outcome}'


def _entry_lines(entry: Subquestion) -> list[str]:
    head = "[OPEN]" if entry.state == "open" else f"[{entry.state.upper()} — {entry.why}]"
    lines = [f"  {head} {entry.angle}", f"    banked: {entry.evidence_line}"]
    for row in entry.queries[:MAX_LEDGER_QUERIES]:
        lines.append(_query_line(row))
    overflow = len(entry.queries) - MAX_LEDGER_QUERIES
    if overflow > 0:
        lines.append(f"    …and {overflow} further queries for this angle")
    if not entry.queries:
        lines.append("    no query has been attributed to this angle yet")
    return lines


def render_ledger(entries: Sequence[Subquestion]) -> str:
    """The ledger block for the turn message.

    Empty until the run has something to report. On the first turn every angle
    is new and every count is zero, and a page of "nothing banked yet" is an
    attention tax with no information in it — presence implies affordance, so
    an empty ledger is not present at all.

    The unattributed-query count is deliberately absent; it lives in
    :func:`ledger_trail_row` for the audit. See the module docstring.
    """
    if not entries or not any(entry.queries or entry.state != "open" for entry in entries):
        return ""
    lines = [LEDGER_HEADER]
    for entry in entries[:MAX_LEDGER_ANGLES]:
        lines.extend(_entry_lines(entry))
    overflow = len(entries) - MAX_LEDGER_ANGLES
    if overflow > 0:
        lines.append(f"  …and {overflow} further angles you named")
    if all_exhausted(entries):
        lines.append(
            "  Every angle you have named is exhausted, so nothing is refused on "
            "that basis this turn. Name the angles this question still needs in "
            "coverage.open, or mark readiness if the evidence floor is satisfied."
        )
    return "\n".join(lines)


def ledger_trail_row(
    turn: int, entries: Sequence[Subquestion], unattributed: int
) -> dict[str, Any]:
    """The audit row for one turn's ledger.

    The trail is what the harness and a resumed run read, so the host's verdict
    has to survive there rather than existing only inside a prompt string.
    """
    return {
        "kind": "subquestion_ledger",
        "turn": turn,
        "unattributed_queries": unattributed,
        "subquestions": [
            {
                "angle": entry.angle,
                "declared": entry.declared,
                "state": entry.state,
                "why": entry.why,
                "queries": len(entry.queries),
                "admitted": entry.admitted,
                "domains": list(entry.domains),
            }
            for entry in entries
        ],
    }


__all__ = [
    "LEDGER_HEADER",
    "MAX_LEDGER_ANGLES",
    "MAX_LEDGER_QUERIES",
    "MAX_TESTED_ZERO_YIELDS",
    "MIN_QUERY_ATTRIBUTION",
    "BANKED_MIN_DOMAINS",
    "BANKED_MIN_PASSAGES",
    "LedgerQuery",
    "LedgerState",
    "Subquestion",
    "all_exhausted",
    "attribute",
    "build_ledger",
    "exhausted_for_query",
    "ledger_trail_row",
    "render_ledger",
    "banked_domain_floor",
]
