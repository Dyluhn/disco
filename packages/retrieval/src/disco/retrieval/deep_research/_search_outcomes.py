"""How an empty research turn is classified, and what the host says about it.

Two outcomes look identical from outside and must never be confused:

* a **clean zero** — the query was really put to the world and the world had
  nothing. That is a research result, and the honest response is a pivot.
* a **provider outage** — the search infrastructure was degraded, the retrieval
  layer already re-issued the query below the turn loop until its attempts were
  exhausted, and the query was never semantically tested. That is a host fault.
  Telling the model to pivot here makes it spend turns reformulating an outage.

An **extraction** outage is the same fault one layer down, and it was being
reported as the first kind. The search provider answered, real sources were
discovered, and every fetch of them failed transiently or without a settled
source status — so the pool did not move, and the query is exactly as untested
as it would be under a search outage. Sending the model to pivot there is the
same wasted turn: no rewording of the query makes the extractor come back. It
gets the same treatment as a degraded search — its own infrastructure message,
and it never counts as an attempt against the freshness wall.

When every discovered source is explicitly ``not_found`` or ``paywalled``, the
host has tested the addresses and learned only that those addresses are
unavailable. That is a model-work result, not an extractor outage: it does not
disprove the research angle, and the model should find the original source's
correct address or an accessible primary version. Such a result is named
``source_unavailable`` and is never put into the infrastructure re-issue queue.

So each outage gets its own message, which says exactly what broke (named
engines and transport errors, or the failed fetches), the state those queries
are in now (untested, and queued for the host to run again by itself), the next
action for the model (spend this turn on other angles), and what remains
available meanwhile. It never carries a pivot instruction, and a pivot
instruction never carries it. The same split governs query freshness: a query
the host could never test does not count as attempted.

RETRYING IS THE HOST'S JOB, NOT THE MODEL'S
-------------------------------------------
These messages used to end with "you MAY re-issue them verbatim on a later
turn". Measured against 4,753 searches inside shipped runs, that clause is what
the harness was flagging as ``REPEATED_QUERY``: the model was spending its own
turns and tokens doing the host's retry, because the host told it to. The
clause and its affordance are gone. Untested queries now go into a loop-owned
re-issue queue (``_reissue_queue``) that the loop drains itself when the engines
that refused them come back, and a model plan that names a queued query is
refused with a message that says it is already queued. Retrying infrastructure
is system mechanics — the model never sees the retry, only its result.

QUERY IDENTITY (the freshness wall)
-----------------------------------
`normalize_query`, `query_tokens` and `queries_are_near_duplicates` answer one
narrow host question: *is this planned query the same query the host already
ran?* That is a **host mechanic**, the same family as exact-match URL dedup —
it decides whether a request is re-issued, and nothing else.

It is emphatically **not** evidence judgment. The design rule against "Jaccard
merges as judgment" governs evidence and theme clustering — deciding that two
passages say the same thing, or that two findings are one finding. Nothing here
may ever be used for that. Two sources that overlap in wording are still two
sources; only queries are collapsed, and only to refuse re-running work the host
already did.

The comparison is deterministic and dependency-free: casefold, split on
non-alphanumerics, drop a small fixed english stopword set, collapse the common
plural, compare token sets. A query whose token set is empty (all stopwords, or
entirely non-ASCII) falls back to exact normalized identity, never to
similarity.

NARROWING IS NOT REWORDING
--------------------------
The refusal above tells the model that a real pivot changes "the angle, the
source class, the domain, or the **specificity**" — and then a token-set
comparison refused exactly that. Measured over 13 recorded runs: 8 of 39
freshness refusals added a scope the matched query did not have — `site:eia.gov`
on a prior EIA query, `PDF` on a prior report query, the year `2026`, a chip
model number, a docket number. The model read the wall correctly and started
gaming it instead ("new phrasing to avoid duplicate filter").

So `added_narrowing` names the one thing a token set cannot see: a term that
changes WHERE the host looks or WHICH document it asks for. A search operator
(`site:`, `filetype:`, `inurl:`, `intitle:`), an exact phrase in quotes, a DOI,
or any 4+ digit number — a year, a page citation, a docket, a PMID, a model
number. A planned query is ISSUED when it carries one that EVERY query it
matched lacks, and the trail records what it narrowed
(`narrowing_trail_rows`). Everything else is a rewording and is still refused.
"Every query it matched" is the half that matters: a query that scopes an
earlier broad one but repeats a later one verbatim reaches exactly what that
later one already retrieved.

Deliberately NOT a narrowing: a plain new content word. "methodology" against a
query that already says "methods" is the same query said differently, and a rule
that counted any added token would refuse nothing at all — every near-duplicate
below Jaccard 1.0 has a token the other lacks. The threshold and the tokenizer
are untouched.
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .._direct_source import _source_url
from .._transport_retry import PROVIDER_DEGRADED, SearchDegradation
from ..models import Passage
from ..url_policy import source_url_key

#: Articles, conjunctions and prepositions only, and deliberately small. It
#: exists so "cost of X" and "cost for X" are one query — not so the wall can
#: guess at meaning.
_STOPWORDS = frozenset(
    {
        "a", "an", "the",
        "and", "but", "nor", "or", "so", "yet",
        "about", "across", "after", "against", "among", "around", "as", "at",
        "before", "between", "by", "during", "for", "from", "in", "into",
        "of", "on", "onto", "over", "per", "than", "through", "to", "under",
        "until", "up", "via", "with", "within", "without",
    }
)  # fmt: skip

#: Token-set Jaccard at or above this is the same query.
#:
#: Settled at 0.7 against measured behavior, not taste. The live paraphrase loop
#: ("solid-state batteries EVs commercialization pathways" vs "solid state
#: battery EV commercialization timeline") is six tokens each differing in one,
#: which is 5/7 = 0.714 — 0.8 would have let every one of those through.
#: Genuinely different angles that share the topic words land at 0.33–0.56, so
#: the band is wide on that side. The honest limit: one differing token in a
#: five-token query is 4/6 = 0.667 and stays issuable, while the same single
#: difference in a six-token query does not. Set arithmetic cannot know which
#: token carried the pivot; the wall errs toward letting shorter queries run.
NEAR_DUPLICATE_JACCARD = 0.7

_TOKEN_SPLIT = re.compile(r"[^0-9a-z]+")

#: A term that scopes a search rather than describing its topic. See NARROWING IS
#: NOT REWORDING above. Every branch is something a reader can point at in the
#: query text — no word list of "specific-sounding" vocabulary, which is
#: unfalsifiable and would make the wall a matter of taste.
_NARROWING = re.compile(
    r"""
      \b(?:site|filetype|inurl|intitle):\S+   # a search operator and its target
    | "[^"]{2,}"                              # an exact phrase, quoted
    | \b10\.\d{4,9}/\S+                       # a DOI
    | \bpdf\b                                 # the document itself, not a page about it
    | \d{4,}                                  # a year, page cite, docket, PMID, model number
    """,
    re.IGNORECASE | re.VERBOSE,
)

#: The audit rows this module writes. Named so the modules that read them
#: (`_turn_accounting`, `tested_query_records`) cannot drift from the writer.
QUERY_REJECTED_KIND = "query_rejected"
QUERY_NARROWED_KIND = "query_narrowed"

#: The yield class for a turn whose discovered sources were all unreadable: the
#: search provider answered, the extractor did not. Named here beside
#: ``PROVIDER_DEGRADED`` because the two are one rule — a query the host could
#: not put to the world — and the producer in `agent.py` reads it from here so
#: the classifier and the rules below cannot drift apart.
EXTRACTION_FAILURE = "extraction_failure"
SOURCE_UNAVAILABLE = "source_unavailable"

#: Yield classes that mean the query was never semantically tested. Neither may
#: block a re-issue: refusing one would forbid the only correct response to an
#: outage, which is to run the same query again once the layer recovers.
UNTESTED_YIELD_REASONS = frozenset({PROVIDER_DEGRADED, EXTRACTION_FAILURE})

_REFUSED_OUTCOME = "refused as a repeat, never issued"
_EXHAUSTED_OUTCOME = "refused: its angle was a proven dead end, never issued"
_SAME_TURN_OUTCOME = "planned earlier in this same turn"
_PIVOT_VOCABULARY = (
    "A real pivot changes the angle, the source class, the domain, or the "
    "specificity — not the wording."
)
#: What the wall will let through, said in the wall's own terms, because it is
#: the wall's own rule (`narrowing_terms`). Rule 3: the refusal has to name the
#: next action that actually works, not a category the model has to guess at.
_NARROWING_VOCABULARY = (
    "a NARROWING of a refused query IS issued, not refused — add a site: or "
    "filetype: scope, quote an exact title, name a DOI, or add a number the "
    "earlier query did not carry (a year, a page or docket citation, a PMID, a "
    "model number). Only a reworded version of the same query is refused."
)
#: The same rule at row size, for the place the model reads it on every LATER
#: turn: the subquestion ledger's refused row. The feedback above is issued
#: once, on the turn after the refusal; the ledger row persists. Measured
#: (post-lane batch run-03, 2026-09-02): the turn-30 feedback was acted on, and
#: on the wrap-up turn the model re-proposed the same query bare, reading a row
#: that said only "refused as a repeat". A wall angles where the ball touches it.
NARROWING_HINT = (
    "a narrowing issues: add site:/filetype:, an exact title in quotes, a DOI, "
    "or a year/PMID/docket number the earlier query lacked"
)


def normalize_query(query: str) -> str:
    """Search identity ignores case/spacing; direct reads preserve URL path case."""
    if url := _source_url(query):
        return "url:" + source_url_key(url)
    return " ".join(query.casefold().split())


def _stem(token: str) -> str:
    """Collapse the common english plural so "batteries"/"battery" are one token."""
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 2 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def query_tokens(query: str) -> frozenset[str]:
    """The token set two queries are compared by. See QUERY IDENTITY above."""
    if _source_url(query) is not None:
        return frozenset({normalize_query(query)})
    return frozenset(
        _stem(token)
        for token in _TOKEN_SPLIT.split(query.casefold())
        if token and token not in _STOPWORDS
    )


def queries_are_near_duplicates(left: frozenset[str], right: frozenset[str]) -> bool:
    """True when two token sets name the same query for the freshness wall."""
    if not left or not right:
        return False
    return len(left & right) / len(left | right) >= NEAR_DUPLICATE_JACCARD


def _narrowing_terms(query: str) -> set[str]:
    return {match.group(0).casefold() for match in _NARROWING.finditer(query)}


def added_narrowing(query: str, earlier: str) -> set[str]:
    """What `query` scopes that `earlier` did not — empty when it only rewords.

    See NARROWING IS NOT REWORDING above.
    """
    return _narrowing_terms(query) - _narrowing_terms(earlier)


def already_admitted_count(
    passages: Sequence[Passage], seen_ids: set[str], seen_urls: set[str]
) -> int:
    """How many retrieved passages were sources already in the evidence pool."""
    return sum(
        1
        for passage in passages
        if passage.id in seen_ids or source_url_key(passage.source_url) in seen_urls
    )


DISCOVERED = "discovered"


def zero_yield_detail(yield_reason: str, degradation: SearchDegradation | None) -> str:
    """The observation `detail` rendered in the trace and the UI."""
    if degradation is not None:
        return (
            f"Search infrastructure degraded ({degradation.describe()}); re-issued "
            f"{degradation.attempts} times below the turn loop and never semantically "
            "tested. Not a result about this query."
        )
    if yield_reason == DISCOVERED:
        return (
            "Search found unread leads. No web document was fetched or admitted. "
            "Choose relevant original sources from UNREAD SEARCH LEADS and put their "
            "complete URLs in queries to read them; search terms remain available "
            "for missing leads. Snippets are not evidence."
        )
    if yield_reason == EXTRACTION_FAILURE:
        return (
            "Extraction infrastructure failed: sources were discovered and every "
            "fetch of them failed, so the query was never semantically tested. "
            "Not a result about this query."
        )
    if yield_reason == SOURCE_UNAVAILABLE:
        return (
            "The discovered source addresses were unavailable (not found or paywalled). "
            "This does not disprove the research angle. Search for the original "
            "source's correct address or an accessible primary version."
        )
    return (
        "No usable evidence: "
        + yield_reason.replace("_", " ")
        + ". Pivot the query or trace the named source upstream."
    )


def only_untested_yields(yield_reasons: object) -> bool:
    """Whether a search turn's every empty query died in a host outage.

    Such a turn is not evidence that the lead is exhausted — it is not evidence
    about the lead at all — so a caller counting empty turns neither counts it
    nor breaks its streak on it. Search and extraction outages count alike, and
    the rule lives here with the rest of the untested-class rules rather than
    being restated at the one call site.
    """
    if not isinstance(yield_reasons, list) or not yield_reasons:
        return False
    return all(reason in UNTESTED_YIELD_REASONS for reason in yield_reasons)


def cooldown_until_text(cooling: Mapping[str, float] | None) -> str:
    """ "engines bing, brave, cooling until 14:32 UTC" — or "" when none are.

    The engines and the wait are STATE, and rule 3 says an error has to say what
    state the system is in now. The model cannot pick an engine, so this is not
    an affordance; it is the difference between "something is wrong" and a wait
    the reader can judge.
    """
    if not cooling:
        return ""
    longest = max(cooling.values())
    until = datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=longest)
    return f"{name_engines(cooling)}, cooling until {until.strftime('%H:%M UTC')}"


def name_engines(engines: Iterable[str]) -> str:
    """ "search engine brave" / "search engines brave, yahoo" — read aloud."""
    names = sorted(engines)
    label = "search engine" if len(names) == 1 else "search engines"
    return f"{label} {', '.join(names)}"


def _queue_state_clause(queued: Sequence[str], cooling: Mapping[str, float] | None) -> str:
    """What the host has already taken over, so the model does not take it back."""
    if not queued:
        return (
            "The host is not running them again: its retry budget for them is spent, so "
            "treat those angles as unreached rather than disproved."
        )
    where = cooldown_until_text(cooling)
    behind = f" behind the {where}" if where else " as soon as the layer answers"
    return (
        f"The host has queued {len(queued)} of them to run again by itself{behind}. "
        "Their results will arrive as ordinary observations on a later turn."
    )


def _infrastructure_feedback(
    headline: str,
    named: str,
    broke: str,
    queued: Sequence[str],
    cooling: Mapping[str, float] | None,
) -> str:
    """One radiant infrastructure block: why, state now, next action, allowed.

    Every outage class shares this shape because they share the whole situation:
    the query never reached the world, the host owns getting it there, and the
    only thing the model should do about it is spend the turn elsewhere.
    """
    return (
        f"{headline} — a host/provider fault, not a research result. NOT "
        f"SEMANTICALLY TESTED: {named}. {broke} Their empty result says nothing "
        "about the query, the angle, or the availability of evidence, so do not "
        "treat it as a dead end. STATE: "
        f"{_queue_state_clause(queued, cooling)} NEXT: spend this turn on "
        "different angles — do not run or paraphrase these queries again; the "
        "host refuses a query it has already queued. STILL AVAILABLE: every "
        "other query, and extraction of already-discovered sources."
    )


def _degraded_feedback(
    degraded: list[tuple[str, SearchDegradation]],
    queued: Sequence[str],
    cooling: Mapping[str, float] | None,
) -> str:
    """What the model is told about a search outage it did not cause.

    Two shapes for the "what broke" sentence, because the host did two different
    things. A transient outage was retried below the turn loop and the message
    says so. A rate limit was NOT retried — an immediate re-issue only extends a
    block — so the message must not claim retries that never happened.
    """
    attempts = max(degradation.attempts for _query, degradation in degraded)
    named = "; ".join(f'"{query}" ({degradation.describe()})' for query, degradation in degraded)
    broke = (
        f"The host already ran each of them again automatically ({attempts} attempts "
        "with backoff) below your turn loop and the search provider stayed degraded."
        if attempts > 1
        else "The host issued each of them once; the provider refused rather than "
        "failed, so trying again immediately would only extend the refusal."
    )
    return _infrastructure_feedback("SEARCH INFRASTRUCTURE DEGRADED", named, broke, queued, cooling)


def _extraction_failure_feedback(
    queries: Sequence[str], queued: Sequence[str], cooling: Mapping[str, float] | None
) -> str:
    """The extraction outage's own message, shaped like the search one.

    Same rule, one layer down: these queries reached the world and their
    sources did not reach us, so their empty result says nothing about the
    query. A pivot instruction here would be a request to reword around a
    broken fetcher.
    """
    named = "; ".join(f'"{query}"' for query in queries)
    broke = (
        "The search provider answered and returned real sources; every attempt to "
        "read those sources failed, and the host already retried the transient ones "
        "below your turn loop."
    )
    return _infrastructure_feedback(
        "EXTRACTION INFRASTRUCTURE FAILING", named, broke, queued, cooling
    )


def _source_unavailable_feedback(queries: Sequence[str]) -> str:
    """Explain settled source-address failures without calling them an outage."""
    named = "; ".join(f'"{query}"' for query in queries)
    return (
        "SOURCE UNAVAILABLE — the discovered source addresses for "
        f"{named} were not found or paywalled. The host will not retry these addresses. "
        "This does not disprove the research "
        "angle. Search for the original source's correct address or an accessible "
        "primary version."
    )


def _queued_queries(queries: Sequence[str], queued: set[str]) -> list[str]:
    return [query for query in queries if normalize_query(query) in queued]


def _queries_with_outcome(yields: Sequence[tuple[str, str]], outcome: str) -> list[str]:
    return [query for query, reason in yields if reason == outcome]


def zero_admission_feedback(
    yield_reasons: Sequence[tuple[str, str]],
    degraded: list[tuple[str, SearchDegradation]],
    duplicate_hits: int = 0,
    *,
    queued: Sequence[str] = (),
    cooling: Mapping[str, float] | None = None,
) -> str:
    """Feedback for a turn that admitted no new evidence.

    ``yield_reasons`` pairs each empty query with how its turn ended, because an
    infrastructure class has to be able to NAME the queries whose retry the host
    has taken over. ``queued`` is what the loop's re-issue queue actually holds
    after this turn and ``cooling`` is the live cooldown registry, so the state
    sentence is built from the host's real state rather than from an intention.
    """
    reasons_seen = {reason for _query, reason in yield_reasons}
    tested = sorted(
        reasons_seen.difference(UNTESTED_YIELD_REASONS, {DISCOVERED, SOURCE_UNAVAILABLE})
    )
    unreadable = _queries_with_outcome(yield_reasons, EXTRACTION_FAILURE)
    unavailable = _queries_with_outcome(yield_reasons, SOURCE_UNAVAILABLE)
    queued_set = {normalize_query(query) for query in queued}
    parts: list[str] = []
    if degraded:
        parts.append(
            _degraded_feedback(
                degraded,
                _queued_queries([query for query, _d in degraded], queued_set),
                cooling,
            )
        )
    if unreadable:
        parts.append(
            _extraction_failure_feedback(
                unreadable,
                _queued_queries(unreadable, queued_set),
                cooling,
            )
        )
    if unavailable:
        parts.append(_source_unavailable_feedback(unavailable))
    if DISCOVERED in reasons_seen:
        parts.append(zero_yield_detail(DISCOVERED, None))
    if tested or not parts:
        reasons = ", ".join(tested) or "retrieval failures"
        parts.append(
            "The last research turn admitted no new evidence "
            f"({reasons}). Do not paraphrase those queries. Pivot by changing "
            "the angle, source/domain, or specificity, and trace named works upstream."
        )
    if duplicate_hits > 0:
        parts.append(
            f"{duplicate_hits} of the results retrieved were sources already in the "
            "evidence pool — the pool already covers this angle."
        )
    return " ".join(parts)


@dataclass(frozen=True)
class TestedQuery:
    """A query the host really put to the world, and how that turn ended."""

    query: str
    normalized: str
    tokens: frozenset[str]
    turn: int | None
    outcome: str


@dataclass(frozen=True)
class QueuedQueryRef:
    """A query the loop's re-issue queue already owns, and where it stands.

    The queue itself lives in ``_reissue_queue``; this is the flat view the
    freshness wall needs, so the wall stays a pure function of trail + plan and
    does not have to import the queue.
    """

    query: str
    normalized: str
    tokens: frozenset[str]
    detail: str
    #: True once the host's own retry budget for it is spent. Both states refuse
    #: the query, for different reasons, and the audit row must not call them the
    #: same thing: one is work still scheduled, the other is an angle unreached.
    spent: bool = False


@dataclass(frozen=True)
class ExhaustedAngle:
    """A subquestion the run PROVED is a dead end, flattened for the message.

    The verdict and the evidence behind it are computed in
    ``_subquestion_ledger``; this is the shape the wall needs, so the wall stays
    a pure function of its arguments and the two modules do not import each
    other. Every field is already visible to the model in that turn's ledger —
    a refusal here can never be the first the model hears of the state.

    A BANKED angle never reaches here: meeting the evidence floor is a fact
    about what can be written, not a ban on further queries. Only exhaustion —
    distinct queries reached the world and admitted nothing — refuses.
    """

    angle: str
    #: Why the host calls it exhausted, in its own words ("3 distinct queries
    #: reached the world for it and admitted nothing").
    why: str
    #: What the pool holds for it right now.
    evidence: str


@dataclass(frozen=True)
class QueryRefusal:
    """A planned query the host refused, and why.

    Three walls, three different refusals, never worded the same because they
    mean different things: the freshness wall matched it against a query
    already tested (``earlier``); the loop's re-issue queue already owns it
    (``queued``); or it is planned for a subquestion the run has proved to be a
    dead end (``exhausted``). Only the first means "the pool already holds
    this".
    """

    query: str
    earlier: TestedQuery | None
    queued: QueuedQueryRef | None = None
    exhausted: ExhaustedAngle | None = None


@dataclass(frozen=True)
class NarrowedQuery:
    """A planned query the freshness wall matched but ISSUED, because it scopes.

    Not a refusal and not a plain fresh query: the host needs to be able to
    prove, later, that a query which looks like a repeat was let through on
    purpose and on what evidence.
    """

    query: str
    #: The tested query it matched — the one it narrows.
    narrowed_from: str
    #: The scoping terms it added, in sorted order.
    added: tuple[str, ...]


def _search_outcome(entry: Mapping[str, Any]) -> str:
    admitted = entry.get("admitted")
    if isinstance(admitted, int) and admitted > 0:
        return f"admitted {admitted}"
    reason = entry.get("yield_reason")
    return reason if isinstance(reason, str) and reason else "admitted 0"


def _record(query: str, entry: Mapping[str, Any], outcome: str) -> TestedQuery:
    turn = entry.get("turn")
    return TestedQuery(
        query=query,
        normalized=normalize_query(query),
        tokens=query_tokens(query),
        turn=turn if isinstance(turn, int) else None,
        outcome=outcome,
    )


def tested_query_records(trail: list[dict[str, Any]]) -> list[TestedQuery]:
    """The tested queries the freshness wall measures against, in trail order.

    Same tested/untested split as :func:`semantically_tested_queries`, but it
    keeps the query as issued plus the turn and outcome, so a refusal can name
    what it collides with instead of saying "already attempted" and stopping.
    """
    tested: dict[str, TestedQuery] = {}
    untested: set[str] = set()
    rejected: dict[str, TestedQuery] = {}
    for entry in trail:
        kind = entry.get("kind")
        if kind not in {"search", QUERY_REJECTED_KIND} or not entry.get("query"):
            continue
        query = str(entry["query"])
        normalized = normalize_query(query)
        if kind == QUERY_REJECTED_KIND:
            # A refusal names why it happened. A query the EXHAUSTION wall
            # turned away was not "a repeat"; a later near-duplicate of it
            # must not be told it was. ("retired" is the pre-three-state name
            # for the same row, kept so a resumed run reads its own history.)
            outcome = (
                _EXHAUSTED_OUTCOME
                if entry.get("rejected") in {"exhausted", "retired"}
                else _REFUSED_OUTCOME
            )
            rejected.setdefault(normalized, _record(query, entry, outcome))
        elif entry.get("yield_reason") in UNTESTED_YIELD_REASONS or entry.get("result") == "failed":
            untested.add(normalized)
        else:
            tested.setdefault(normalized, _record(query, entry, _search_outcome(entry)))
    for normalized, record in rejected.items():
        if normalized not in tested and normalized not in untested:
            tested[normalized] = record
    return list(tested.values())


def _duplicates(
    records: Sequence[TestedQuery], normalized: str, tokens: frozenset[str]
) -> list[TestedQuery]:
    """EVERY tested query this planned query collides with, in trail order.

    All of them, not the first: a narrowing has to narrow everything it
    matched. Measured live, when this returned only the first match — an
    identical ``site:… filetype:pdf …`` query ran on two consecutive turns,
    because the first thing it matched in trail order was the earlier broad
    query it really does scope, and the verbatim repeat further down was never
    looked at.
    """
    return [
        record
        for record in records
        if record.normalized == normalized or queries_are_near_duplicates(tokens, record.tokens)
    ]


def _first_queued(
    queued: Sequence[QueuedQueryRef], normalized: str, tokens: frozenset[str]
) -> QueuedQueryRef | None:
    for record in queued:
        if record.normalized == normalized or queries_are_near_duplicates(tokens, record.tokens):
            return record
    return None


def partition_fresh_queries(
    trail: list[dict[str, Any]],
    queries: Sequence[str],
    queued: Sequence[QueuedQueryRef] = (),
) -> tuple[list[str], list[QueryRefusal], list[NarrowedQuery]]:
    """Split a turn's planned queries into issuable ones, refusals and narrowings.

    An exact repeat and a rewording are refused identically: re-running either
    retrieves what the pool already holds. A NARROWING of a tested query is
    issued — it carries a scope the tested query did not, so it reaches
    different sources — and is returned separately so the trail can say which
    query it narrowed. A query whose every issue was provider-degraded or
    transport-failed was never tested, so neither it nor a near-duplicate of it
    is blocked by the freshness wall — that rule is unchanged, and it is exactly
    why the ``queued`` check has to come first: an untested query is issuable in
    principle, and the only reason to refuse it is that the HOST already owns
    re-running it.
    """
    records = tested_query_records(trail)
    fresh: list[str] = []
    refusals: list[QueryRefusal] = []
    narrowings: list[NarrowedQuery] = []
    for query in queries:
        normalized = normalize_query(query)
        if not normalized:
            refusals.append(QueryRefusal(query=query, earlier=None))
            continue
        tokens = query_tokens(query)
        pending = _first_queued(queued, normalized, tokens)
        if pending is not None:
            refusals.append(QueryRefusal(query=query, earlier=None, queued=pending))
            continue
        matched = _duplicates(records, normalized, tokens)
        # Refuse on the first thing it does NOT narrow — an exact repeat adds
        # nothing to itself, so it lands here without a special case.
        repeated = next(
            (record for record in matched if not added_narrowing(query, record.query)), None
        )
        if repeated is not None:
            refusals.append(QueryRefusal(query=query, earlier=repeated))
            continue
        if matched:
            narrowings.append(
                NarrowedQuery(
                    query=query,
                    narrowed_from=matched[0].query,
                    added=tuple(sorted(added_narrowing(query, matched[0].query))),
                )
            )
        records.append(TestedQuery(query, normalized, tokens, None, _SAME_TURN_OUTCOME))
        fresh.append(query)
    return fresh, refusals, narrowings


def _refusal_line(refusal: QueryRefusal) -> str:
    if refusal.exhausted is not None:
        # Dead-end refusals get their own block (`_exhausted_feedback`); this is
        # the one-line form, so a caller that mixes the classes still says the
        # real reason rather than falling through to "empty".
        return (
            f'"{refusal.query}" is planned for the exhausted angle '
            f'"{refusal.exhausted.angle}" ({refusal.exhausted.why})'
        )
    if refusal.queued is not None:
        return (
            f'"{refusal.query}" is the host\'s work, not yours: '
            f'"{refusal.queued.query}" {refusal.queued.detail}'
        )
    earlier = refusal.earlier
    if earlier is None:
        return f'"{refusal.query}" is empty, so the host has nothing to issue'
    where = f"turn {earlier.turn}" if earlier.turn is not None else "this turn"
    return (
        f'"{refusal.query}" repeats "{earlier.query}", which ran on {where} '
        f"(outcome: {earlier.outcome})"
    )


def _exhausted_feedback(refusals: Sequence[QueryRefusal]) -> str:
    """The dead-end refusal: why, what the run tried, next, what stays allowed.

    Its own block rather than a line inside the repeat message, because the
    reason is different in kind. A repeat refusal says "you already ran this
    query"; this one says "distinct queries for this angle reached the world
    and admitted nothing". Everything it states is also in this turn's
    subquestion ledger, so the model can check the wall against the ledger it
    just read instead of taking the refusal on faith.

    It never says a banked angle is closed, because it is never raised for one.
    """
    named = "; ".join(
        f'"{refusal.query}" is planned for the exhausted angle '
        f'"{refusal.exhausted.angle}" ({refusal.exhausted.why}); the pool for it: '
        f"{refusal.exhausted.evidence}"
        for refusal in refusals
        if refusal.exhausted is not None
    )
    return (
        "QUERIES REFUSED — EXHAUSTED ANGLE. Distinct queries for these angles "
        "reached the world and admitted nothing, so another one is spent effort, "
        f"not a pivot: {named}. STATE: the subquestion ledger in this message "
        "lists every attempt and its outcome; nothing else about the run has "
        "changed. NEXT: treat the angle as unresolved and say so in the report's "
        "own terms, and spend this turn on a different angle — one the ledger "
        "marks OPEN or BANKED, or a new one you add to coverage.open. STILL "
        "AVAILABLE: every OPEN angle; every BANKED angle, which is NOT closed — "
        "cross-validation, the upstream chase to an original paper, filing or "
        "DOI, and primary-source replacements for secondary summaries are all "
        "issuable there; and any query the ledger attributes to no single angle."
    )


def refusal_feedback(refusals: Sequence[QueryRefusal]) -> str:
    """Name every refused query, why it was refused, and what to do instead."""
    if not refusals:
        return ""
    exhausted = [refusal for refusal in refusals if refusal.exhausted is not None]
    others = [refusal for refusal in refusals if refusal.exhausted is None]
    parts: list[str] = []
    if others:
        lines = "; ".join(_refusal_line(refusal) for refusal in others)
        parts.append(
            "QUERIES REFUSED — either already attempted (and a rewording is the same "
            "query to the host: it reaches the same sources and the evidence pool does "
            f"not move), or already queued for the host to run again by itself. {lines}. "
            "STATE: nothing else about the run changed; every other query you planned "
            "this turn was issued. NEXT: replace each refused query. STILL AVAILABLE: "
            f"{_NARROWING_VOCABULARY} {_PIVOT_VOCABULARY}"
        )
    if exhausted:
        parts.append(_exhausted_feedback(exhausted))
    return " ".join(parts)


def refusal_trail_rows(turn: int, refusals: Sequence[QueryRefusal]) -> list[dict[str, Any]]:
    """`query_rejected` audit rows — the existing shape, with the match named."""
    rows: list[dict[str, Any]] = []
    for refusal in refusals:
        earlier = refusal.earlier
        row: dict[str, Any] = {
            "kind": QUERY_REJECTED_KIND,
            "turn": turn,
            "query": refusal.query,
        }
        if refusal.exhausted is not None:
            row["rejected"] = "exhausted"
            row["exhausted_angle"] = refusal.exhausted.angle
            row["exhausted_why"] = refusal.exhausted.why
        elif refusal.queued is not None:
            row["rejected"] = "retry_budget_spent" if refusal.queued.spent else "queued"
            row["duplicates"] = refusal.queued.query
            row["duplicates_outcome"] = refusal.queued.detail
        elif earlier is None:
            row["rejected"] = "empty"
        else:
            exact = earlier.normalized == normalize_query(refusal.query)
            row["rejected"] = "repeat" if exact else "near_duplicate"
            row["duplicates"] = earlier.query
            row["duplicates_outcome"] = earlier.outcome
            if earlier.turn is not None:
                row["duplicates_turn"] = earlier.turn
        rows.append(row)
    return rows


def narrowing_trail_rows(turn: int, narrowings: Sequence[NarrowedQuery]) -> list[dict[str, Any]]:
    """`query_narrowed` audit rows — a repeat-shaped query the wall let through.

    Trail-only on purpose. The query is issued, so it already reaches the wire
    as an ordinary `search`; this row exists so an auditor reading the trail can
    tell a query the wall never matched from one it matched and admitted, and on
    what scope. No new progress action name is introduced.
    """
    return [
        {
            "kind": QUERY_NARROWED_KIND,
            "turn": turn,
            "query": narrowed.query,
            "narrowed_from": narrowed.narrowed_from,
            "narrowing": list(narrowed.added),
        }
        for narrowed in narrowings
    ]


def semantically_tested_queries(trail: list[dict[str, Any]]) -> set[str]:
    """Queries the model has actually put to the world.

    Three untested classes exist, and none may be blocked:

    * a query whose EVERY issue ended in `provider_degraded` — the search
      infrastructure was down, the retrieval layer already exhausted its own
      retries, and the query never reached the world.
    * a query whose EVERY issue ended in `extraction_failure` — the search
      provider answered and every discovered source was unreadable, so the
      pool did not move and the query was never semantically tested either.
    * a query whose EVERY issue ended in a retrieval-level EXCEPTION (a trail
      entry with ``result == "failed"``) — it blew up in transport, so it was
      not semantically tested either.

    All three are host faults, not the model's. Blocking re-issue would forbid
    the only correct response to an outage, so they stay available; genuinely
    tested queries stay blocked. A query tested once and broken once stays
    blocked, because the tested issue is a real result about it.
    """
    return {record.normalized for record in tested_query_records(trail)}
