"""Focused tests for the redacted search I/O diagnostic projection."""

from __future__ import annotations

import asyncio
import json

from harness.research_harness import (
    FakeTransport,
    ObservationEvent,
    ResearchRequest,
    collect_search_io,
    run_harness,
)


def test_action_only_old_stream_keeps_query_but_does_not_infer_empty_response() -> None:
    rows = collect_search_io(
        [
            ObservationEvent(
                seq=7,
                elapsed_ms=12,
                kind="replay",
                phase="search",
                payload={
                    "type": "event",
                    "event": {
                        "kind": "action",
                        "tool_call": {
                            "tool_name": "search",
                            "arguments": {
                                "subquestion": "named work",
                                "query": "named work exact title",
                                "round": 2,
                            },
                        },
                    },
                },
            )
        ]
    )
    assert len(rows) == 1
    assert rows[0]["planned_query"] == "named work exact title"
    assert rows[0]["issued_query"] is None
    assert rows[0]["raw_hit_count"] is None
    assert rows[0]["provider_outcome"] == "not_observed"


def test_structured_response_is_bounded_and_replaces_action_row() -> None:
    events = [
        {
            "seq": 1,
            "elapsed_ms": 2,
            "kind": "live",
            "payload": {
                "event": {
                    "kind": "action",
                    "tool_call": {
                        "tool_name": "search",
                        "arguments": {"query": "planned", "round": 1, "turn": 0},
                    },
                }
            },
        },
        {
            "seq": 2,
            "elapsed_ms": 30,
            "kind": "live",
            "payload": {
                "kind": "search_io",
                "planned_query": "planned",
                "issued_query": "rewritten keywords",
                "round": 1,
                "turn": 0,
                "provider": "searxng",
                "provider_outcome": "ok",
                "raw_hits": [
                    {
                        "title": "A useful result",
                        "url": "https://example.test/a?api_key=secret&ok=1",
                        "source_engine": "searxng",
                        "status": "ok",
                        "snippet": {"text": "provider-only text"},
                    }
                ],
                "extracted": [
                    {"url": "https://example.test/a", "status": "ok", "passages": [1, 2]},
                    {"url": "https://example.test/b", "status": "blocked", "error": "nope"},
                ],
                "passages": [1, 2, 3],
                "admission_count": 2,
                "yield_reason": "new_evidence",
                "latency_ms": 28,
            },
        },
    ]
    rows = collect_search_io(events)
    assert len(rows) == 1
    row = rows[0]
    assert row["issued_query"] == "rewritten keywords"
    assert row["raw_hit_count"] == 1
    assert row["raw_hits"][0]["url"] == "https://example.test/a?ok=1"
    assert row["raw_hits"][0]["status"] == "ok"
    assert row["raw_hits"][0]["snippet"] == {
        "text": "provider-only text",
        "untrusted": True,
    }
    assert row["extraction_statuses"] == {"ok": 1, "blocked": 1}
    assert row["passage_count"] == 3
    assert row["admission_count"] == 2
    assert row["yield_reason"] == "new_evidence"


def test_inspect_trace_structured_search_event_is_consumed_without_model_text() -> None:
    rows = collect_search_io(
        [],
        {
            "inspect_trace": {
                "events": [
                    {
                        "seq": 9,
                        "kind": "search_trace",
                        "query": "q",
                        "raw_hits": [{"title": "T", "url": "https://example.test"}],
                        "provider_outcome": "error",
                        "provider_error": "upstream unavailable",
                    },
                    {"seq": 10, "kind": "model_io", "request": {"content": "secret"}},
                ]
            }
        },
    )
    assert len(rows) == 1
    assert rows[0]["provider_error"] == "upstream unavailable"
    assert "secret" not in json.dumps(rows)


def test_public_tool_result_structured_trace_is_flattened() -> None:
    rows = collect_search_io(
        [
            {
                "seq": 12,
                "elapsed_ms": 44,
                "kind": "live",
                "payload": {
                    "event": {
                        "kind": "tool_result",
                        "tool_result": {
                            "tool_name": "search",
                            "success": True,
                            "structured": {
                                "retrieval_trace": {
                                    "queries": [
                                        {
                                            "planned_query": "planned",
                                            "issued_query": "issued",
                                            "raw_discovered_hit_count": 1,
                                            "hits": [
                                                {
                                                    "title": "Hit",
                                                    "url": "https://example.test/hit",
                                                    "engine": "searxng",
                                                }
                                            ],
                                            "extraction": {"attempted": 1, "success": 1},
                                            "reranked_passage_count": 2,
                                        }
                                    ]
                                },
                                "added": 1,
                                "round": 2,
                                "yield_reason": "new_evidence",
                            },
                        },
                    }
                },
            }
        ]
    )
    assert len(rows) == 1
    assert rows[0]["planned_query"] == "planned"
    assert rows[0]["issued_query"] == "issued"
    assert rows[0]["raw_hit_count"] == 1
    assert rows[0]["passage_count"] == 2
    assert rows[0]["admission_count"] == 1


