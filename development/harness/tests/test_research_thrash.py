from __future__ import annotations

from disco.retrieval.deep_research._search_outcomes import partition_fresh_queries
from harness.research_harness import ObservationEvent, analyze_thrash

#: Three queries the researcher proposed on separate turns of the T3 pass-3
#: clean live run, copied verbatim from that run's `model_io.jsonl`
#: `declared_decision.queries`.  Its `search_io.jsonl` records that the host
#: ISSUED the first and the narrowed one (`origin: "model"`) and never issued
#: the reworded one: a rewording reaches the same sources, a `site:` scope does
#: not.
_ATB_ISSUED = (
    "NREL ATB 2024 utility-scale battery 4-hour overnight capital cost"
    " 2020 vs 2024 $ per kW dataset"
)
_ATB_REWORDED = (
    "NREL ATB 2024 utility-scale battery storage overnight capital cost"
    " $/kW 2020 vs 2024 4-hour trajectory dataset"
)
_ATB_NARROWED = (
    "site:atb.nrel.gov 2024 utility scale battery 4-hour overnight capital cost $ per kW 2020 trend"
)

#: Query pairs the detector and the product's freshness wall must classify the
#: same way. `True` means "the host would refuse the second as a repeat of the
#: first"; `False` means it is a real pivot and must run.
_IDENTITY_FIXTURE = [
    ("solid-state batteries EVs commercialization pathways",
     "solid state battery EV commercialization timeline", True),
    ("solid-state batteries EVs commercialization pathways",
     "commercialization pathways for solid-state batteries in EVs", True),
    ("EV charging infrastructure buildout rural US",
     "rural US EV charging infrastructure buildout", True),
    ("grid storage cost per kWh 2026", "  GRID  STORAGE COST PER KWH 2026 ", True),
    ("solid-state battery manufacturing cost breakdown",
     "solid-state battery safety failure modes", False),
    ("grid storage lithium iron phosphate cost per kWh",
     "grid storage lithium iron phosphate recycling regulations", False),
    ("sodium-ion battery energy density 2026",
     "sodium-ion battery supply chain China", False),
    ("solid-state battery cathode manufacturing",
     "solid-state battery anode manufacturing", False),
    # A NARROWING: same tokens, plus a site: scope the first query did not carry.
    # The wall issues it, so the detector must not call it a repeat.
    (_ATB_ISSUED, _ATB_NARROWED, False),
]  # fmt: skip


def _event(seq: int, *, phase: str = "search", **payload):
    return ObservationEvent(seq, seq, "frame", phase, payload)


def test_thrash_reports_repeated_queries_and_no_progress() -> None:
    events = [
        _event(1, query="same", rows=0),
        _event(2, query="same", rows=0),
        _event(3, query="same", rows=0),
    ]
    report = analyze_thrash(events)
    assert report["passed"] is False
    codes = {finding["code"] for finding in report["findings"]}
    assert "REPEATED_QUERY" in codes
    assert "NO_PROGRESS" in codes


def test_thrash_accepts_pivoted_queries_with_progress() -> None:
    events = [
        _event(1, query="primary question", rows=4),
        _event(2, query="independent source question", rows=3),
        _event(3, query="contradiction check", rows=2),
    ]
    report = analyze_thrash(events)
    assert report["passed"] is True
    assert report["signals"]["unique_query_count"] == 3


def test_zero_added_queries_in_one_round_count_as_one_turn() -> None:
    events = [
        _event(1, round=1, ok=True, query="angle one", added=0),
        _event(2, round=1, ok=True, query="angle two", added=0),
        _event(3, round=1, ok=True, query="angle three", added=0),
    ]
    report = analyze_thrash(events, max_no_progress_streak=1)
    assert report["signals"]["max_no_progress_streak"] == 1
    assert not any(finding["code"] == "NO_PROGRESS" for finding in report["findings"])


def test_consecutive_zero_yield_rounds_use_the_configured_threshold() -> None:
    events = [
        _event(1, round=1, ok=True, query="angle one", added=0),
        _event(2, round=1, ok=True, query="angle two", added=0),
        _event(3, round=2, ok=True, query="angle three", added=0),
    ]
    report = analyze_thrash(events, max_no_progress_streak=1)
    assert report["signals"]["max_no_progress_streak"] == 2
    assert any(finding["code"] == "NO_PROGRESS" for finding in report["findings"])


def test_retrieval_failure_round_is_visible_but_not_model_thrashing() -> None:
    events = [
        _event(1, round=1, ok=False, query="provider down"),
        _event(2, round=2, ok=False, query="provider still down"),
    ]
    report = analyze_thrash(events, max_no_progress_streak=0)
    assert report["signals"]["retrieval_failure_turns"] == 2
    assert report["signals"]["max_no_progress_streak"] == 0
    assert not any(finding["code"] == "NO_PROGRESS" for finding in report["findings"])


def test_thrash_identifies_malformed_and_repeated_deficiency_turns() -> None:
    events = [
        _event(1, phase="verification", malformed=True, deficiencies=["missing citation"]),
        _event(2, phase="verification", deficiencies=["missing citation"]),
    ]
    report = analyze_thrash(events)
    codes = {finding["code"] for finding in report["findings"]}
    assert "MALFORMED_TURN" in codes
    assert "REPEATED_DEFICIENCY" in codes


def test_report_review_parse_retry_is_not_research_loop_malformed_thrash() -> None:
    report = analyze_thrash(
        [],
        model_io=[
            {"stage": "report_review", "parse_error": "empty response"},
            {"stage": "research_turn", "parse_error": "invalid JSON"},
        ],
    )
    assert report["signals"]["malformed_turns"] == 1
    assert [finding["code"] for finding in report["findings"]] == ["MALFORMED_TURN"]


