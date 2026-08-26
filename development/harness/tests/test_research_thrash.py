from __future__ import annotations

from harness.research_harness import ObservationEvent, analyze_thrash


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