def test_provider_diagnostic_is_preserved_and_downgrades_outcome() -> None:
    rows = collect_search_io(
        [
            {
                "kind": "search_io",
                "query": "q",
                "raw_discovered_hit_count": 0,
                "provider_diagnostic": {
                    "unresponsive_engines": ["searxng"],
                    "provider_error": "timeout",
                    "ignored_secret": "do not copy",
                },
            }
        ]
    )
    assert rows[0]["provider_outcome"] == "error"
    assert rows[0]["provider_error"] == "timeout"
    assert rows[0]["provider_diagnostic"]["unresponsive_engines"] == ["searxng"]
    assert "ignored_secret" not in json.dumps(rows)


def test_nested_provider_diagnostic_takes_precedence_over_completed_call() -> None:
    rows = collect_search_io(
        [
            {
                "kind": "observation",
                "query": "q",
                "ok": True,
                "raw_discovered_hit_count": 0,
                "provider_diagnostic": {
                    "providers": {
                        "searxng": {
                            "provider_error": "HTTPStatusError",
                            "status_code": 502,
                            "result_count": 0,
                            "latency_ms": 41,
                        }
                    }
                },
            }
        ]
    )
    assert rows[0]["provider_outcome"] == "error"
    assert rows[0]["provider_error"] == "HTTPStatusError"
    assert rows[0]["provider_diagnostic"]["providers"]["searxng"]["status_code"] == 502
    assert rows[0]["latency_ms"] == 41


def test_multi_query_trace_keeps_each_issued_query_and_its_zero_extraction() -> None:
    rows = collect_search_io(
        [
            {
                "kind": "search_io",
                "round": 1,
                "retrieval_trace": {
                    "extraction": {
                        "attempted": 1,
                        "success": 0,
                        "failure": 1,
                        "statuses": [{"url": "https://example.test/a", "status": "blocked"}],
                    },
                    "queries": [
                        {
                            "planned_query": "planned",
                            "issued_query": "issued one",
                            "raw_discovered_hit_count": 1,
                            "hits": [{"url": "https://example.test/a", "title": "A"}],
                            "extraction": {"attempted": 1, "success": 0, "failure": 1},
                        },
                        {
                            "planned_query": "planned",
                            "issued_query": "issued two",
                            "raw_discovered_hit_count": 0,
                            "hits": [],
                            "extraction": {"attempted": 0, "success": 0, "failure": 0},
                        },
                    ],
                },
            }
        ]
    )
    assert [row["issued_query"] for row in rows] == ["issued one", "issued two"]
    assert rows[0]["extraction_ok_count"] == 0
    assert rows[0]["extraction_statuses"] == {"blocked": 1}
    assert rows[0]["extractions"][0]["url"] == "https://example.test/a"
    assert rows[1]["extraction_count"] == 0
    assert rows[1]["extraction_ok_count"] == 0


def test_extraction_error_class_is_retained_and_raw_class_text_is_dropped() -> None:
    rows = collect_search_io(
        [
            {
                "kind": "search_io",
                "query": "q",
                "extraction": {
                    "attempted": 2,
                    "success": 0,
                    "failure": 2,
                    "statuses": [
                        {
                            "url": "https://example.test/a",
                            "status": "blocked",
                            "error_class": "anti_bot",
                        },
                        {
                            "url": "https://example.test/b",
                            "status": "error",
                            "error_class": "raw exception text that must not pass",
                        },
                    ],
                },
            }
        ]
    )
    assert rows[0]["extractions"][0]["error_class"] == "anti_bot"
    assert "error_class" not in rows[0]["extractions"][1]
    assert rows[0]["extraction_error_classes"] == {"anti_bot": 1}


def test_retrieval_exception_replaces_action_only_row() -> None:
    rows = collect_search_io(
        [
            {
                "payload": {
                    "event": {
                        "kind": "action",
                        "tool_call": {
                            "tool_name": "search",
                            "arguments": {"query": "full planned query", "round": 2},
                        },
                    }
                }
            },
            {
                "payload": {
                    "event": {
                        "kind": "tool_result",
                        "tool_result": {
                            "structured": {
                                "query": "full planned query",
                                "subquestion": "full planned query",
                                "round": 2,
                                "ok": False,
                                "provider_error": "ConnectionError",
                            }
                        },
                    }
                }
            },
        ]
    )
    assert len(rows) == 1
    assert rows[0]["provider_outcome"] == "error"
    assert rows[0]["provider_error"] == "ConnectionError"


def test_run_writes_search_artifacts_for_old_and_new_runs(tmp_path) -> None:
    frames = [
        {"type": "state", "status": "running"},
        {
            "type": "event",
            "event": {
                "kind": "search_io",
                "query": "q",
                "raw_hits": [{"title": "T", "url": "https://example.test"}],
                "provider_outcome": "ok",
            },
        },
        {
            "type": "final",
            "answer": {
                "summary": "A substantive answer.",
                "sections": [],
                "passages": [],
                "depth_tier": "quick",
            },
        },
        {"type": "state", "status": "finished"},
    ]
    result = asyncio.run(
        run_harness(
            ResearchRequest("q", depth="quick"),
            transport=FakeTransport(frames),
            output_dir=tmp_path,
        )
    )
    assert result.search_io
    assert (tmp_path / "search_io.jsonl").exists()
    assert (tmp_path / "search_timeline.md").exists()
    assert (
        json.loads((tmp_path / "search_io.jsonl").read_text().splitlines()[0])["raw_hit_count"] == 1
    )
    assert "search_io_jsonl" in result.artifacts
