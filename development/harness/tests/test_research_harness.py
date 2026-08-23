"""Tests for the injectable Deep Research observation harness."""

from __future__ import annotations

import asyncio
import json

from harness.cassette import Cassette
from harness.research_harness import (
    DEFAULT_ACCEPTANCE_CORPUS,
    FakeTransport,
    Observation,
    ReplayTransport,
    ResearchRequest,
    _conversation_status,
    check_invariants,
    phase_for,
    redact,
    render_report_markdown,
    run_batch,
    run_harness,
)


def _frames() -> list[dict]:
    body = " ".join(
        f"Finding {index} remains supported by the cited source [[p0]], with distinct context "
        "for this evaluation."
        for index in range(750)
    )
    return [
        {"type": "state", "status": "running"},
        {"type": "probe", "phase": "search", "query": "a stable query"},
        {
            "type": "final",
            "answer": {
                "query": "q",
                "summary": "A substantive answer explains the result and its important caveats.",
                "sections": [
                    {
                        "id": "s0",
                        "title": "Findings",
                        "markdown": body,
                        "cited_passage_ids": ["p0"],
                    }
                ],
                "passages": [
                    {
                        "id": "p0",
                        "source_url": "https://example.test/source",
                        "source_title": "Example source",
                        "text": "The result.",
                    }
                ],
                "depth_tier": "exhaustive",
            },
        },
        {"type": "state", "status": "finished"},
    ]


def test_fake_transport_injects_request_controls_and_writes_artifacts(tmp_path) -> None:
    transport = FakeTransport(_frames())
    request = ResearchRequest(
        "q", depth="exhaustive", recency="week", model="model-x", provider="brave"
    )
    result = asyncio.run(run_harness(request, transport=transport, output_dir=tmp_path))

    assert result.ok
    assert transport.requests[0].depth == "exhaustive"
    assert result.telemetry["requested_recency"] == "week"
    assert result.telemetry["phase_counts"]["search"] == 1
    assert result.telemetry["phase_counts"]["final"] == 1
    assert (tmp_path / "report.md").exists()
    assert (tmp_path / "summary.json").exists()
    assert (tmp_path / "cassette.jsonl").exists()
    assert (tmp_path / "events.jsonl").read_text().count("\n") == 4

    replay = asyncio.run(
        run_harness(
            ResearchRequest("q", depth="exhaustive"),
            transport=ReplayTransport(tmp_path / "cassette.jsonl"),
        )
    )
    assert replay.ok
    assert replay.report == result.report


def test_replay_uses_existing_cassette_frame_seam_and_is_stable() -> None:
    cassette = Cassette()
    cassette.record("research.frames", {"query": "q"}, _frames())
    request = ResearchRequest("q", cassette="ignored-by-injected-transport")

    def run_once():
        result = asyncio.run(run_harness(request, transport=ReplayTransport(cassette)))
        return result.report, [(event.kind, event.phase, event.payload) for event in result.events]

    report_a, events_a = run_once()
    report_b, events_b = run_once()
    assert report_a == report_b
    assert events_a == events_b


def test_redaction_covers_secret_keys_and_bearer_like_values() -> None:
    clean = redact(
        {
            "api_key": "super-secret-value",
            "authorization": "Bearer super-secret-value",
            "message": "prefix sk-live_12345678901234567890 suffix",
        }
    )
    assert "super-secret-value" not in json.dumps(clean)
    assert "sk-live_12345678901234567890" not in json.dumps(clean)
    assert clean["api_key"] == "[REDACTED]"


def test_run_result_does_not_expose_auth_token() -> None:
    request = ResearchRequest("q", auth_token="Bearer very-secret-token-value")
    result = asyncio.run(run_harness(request, transport=FakeTransport(_frames())))
    assert result.request["auth_token"] == "[REDACTED]"
    assert "very-secret-token-value" not in json.dumps(result.as_dict())


def test_missing_report_structure_is_not_manufactured() -> None:
    # The wire had prose but no sections and no summary.  The harness judges the
    # artifact as-is: no manufactured "Findings" section, no summary copied from
    # a body — the invariants surface the contract violation instead.
    frames = [
        {"type": "state", "status": "running"},
        {
            "type": "final",
            "answer": {
                "query": "q",
                "sections": [],
                "answer_markdown": "Prose without report structure [[p0]].",
                "passages": [{"id": "p0", "source_url": "https://example.test/source"}],
            },
        },
        {"type": "state", "status": "finished"},
    ]
    result = asyncio.run(run_harness(ResearchRequest("q"), transport=FakeTransport(frames)))
    assert result.report["sections"] == []
    assert result.report["summary"] == ""
    assert not result.ok
    assert not result.invariants["final_report_exists"]
    assert not result.invariants["executive_summary_substantive"]
    assert "Findings" not in json.dumps(result.report)