def test_stage_less_model_io_keeps_legacy_malformed_detection() -> None:
    report = analyze_thrash([], model_io=[{"parse_error": "invalid JSON"}])
    assert report["signals"]["malformed_turns"] == 1
    assert any(finding["code"] == "MALFORMED_TURN" for finding in report["findings"])


def test_repeated_query_detector_matches_the_product_freshness_wall() -> None:
    """Drift test: the harness and the host must mean the same thing by "same query".

    The detector imports the product's identity helpers, so this proves the
    import is live rather than assumed — and fails loudly if a copy ever drifts.
    """
    for first, second, same in _IDENTITY_FIXTURE:
        trail = [{"kind": "search", "turn": 0, "query": first, "admitted": 1, "result": "evidence"}]
        _fresh, refused, _narrowed = partition_fresh_queries(trail, (second,))
        product_refuses = bool(refused)

        report = analyze_thrash(
            [],
            model_io=[{"declared_decision": {"queries": [first, second]}}],
            max_repeated_query=1,
        )
        harness_flags = any(finding["code"] == "REPEATED_QUERY" for finding in report["findings"])

        assert product_refuses is same, (first, second)
        assert harness_flags is product_refuses, (first, second)


def test_paraphrased_queries_collapse_into_one_unique_query() -> None:
    """The looped paraphrases the live batch produced count as one query, not four."""
    events = [
        _event(1, query="solid-state batteries EVs commercialization pathways", rows=0),
        _event(2, query="solid state battery EV commercialization timeline", rows=0),
        _event(3, query="commercialization pathways for solid-state batteries in EVs", rows=0),
    ]
    report = analyze_thrash(events)
    assert report["signals"]["unique_query_count"] == 1
    assert report["signals"]["max_repeated_query"] == 3
    assert any(finding["code"] == "REPEATED_QUERY" for finding in report["findings"])


# ---- L19: the host's own retries are not the model repeating itself ---------


def test_repeated_query_ignores_the_loops_own_re_issues() -> None:
    """The product re-runs an untested query itself and stamps it `system`.

    Counting those would report the HOST's retry as model thrash — the exact
    misattribution this instrument exists to avoid.
    """
    events = [
        _event(1, query="same", rows=0, origin="model"),
        _event(2, query="same", rows=0, origin="system"),
        _event(3, query="same", rows=0, origin="system"),
        _event(4, query="same", rows=0, origin="system"),
    ]
    report = analyze_thrash(events)

    assert report["signals"]["query_count"] == 1
    assert not any(finding["code"] == "REPEATED_QUERY" for finding in report["findings"])


def test_the_model_repeating_itself_is_still_thrash_beside_system_re_issues() -> None:
    """The guard on the guard: `origin` must not become a way to hide thrash."""
    events = [_event(index, query="same", rows=0, origin="model") for index in range(1, 4)] + [
        _event(4, query="same", rows=0, origin="system")
    ]
    report = analyze_thrash(events)

    assert any(finding["code"] == "REPEATED_QUERY" for finding in report["findings"])
    assert report["signals"]["max_repeated_query"] == 3


def test_origin_is_read_through_the_wire_envelopes() -> None:
    """Live frames wrap the search in event -> tool_call -> arguments."""
    events = [
        _event(
            index,
            event={
                "kind": "action",
                "tool_call": {
                    "tool_name": "search",
                    "arguments": {"query": "same", "round": index, "origin": "system"},
                },
            },
        )
        for index in range(1, 5)
    ]
    report = analyze_thrash(events)

    assert report["signals"]["query_count"] == 0


def test_a_narrowing_the_host_issued_is_not_the_model_repeating_itself() -> None:
    """Real slice: three ATB proposals the detector reported as REPEATED_QUERY ×3.

    Only the middle one is a repeat — it rewords the first and the host refused
    it. The third adds a `site:` scope, so the wall issued it as a narrowing and
    the wire shows it running. Counting the narrowing made `thrash_clean` false
    on a run whose 48 proposals were all distinct.
    """
    report = analyze_thrash(
        [],
        model_io=[{"declared_decision": {"queries": [_ATB_ISSUED, _ATB_REWORDED, _ATB_NARROWED]}}],
    )

    assert report["signals"]["max_repeated_query"] == 2
    assert report["signals"]["unique_query_count"] == 2
    assert not any(finding["code"] == "REPEATED_QUERY" for finding in report["findings"])


def test_a_system_drains_empty_turn_is_not_the_models_no_progress() -> None:
    events = [
        _event(
            index,
            kind="search_turn_summary",
            origin="system",
            round=index,
            queries=2,
            failed_queries=0,
            new_admitted=0,
        )
        for index in range(1, 5)
    ]
    report = analyze_thrash(events)

    assert report["signals"]["max_no_progress_streak"] == 0


def test_discovery_progress_is_distinct_from_admission_and_repeated_leads():
    new_leads = [
        _event(
            i,
            kind="search_turn_summary",
            turn=i,
            queries=1,
            failed_queries=0,
            new_admitted=0,
            new_discovered=2,
        )
        for i in range(4)
    ]
    assert analyze_thrash(new_leads)["signals"]["max_no_progress_streak"] == 0
    repeated_leads = [
        _event(
            i,
            kind="search_turn_summary",
            turn=i,
            queries=1,
            failed_queries=0,
            new_admitted=0,
            new_discovered=0,
        )
        for i in range(4)
    ]
    assert analyze_thrash(repeated_leads)["signals"]["max_no_progress_streak"] == 4
    wire_leads = [_event(i, round=i, ok=True, added=0, new_discovered=2) for i in range(4)]
    assert analyze_thrash(wire_leads)["signals"]["max_no_progress_streak"] == 0
