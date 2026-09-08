"""Lane L22/T1 — the per-subquestion ledger and its one system-gated wall.

Grounded in the smoke run of 2026-09-02 (quick tier, US office CRE,
`~/AI-Work/disco-research-v2-2026-09-01/smoke-01-wave1/`). Its measured defect:
the model proposed "Trepp CMBS office delinquency rate …" on turns 1, 3 and 5;
turn 1 issued it and banked two passages, the freshness wall correctly refused
the other two, and the harness recorded three `REPEATED_QUERY` findings. The
model kept re-proposing because the subquestion "loan delinquencies" stayed
open in its own coverage list while the pool already held evidence for it.

Every angle string and every query string below is verbatim from that run's
`model_io.jsonl`, and the admissions and domains are verbatim from its
`search_io.jsonl` (the Trepp query admitted 2 passages, both from
`www.trepp.com`).
"""

from __future__ import annotations

from typing import Any

from disco.retrieval.deep_research._agent_state import _AgentState
from disco.retrieval.deep_research._budget import SourceBudget
from disco.retrieval.deep_research._search_outcomes import query_tokens, refusal_feedback
from disco.retrieval.deep_research._subquestion_ledger import (
    BANKED_MIN_DOMAINS,
    MAX_TESTED_ZERO_YIELDS,
    MIN_QUERY_ATTRIBUTION,
    all_exhausted,
    attribute,
    banked_domain_floor,
    build_ledger,
    exhausted_for_query,
    ledger_trail_row,
    render_ledger,
)
from disco.retrieval.deep_research.agent import _fresh_queries
from disco.retrieval.deep_research.depth import DepthTier, bounds_for

# ---- verbatim from smoke-01-wave1/model_io.jsonl, turn 0 --------------------

_VACANCY = "national vacancy 2020-2026 trend and sources (CBRE/JLL/Cushman/Colliers)"
_VALUATIONS = (
    "valuations / price indices / cap rates / transaction volume "
    "(NCREIF/Green Street/ MSCI Real Capital)"
)
_DELINQUENCIES = (
    "loan delinquencies & defaults - CMBS and bank office loans, maturity wall "
    "(Trepp/MBA/Fed)"
)
_CONVERSIONS = (
    "office-to-residential conversions - actual units vs predicted potential "
    "(CBRE/RentCafe/Yardi/ULI)"
)
_BANKS = (
    "regional/small bank exposure - CRE share of loans, office share, "
    "failures/stress (Fed/FDIC/BIS/FSOC)"
)
_OPEN_ANGLES = [_VACANCY, _VALUATIONS, _DELINQUENCIES, _CONVERSIONS, _BANKS]

_VACANCY_QUERY = "US office vacancy rate national 2020-2026 CBRE JLL historical report"
_TREPP_QUERY = "Trepp CMBS office delinquency rate 2024 2025 2026 MBA delinquency"
# The re-proposal the harness flagged, verbatim from turn 2 of the same run.
_TREPP_REPROPOSAL = (
    "Trepp CMBS office delinquency rate 2024 2025 2025 MBA mortgage delinquency office"
)
_MBA_QUERY = (
    "MBA Mortgage Bankers Association CREF delinquency report office CMBS bank "
    "life company 2024 2025 rate"
)