def test_missing_summary_stays_empty_and_fails_the_invariant() -> None:
    frames = _frames()
    frames[2]["answer"].pop("summary")
    result = asyncio.run(
        run_harness(ResearchRequest("q", depth="exhaustive"), transport=FakeTransport(frames))
    )
    assert result.report["summary"] == ""
    assert not result.invariants["executive_summary_substantive"]
    assert not result.ok


def test_legacy_cassette_replay_does_not_fabricate_a_report() -> None:
    cassette = Cassette()
    cassette.record(
        "search",
        {"query": "q"},
        [{"url": "https://example.test/hit", "title": "Hit", "snippet": "recorded snippet"}],
    )
    cassette.record(
        "extract",
        {"url": "https://example.test/hit"},
        {"passages": [{"id": "p0", "source_url": "https://example.test/hit", "text": "rec"}]},
    )
    cassette.record("llm.complete", {"messages": []}, {"text": "A recorded answer."})
    result = asyncio.run(run_harness(ResearchRequest("q"), transport=ReplayTransport(cassette)))
    assert result.report.get("legacy_replay") is True
    assert result.report["sections"] == []
    assert result.report["summary"] == ""
    assert not result.ok
    serialized = json.dumps(result.as_dict())
    assert "Recorded findings" not in serialized
    assert "did not include an answer text" not in serialized


def test_stopped_checkpoint_is_judged_as_checkpoint() -> None:
    frames = [
        {"type": "state", "status": "running"},
        {
            "type": "final",
            "answer": {
                "query": "q",
                "summary": "",
                "sections": [],
                "passages": [],
                "bounded_by": "stopped",
            },
        },
        {"type": "state", "status": "finished"},
    ]
    result = asyncio.run(run_harness(ResearchRequest("q"), transport=FakeTransport(frames)))
    assert result.report["bounded_by"] == "stopped"
    assert result.invariants["stopped_checkpoint_valid"]
    assert result.invariants["depth_length_target"]
    assert result.ok


def test_empty_report_is_a_checkpoint_only_when_bounded_by_stopped() -> None:
    observer = Observation()
    empty_unstopped = {"query": "q", "summary": "", "sections": [], "passages": []}
    checks = check_invariants(
        empty_unstopped, render_report_markdown(empty_unstopped), observer
    )
    assert not checks["final_report_exists"]
    assert not checks["executive_summary_substantive"]
    assert checks["stopped_checkpoint_valid"]  # the stopped rule simply does not apply

    stopped_with_body = {
        "query": "q",
        "summary": "",
        "sections": [{"id": "s0", "title": "T", "markdown": "left-over body"}],
        "passages": [],
        "bounded_by": "stopped",
    }
    checks = check_invariants(
        stopped_with_body, render_report_markdown(stopped_with_body), observer
    )
    assert not checks["stopped_checkpoint_valid"]


def test_timeout_defaults_derive_from_depth() -> None:
    assert ResearchRequest("q", depth="quick").timeout_s == 600.0
    assert ResearchRequest("q").timeout_s == 1500.0
    assert ResearchRequest("q", depth="exhaustive").timeout_s == 3000.0
    assert ResearchRequest("q", depth="exhaustive", timeout_s=42.0).timeout_s == 42.0


def test_failed_or_empty_report_fails_closed() -> None:
    observer = Observation()
    report = {
        "summary": "No sources were available; the provider failed.",
        "sections": [{"id": "s0", "title": "", "markdown": ""}],
        "passages": [],
    }
    checks = check_invariants(report, "", observer)
    assert not checks["final_report_exists"]
    assert not checks["executive_summary_substantive"]
    assert not checks["no_empty_or_failure_sections"]
    assert not checks["citations_resolve"]
    observer.record(
        {
            "type": "event",
            "event": {
                "kind": "status",
                "status": "ERROR",
                "detail": "provider rejected the request",
            },
        }
    )
    assert observer.errors == ["provider rejected the request"]


