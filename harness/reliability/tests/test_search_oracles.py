from __future__ import annotations

from copy import deepcopy

from harness.reliability.search_oracles import (
    FAIL,
    PASS,
    validate_grounded_answer,
    validate_report_event,
)


def _passage(passage_id: str = "p1") -> dict:
    return {
        "id": passage_id,
        "source_url": "https://example.com/source",
        "source_title": "Primary source",
        "text": "A sufficiently detailed source passage that supports the factual statement.",
    }


def _connectivity() -> list[dict]:
    return [{"url": "https://example.com/source", "connected": True, "status": 200}]


def _answer() -> dict:
    return {
        "query": "What happened?",
        "blocks": [
            {
                "kind": "prose",
                "id": "b1",
                "text": "The documented event happened in 2025. [[p1]]",
                "cited_passage_ids": ["p1"],
            }
        ],
        "claims": [
            {
                "claim": {"text": "The event happened in 2025", "cited_passage_ids": ["p1"]},
                "verdict": "supported",
                "best_passage_id": "p1",
                "entailment_score": 0.93,
            }
        ],
        "passages": [_passage()],
        "all_hits": [{"url": "https://example.com/source", "title": "Source"}],
        "unsupported_count": 0,
        "follow_ups": ["What changed afterward?", "Who confirmed the result?"],
    }


def _report() -> dict:
    return {
        "query": "Investigate the event",
        "summary": (
            "This executive summary is long enough to communicate the principal "
            "research result."
        ),
        "sections": [
            {
                "id": "s1",
                "title": "Evidence",
                "markdown": (
                    "The evidence establishes a documented result with useful context. [[p1]]"
                ),
                "cited_passage_ids": ["p1"],
                "confidence": "high",
                "disputed_notes": [],
                "unsupported_count": 0,
            }
        ],
        "passages": [_passage()],
        "all_hits": [{"url": "https://example.com/source", "title": "Source"}],
        "unsupported_count": 0,
        "bounded_by": None,
        "depth_tier": "quick",
    }


def _codes(result: dict) -> set[str]:
    return {item["code"] for item in result["findings"]}


def test_grounded_answer_passes_when_every_claim_resolves() -> None:
    result = validate_grounded_answer(_answer(), connectivity=_connectivity())
    assert result["status"] == PASS


def test_grounded_answer_fails_dangling_citation_and_count_lie() -> None:
    answer = _answer()
    answer["claims"][0]["claim"]["cited_passage_ids"] = ["missing"]
    answer["claims"][0]["best_passage_id"] = "missing"
    answer["claims"][0]["verdict"] = "unsupported"
    result = validate_grounded_answer(answer)
    assert result["status"] == FAIL
    assert {"UNRESOLVED_CITATION", "UNSUPPORTED_COUNT_MISMATCH"} <= _codes(result)


def test_grounded_answer_fails_empty_followups_and_unreachable_source() -> None:
    answer = _answer()
    answer["follow_ups"] = []
    connectivity = [{"url": "https://example.com/source", "connected": False, "status": None}]
    result = validate_grounded_answer(answer, connectivity=connectivity)
    assert result["status"] == FAIL
    assert {"MISSING_FOLLOW_UPS", "CITED_SOURCE_UNREACHABLE"} <= _codes(result)


def test_grounded_answer_does_not_probe_uncited_candidates() -> None:
    answer = _answer()
    answer["passages"].append(
        {
            **_passage("unused"),
            "source_url": "https://dead.example/unused",
        }
    )
    connectivity = [
        *_connectivity(),
        {
            "url": "https://dead.example/unused",
            "connected": True,
            "status": 404,
        },
    ]

    result = validate_grounded_answer(answer, connectivity=connectivity)

    assert result["status"] == PASS


def test_report_passes_when_sections_are_grounded() -> None:
    result = validate_report_event(_report(), connectivity=_connectivity())
    assert result["status"] == PASS


def test_report_fails_zero_passage_section() -> None:
    report = deepcopy(_report())
    report["sections"][0]["markdown"] = (
        "A long but completely uncited section that cannot support its factual contents."
    )
    report["sections"][0]["cited_passage_ids"] = []
    result = validate_report_event(report)
    assert result["status"] == FAIL
    assert {"UNCITED_SECTION", "NO_SECTION_MARKERS"} <= _codes(result)


def test_report_fails_unrendered_citation_and_bad_rollup() -> None:
    report = deepcopy(_report())
    report["passages"].append(_passage("p2"))
    report["sections"][0]["cited_passage_ids"].append("p2")
    report["sections"][0]["unsupported_count"] = 1
    result = validate_report_event(report)
    assert result["status"] == FAIL
    assert {"UNRENDERED_SECTION_CITATION", "UNSUPPORTED_COUNT_MISMATCH"} <= _codes(result)