def _coverage(
    open_angles: list[str], covered: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {
        "covered": covered or [],
        "open": open_angles,
        "contradictions_checked": [],
    }


def _search(
    query: str,
    *,
    turn: int = 0,
    admitted: int = 0,
    domains: tuple[str, ...] = (),
    yield_reason: str | None = None,
) -> dict[str, Any]:
    """One `search` trail row in the shape `_search_turn._record_result` writes."""
    row: dict[str, Any] = {
        "kind": "search",
        "turn": turn,
        "query": query,
        "origin": "model",
        "admitted": admitted,
        "final_admitted_count": admitted,
        "result": "evidence" if admitted else "empty",
    }
    if domains:
        row["admitted_domains"] = list(domains)
    if yield_reason:
        row["yield_reason"] = yield_reason
    return row


# ---- attribution ------------------------------------------------------------


def test_each_smoke_run_query_attributes_to_the_angle_it_really_served() -> None:
    """The measured band: real attributions scored 0.27-0.64 containment."""
    tokens = [(index, query_tokens(angle)) for index, angle in enumerate(_OPEN_ANGLES)]
    assert attribute(_TREPP_QUERY, tokens) == _OPEN_ANGLES.index(_DELINQUENCIES)
    assert attribute(_VACANCY_QUERY, tokens) == _OPEN_ANGLES.index(_VACANCY)
    assert (
        attribute(
            "Green Street CPPI NCREIF office property price decline 2022 2023 2024 2025",
            tokens,
        )
        == _OPEN_ANGLES.index(_VALUATIONS)
    )


def test_a_query_that_serves_no_single_angle_is_attributed_to_none() -> None:
    """The wall is never allowed to guess: an unattributed query stays issuable.

    Below the floor, and a tie above it, both return None — a query that spans
    two angles is the model's to run.
    """
    tokens = [(index, query_tokens(angle)) for index, angle in enumerate(_OPEN_ANGLES)]
    assert attribute("what did people think about all of this", tokens) is None
    assert attribute("", tokens) is None
    # Two identical angles cannot break the tie between them.
    same = [(0, query_tokens(_DELINQUENCIES)), (1, query_tokens(_DELINQUENCIES))]
    assert attribute(_TREPP_QUERY, same) is None


def test_the_attribution_floor_sits_under_the_measured_band() -> None:
    """Pin the number the smoke run justifies so drift is visible."""
    tokens = query_tokens(_TREPP_QUERY)
    angle = query_tokens(_DELINQUENCIES)
    assert len(tokens & angle) / len(tokens) >= MIN_QUERY_ATTRIBUTION
    assert MIN_QUERY_ATTRIBUTION < 0.27  # the weakest real attribution measured


# ---- the ledger -------------------------------------------------------------


def test_the_ledger_shows_what_the_pool_holds_for_the_angle_the_model_kept_open() -> None:
    """The smoke defect, rendered: the model can now SEE the Trepp admissions.

    Two passages from one domain, so the angle stays open — one domain is one
    source — but the model is no longer left to remember that turn 1 fed it.
    """
    trail = [
        _search(_VACANCY_QUERY, turn=0, admitted=3, domains=("cbre.com", "jll.com")),
        _search(_TREPP_QUERY, turn=0, admitted=2, domains=("www.trepp.com",)),
    ]
    entries, unattributed = build_ledger(_coverage(_OPEN_ANGLES), trail, frozenset())
    delinquencies = next(e for e in entries if e.angle == _DELINQUENCIES)

    assert delinquencies.admitted == 2
    assert delinquencies.domains == ("www.trepp.com",)
    assert delinquencies.state == "open"
    assert [row.query for row in delinquencies.queries] == [_TREPP_QUERY]
    assert delinquencies.queries[0].outcome == "admitted 2"
    assert unattributed == 0

    rendered = render_ledger(entries)
    assert _TREPP_QUERY in rendered
    assert "2 passages from 1 domain" in rendered
    assert "[OPEN]" in rendered


def test_an_empty_ledger_is_not_rendered_at_all() -> None:
    """Presence implies affordance: a page of zeroes on turn 0 is a tax."""
    entries, _unattributed = build_ledger(_coverage(_OPEN_ANGLES), [], frozenset())
    assert len(entries) == len(_OPEN_ANGLES)
    assert render_ledger(entries) == ""


def test_the_ledger_row_records_the_hosts_verdict_in_the_trail() -> None:
    """The verdict has to survive into the audit, not only into a prompt."""
    trail = [_search(_TREPP_QUERY, turn=0, admitted=2, domains=("www.trepp.com",))]
    entries, unattributed = build_ledger(_coverage(_OPEN_ANGLES), trail, frozenset())
    row = ledger_trail_row(3, entries, unattributed)

    assert row["kind"] == "subquestion_ledger"
    assert row["turn"] == 3
    delinquencies = next(
        item for item in row["subquestions"] if item["angle"] == _DELINQUENCIES
    )
    assert delinquencies == {
        "angle": _DELINQUENCIES,
        "declared": "open",
        "state": "open",
        "why": "",
        "queries": 1,
        "admitted": 2,
        "domains": ["www.trepp.com"],
    }


def test_untested_queries_are_listed_but_never_counted_as_attempts() -> None:
    """An extraction outage says nothing about the angle.

    Verbatim from the smoke run: the MBA CREF query was issued twice and every
    discovered page failed to extract. It appears in the ledger — the model has
    to see that it ran — and it does not move the angle toward exhaustion.
    """
    trail = [
        _search(_MBA_QUERY, turn=5, yield_reason="extraction_failure"),
        _search(_MBA_QUERY, turn=6, yield_reason="extraction_failure"),
    ]
    entries, _ = build_ledger(_coverage(_OPEN_ANGLES), trail, frozenset())
    delinquencies = next(e for e in entries if e.angle == _DELINQUENCIES)

    assert [row.query for row in delinquencies.queries] == [_MBA_QUERY]
    assert delinquencies.queries[0].untested is True
    assert delinquencies.queries[0].outcome == "extraction_failure"
    assert delinquencies.state == "open"


# ---- the three states -------------------------------------------------------


def _banked_trail() -> list[dict[str, Any]]:
    """Two admitted passages from two independent domains for one angle."""
    return [
        _search(_TREPP_QUERY, turn=0, admitted=2, domains=("www.trepp.com",)),
        _search(
            "CMBS office loan delinquency rate maturity wall Federal Reserve bank data",
            turn=2,
            admitted=3,
            domains=("federalreserve.gov",),
        ),
    ]


def _exhausted_trail() -> list[dict[str, Any]]:
    """Three distinct queries reached the world for one angle and admitted nothing."""
    return [
        _search(f"CMBS office loan delinquency {word} rate report", turn=index)
        for index, word in enumerate(
            ("special servicing", "maturity wall", "bank portfolio")
        )
    ]


def test_two_passages_from_two_domains_bank_the_angle_without_closing_it() -> None:
    entries, _ = build_ledger(_coverage(_OPEN_ANGLES), _banked_trail(), frozenset())
    delinquencies = next(e for e in entries if e.angle == _DELINQUENCIES)

    assert delinquencies.state == "banked"
    assert delinquencies.exhausted is False
    assert delinquencies.why == "the pool holds 5 passages from 2 independent domains"
    assert "[BANKED" in render_ledger(entries)


def test_one_domain_however_many_pages_never_banks_an_angle() -> None:
    """The run's own rule: multiple pages from one domain are one source."""
    trail = [_search(_TREPP_QUERY, turn=0, admitted=9, domains=("www.trepp.com",))]
    entries, _ = build_ledger(_coverage(_OPEN_ANGLES), trail, frozenset())
    assert next(e for e in entries if e.angle == _DELINQUENCIES).state == "open"


def test_a_covered_declaration_banks_only_when_its_ids_are_really_in_the_pool() -> None:
    covered = [{"angle": _DELINQUENCIES, "evidence_ids": ["p1", "p2"]}]
    coverage = _coverage([_VACANCY], covered)

    unverified, _ = build_ledger(coverage, [], frozenset({"p1"}))
    assert unverified[0].state == "open"

    verified, _ = build_ledger(coverage, [], frozenset({"p1", "p2"}))
    assert verified[0].state == "banked"
    assert "all 2 cited ids are in the pool" in verified[0].why


def test_three_tested_zero_yields_exhaust_an_angle() -> None:
    """The host closes a dead end instead of asking the model to notice one."""
    trail = _exhausted_trail()
    assert len(trail) == MAX_TESTED_ZERO_YIELDS
    entries, _ = build_ledger(_coverage(_OPEN_ANGLES), trail, frozenset())
    delinquencies = next(e for e in entries if e.angle == _DELINQUENCIES)

    assert delinquencies.state == "exhausted"
    assert delinquencies.exhausted is True
    assert "reached the world for it and admitted nothing" in delinquencies.why
    assert "[EXHAUSTED" in render_ledger(entries)


def test_only_exhausted_refuses_a_query() -> None:
    """The whole T1 fix in one assertion, over all three states.

    Batch B (13 hosted runs, standard depth) lost 104 of 490 proposed queries;
    a replay of the old walls attributed 88 refusals to the BANKED floor being
    read as a ceiling — DOI verifications, NREL/Lazard/EIA primary sources, the
    upstream chases the research prompt orders. Only a proven dead end refuses.
    """
    open_entries, _ = build_ledger(_coverage(_OPEN_ANGLES), [], frozenset())
    banked_entries, _ = build_ledger(
        _coverage(_OPEN_ANGLES), _banked_trail(), frozenset()
    )
    exhausted_entries, _ = build_ledger(
        _coverage(_OPEN_ANGLES), _exhausted_trail(), frozenset()
    )
    upstream_chase = (
        "Trepp CMBS office loan delinquency special servicing original 2025 "
        "methodology PDF"
    )

    assert exhausted_for_query(upstream_chase, open_entries) is None
    assert exhausted_for_query(upstream_chase, banked_entries) is None
    refused = exhausted_for_query(upstream_chase, exhausted_entries)
    assert refused is not None and refused.angle == _DELINQUENCIES


def test_a_primary_source_pivot_into_a_banked_angle_is_issued() -> None:
    """The refused-pivot class from batch B, at the wall that used to refuse it.

    Verbatim shape of run-13 t9 (a DOI verification for an angle holding ten
    passages from ten domains) and run-02 t10-t12 (the NREL/Lazard/EIA primary
    sources for cost numbers already covered by secondary reporting).
    """
    coverage = _coverage(_OPEN_ANGLES)
    state = _state_with(_banked_trail(), coverage)
    entries, _ = build_ledger(coverage, state.trail, frozenset())
    assert (
        next(e for e in entries if e.angle == _DELINQUENCIES).state == "banked"
    )
    pivot = (
        "Trepp CMBS office delinquency maturity wall 2025 special report DOI "
        "original methodology"
    )

    fresh, refused, _narrowed = _fresh_queries(state, (pivot,), entries)

    assert fresh == [pivot]
    assert refused == []


# ---- the wall ---------------------------------------------------------------


def _state_with(trail: list[dict[str, Any]], coverage: dict[str, Any]) -> _AgentState:
    state = _AgentState(budget=SourceBudget(30))
    state.trail.extend(trail)
    state.coverage = coverage
    return state


def test_a_query_for_an_exhausted_angle_is_refused_with_all_four_parts() -> None:
    coverage = _coverage(_OPEN_ANGLES)
    state = _state_with(_exhausted_trail(), coverage)
    entries, _ = build_ledger(coverage, state.trail, frozenset())
    another_attempt = "CMBS office loan delinquency servicer watchlist rate report 2025"

    fresh, refused, _narrowed = _fresh_queries(state, (another_attempt,), entries)

    assert fresh == []
    assert len(refused) == 1
    assert refused[0].exhausted is not None
    assert refused[0].exhausted.angle == _DELINQUENCIES

    message = refusal_feedback(refused)
    # why / state / next / allowed, each present and each specific.
    assert "EXHAUSTED ANGLE" in message
    assert _DELINQUENCIES in message
    assert "reached the world and admitted nothing" in message
    assert "STATE:" in message and "every attempt and its outcome" in message
    assert "NEXT:" in message and "marks OPEN or BANKED" in message
    # What stays allowed has to name the banked case explicitly: the model's
    # own decision text in batch B shows it inferring bans that were never made.
    assert "STILL AVAILABLE:" in message
    assert "every BANKED angle, which is NOT closed" in message
    assert "cross-validation" in message and "upstream chase" in message


def test_the_l22_smoke_reproposal_is_still_refused_but_by_the_freshness_wall() -> None:
    """The original defect stays fixed, by the wall that owns it.

    The smoke run re-proposed the Trepp query on turns 3 and 5 after turn 1 had
    banked two passages. It is still refused — it really is a near-duplicate of
    a query the host ran — but as a repeat, never as "this angle is closed".
    """
    coverage = _coverage(_OPEN_ANGLES)
    state = _state_with(_banked_trail(), coverage)
    entries, _ = build_ledger(coverage, state.trail, frozenset())

    fresh, refused, _narrowed = _fresh_queries(state, (_TREPP_REPROPOSAL,), entries)

    assert fresh == []
    assert [refusal.exhausted for refusal in refused] == [None]
    assert refused[0].earlier is not None
    assert refused[0].earlier.query == _TREPP_QUERY


def test_a_query_for_an_open_angle_is_never_touched_by_the_exhaustion_wall() -> None:
    coverage = _coverage(_OPEN_ANGLES)
    state = _state_with(_banked_trail(), coverage)
    entries, _ = build_ledger(coverage, state.trail, frozenset())
    pivot = "office to residential conversion completions 2025 RentCafe adaptive reuse units"

    fresh, refused, _narrowed = _fresh_queries(state, (pivot,), entries)

    assert fresh == [pivot]
    assert refused == []


def test_the_banked_floor_scales_with_the_tiers_own_evidence_floor() -> None:
    """An exhaustive survey is not closed by what closes a quick check.

    Derived from the tier bounds themselves (distinct works ÷ major angles),
    never from a fourth invented number.
    """
    quick, standard, exhaustive = (bounds_for(tier) for tier in DepthTier)
    assert banked_domain_floor(quick.min_evidence_sources, quick.min_evidence_themes) == 2
    assert (
        banked_domain_floor(standard.min_evidence_sources, standard.min_evidence_themes)
        == 3
    )
    assert (
        banked_domain_floor(
            exhaustive.min_evidence_sources, exhaustive.min_evidence_themes
        )
        == 3
    )
    # Never below the floor, whatever a tier declares.
    assert banked_domain_floor(0, 0) == BANKED_MIN_DOMAINS

    # Two domains bank the angle at quick depth and leave it open at standard.
    entries, _ = build_ledger(
        _coverage(_OPEN_ANGLES), _banked_trail(), frozenset(), min_domains=3
    )
    assert next(e for e in entries if e.angle == _DELINQUENCIES).state == "open"


def test_the_wall_stands_down_when_every_named_angle_is_exhausted() -> None:
    """A wall that blocks the whole board is a maze, not a funnel."""
    coverage = _coverage([_DELINQUENCIES])
    state = _state_with(_exhausted_trail(), coverage)
    entries, _ = build_ledger(coverage, state.trail, frozenset())
    another_attempt = "CMBS office loan delinquency servicer watchlist rate report 2025"

    assert all_exhausted(entries)
    assert exhausted_for_query(another_attempt, entries) is None
    _fresh, refused, _narrowed = _fresh_queries(state, (another_attempt,), entries)
    assert [refusal.exhausted for refusal in refused] == []
    assert "Every angle you have named is exhausted" in render_ledger(entries)


def test_an_unattributable_query_stays_issuable_even_beside_an_exhausted_angle() -> None:
    """Do not build a wall the model cannot see through."""
    coverage = _coverage(_OPEN_ANGLES)
    state = _state_with(_exhausted_trail(), coverage)
    entries, _ = build_ledger(coverage, state.trail, frozenset())
    broad = "what economists actually predicted and what happened instead"

    assert exhausted_for_query(broad, entries) is None
    fresh, refused, _narrowed = _fresh_queries(state, (broad,), entries)
    assert fresh == [broad]
    assert refused == []


def test_the_exhausted_refusal_names_the_angle_in_its_audit_row() -> None:
    from disco.retrieval.deep_research._search_outcomes import refusal_trail_rows

    coverage = _coverage(_OPEN_ANGLES)
    state = _state_with(_exhausted_trail(), coverage)
    entries, _ = build_ledger(coverage, state.trail, frozenset())
    another_attempt = "CMBS office loan delinquency servicer watchlist rate report 2025"
    _fresh, refused, _narrowed = _fresh_queries(state, (another_attempt,), entries)

    row = refusal_trail_rows(4, refused)[0]
    assert row["rejected"] == "exhausted"
    assert row["exhausted_angle"] == _DELINQUENCIES
    assert "admitted nothing" in row["exhausted_why"]


def test_the_ledger_header_says_a_banked_angle_is_still_open_for_work() -> None:
    """The words are the wall. Batch B's model read BANKED as a ban and started
    writing queries shaped to slip past attribution instead of researching."""
    entries, _ = build_ledger(_coverage(_OPEN_ANGLES), _banked_trail(), frozenset())
    rendered = render_ledger(entries)

    assert "[BANKED] — the evidence floor for this angle is met" in rendered
    assert "It is NOT closed" in rendered
    assert "Further queries for a banked angle are allowed and expected" in rendered
    assert "original paper, filing or DOI" in rendered
    assert "only state that refuses a query" in rendered
    # The unattributed count is an audit fact, not a prompt line: while it was
    # rendered, the model treated "counted for none of them" as a defect.
    assert "counted for none" not in rendered


def test_the_unattributed_count_stays_in_the_audit_row() -> None:
    coverage = _coverage(_OPEN_ANGLES)
    trail = [_search("what happened to everything, generally speaking", turn=1, admitted=1)]
    entries, unattributed = build_ledger(coverage, trail, frozenset())

    assert unattributed == 1
    assert ledger_trail_row(2, entries, unattributed)["unattributed_queries"] == 1


# ---- the refused row carries its own angle ---------------------------------
#
# Verbatim from the post-lane exhaustive batch, run-03 (2026-09-02,
# `~/AI-Work/disco-research-v2-2026-09-01/phase6/disco-exhaustive-postlane/`):
# the one `thrash_clean` flag in 8/8. Turn 26 issued the WoodMac query and
# admitted 5; turn 29 re-proposed a reword, refused; turn 30's feedback carried
# the narrowing rule and the model pivoted on it; turn 32 (wrap-up, 1 turn left)
# re-proposed the query bare, reading a ledger row that said only "refused as a
# repeat". Zero searches were wasted — the wall held — but the wall said nothing
# about what would get through, and the rule that would have was three turns
# behind it.
#
# The angle below is the model's turn-26 wording. Against it the reword's own
# words score 0.20 (< MIN_QUERY_ATTRIBUTION) — the second failure mode: a
# refusal that drifts away from, or off, the ledger. It is attributed through
# the query it repeats instead, so it renders beside the issue it repeats.

_LCOS_ANGLE = (
    "BNEF Feb 2026 4-hr LCOS record-low independent cross-validation via Ember/WoodMac/Lazard"
)
_WOODMAC_QUERY = (
    "Wood Mackenzie Levelized Cost of Storage 2025 four-hour battery LCOS benchmark "
    "independent vs BNEF record low"
)
_WOODMAC_REPROPOSAL = (
    "Wood Mackenzie Levelized Cost of Storage LCOS 2025 battery storage report "
    "four-hour benchmark independent vs BNEF"
)
_WOODMAC_DOMAINS = (
    "ember-energy.org",
    "www.pv-magazine-australia.com",
    "www.saurenergy.com",
    "energytech-news.com",
    "howtostoreelectricity.com",
)


def _rejected(query: str, *, turn: int, duplicates: str, duplicates_turn: int) -> dict[str, Any]:
    """One `query_rejected` trail row in the shape `refusal_trail_rows` writes."""
    return {
        "kind": "query_rejected",
        "turn": turn,
        "query": query,
        "rejected": "near_duplicate",
        "duplicates": duplicates,
        "duplicates_outcome": "admitted 5",
        "duplicates_turn": duplicates_turn,
    }


def test_a_refused_row_says_what_would_issue_not_only_that_it_was_refused() -> None:
    """Rule 3 at the row: the wall the model reads on every later turn angles."""
    trail = [
        _search(_WOODMAC_QUERY, turn=26, admitted=5, domains=_WOODMAC_DOMAINS),
        _rejected(_WOODMAC_REPROPOSAL, turn=29, duplicates=_WOODMAC_QUERY, duplicates_turn=26),
    ]
    entries, _ = build_ledger(_coverage([_LCOS_ANGLE]), trail, frozenset())
    lcos = next(e for e in entries if e.angle == _LCOS_ANGLE)

    # Both rows survive: the issue is what happened, the refusal is its own row.
    assert [row.turn for row in lcos.queries] == [26, 29]
    assert lcos.queries[0].outcome == "admitted 5"
    refused = lcos.queries[1]
    assert refused.refused

    rendered = render_ledger(entries)
    line = next(row for row in rendered.splitlines() if row.lstrip().startswith('t29 "'))
    assert "refused as a repeat" in line
    # What gets through, in the wall's own terms — on the row, not three turns back.
    assert "a narrowing issues" in line
    assert "site:/filetype:" in line and "DOI" in line and "year" in line
    # And the bare re-proposal from turn 32 is still a repeat of both rows.
    for earlier in (_WOODMAC_QUERY, _WOODMAC_REPROPOSAL):
        assert query_tokens(earlier) & query_tokens(
            "Wood Mackenzie energy storage Levelized Cost of Storage 2025 four-hour "
            "battery benchmark independent vs BNEF"
        )