def test_quality_gates_reject_thin_damaged_and_diagnostic_reports() -> None:
    observer = Observation()
    body = "..."
    report = {
        "summary": "This report used a retrieval process and confidence labels.",
        "sections": [{"id": "s0", "title": "Findings", "markdown": body}],
        "passages": [{"id": "p0", "source_url": "https://example.test/source"}],
    }
    checks = check_invariants(report, "# Title\n\n### Executive summary\n\n...", observer)
    assert not checks["substantive_section_bodies"]
    assert not checks["no_diagnostic_or_research_process_language"]
    assert not checks["heading_hierarchy_valid"]


def test_quality_gate_allows_normal_analytical_report_language() -> None:
    observer = Observation()
    sentence = (
        "Retrieval-augmented generation links a model to external evidence, while this report "
        "distinguishes measured results from vendor claims [[p0]]."
    )
    body = " ".join(
        f"{sentence} Distinct implication {index} follows [[p0]]." for index in range(15)
    )
    report = {
        "summary": "The evidence supports a qualified conclusion about the technology [[p0]].",
        "sections": [{"id": "s0", "title": "Findings", "markdown": body}],
        "passages": [{"id": "p0", "source_url": "https://example.test/source"}],
    }
    checks = check_invariants(
        report,
        "# Title\n\n## Executive summary\n\nSummary [[p0]]\n\n## Findings\n\n" + body,
        observer,
    )
    assert checks["no_diagnostic_or_research_process_language"]


def test_quality_gates_reject_repeated_prose_and_heading_jumps() -> None:
    observer = Observation()
    sentence = "A distinct finding is established by the cited source [[p0]]."
    report = {
        "summary": "A substantive answer explains the result and caveats [[p0]].",
        "sections": [
            {"id": "s0", "title": "Findings", "markdown": " ".join([sentence] * 200)}
        ],
        "passages": [{"id": "p0", "source_url": "https://example.test/source"}],
    }
    markdown = "# Title\n\n## Executive summary\n\nSummary [[p0]]\n\n#### Findings\n\n" + sentence
    checks = check_invariants(report, markdown, observer)
    assert not checks["repetition_acceptable"]
    assert not checks["heading_hierarchy_valid"]


def test_quality_gate_rejects_summary_copied_from_one_section() -> None:
    observer = Observation()
    copied = (
        "The opening finding establishes the background with supporting evidence [[p0]]. "
        "A second opening sentence adds context but does not synthesize the report [[p0]]."
    )
    report = {
        "summary": copied,
        "sections": [
            {"id": "s0", "title": "Background", "markdown": copied},
            {
                "id": "s1",
                "title": "Bottom line",
                "markdown": (
                    "The technology works only in narrow controlled conditions [[p0]]. " * 12
                ),
            },
        ],
        "passages": [{"id": "p0", "source_url": "https://example.test/source"}],
    }

    checks = check_invariants(report, "# Report\n\n## Executive summary\n\n" + copied, observer)

    assert not checks["executive_summary_synthesizes_report"]


def test_batch_preserves_each_report_and_internal_trace(tmp_path) -> None:
    requests = [
        ResearchRequest(query, depth="exhaustive") for query in DEFAULT_ACCEPTANCE_CORPUS[:2]
    ]
    results = asyncio.run(
        run_batch(
            requests,
            transport_factory=lambda _request: FakeTransport(_frames()),
            output_dir=tmp_path,
        )
    )
    assert len(results) == 2
    assert all(result.ok for result in results)
    assert (tmp_path / "run-01" / "report.md").exists()
    assert (tmp_path / "run-02" / "events.jsonl").exists()
    assert (tmp_path / "run-01" / "cassette.jsonl").exists()
    assert (tmp_path / "batch_summary.json").exists()


def test_phase_classifier_names_observable_pipeline_stages() -> None:
    assert phase_for({"type": "search", "query": "q"}) == "search"
    assert phase_for({"type": "extract", "url": "https://example.test"}) == "extract"
    assert phase_for({"phase": "gap_analysis"}) == "gap"
    assert phase_for({"kind": "report"}) == "synthesis"
    assert phase_for({"type": "verification", "claim": "x"}) == "verification"
    assert (
        _conversation_status(
            {"type": "state", "state": {"execution_status": "AWAITING_PLAN_APPROVAL"}}
        )
        == "AWAITING_PLAN_APPROVAL"
    )
    assert (
        _conversation_status({"type": "event", "event": {"kind": "status", "status": "FINISHED"}})
        == "FINISHED"
    )
    assert _conversation_status({"type": "event", "event": {"kind": "report"}}) == ""
