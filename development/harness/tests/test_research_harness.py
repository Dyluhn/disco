"""Tests for the injectable Deep Research observation harness."""

from __future__ import annotations

import asyncio
import json

import harness.research_harness_parts._run as run_parts
import harness.research_harness_parts._transports as transport_parts
import pytest
from harness.cassette import Cassette
from harness.research_harness import (
    DEFAULT_ACCEPTANCE_CORPUS,
    FakeTransport,
    Observation,
    ReplayTransport,
    ResearchRequest,
    _conversation_status,
    _is_terminal_research_frame,
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
                "summary": "A substantive answer explains the result and its important caveats [[p0]].",
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


def test_a_reports_unverified_sentences_reach_the_run_artifacts(tmp_path) -> None:
    """A shipped report says what it still gets wrong; the batch must show it.

    The writer always ships: sentences its NLI verifier could not ground after
    the rework go out AS WRITTEN and are named in `meta.unverified_sentences`.
    Four Flash Next acceptance runs failed the repetition ruler while their
    artifacts said nothing was declared — the normalizer had dropped `meta`,
    so an operator could not tell a known bounded miss from grader drift.
    """
    declared = [
        "Finding 12 remains supported by the cited source [[p0]], with distinct "
        "context for this evaluation."
    ]
    frames = _frames()
    frames[2]["answer"]["meta"] = {"unverified_sentences": declared}

    result = asyncio.run(
        run_harness(
            ResearchRequest("q", depth="exhaustive"),
            transport=FakeTransport(frames),
            output_dir=tmp_path,
        )
    )

    assert result.report["meta"]["unverified_sentences"] == declared
    assert result.telemetry["unverified_sentence_count"] == 1
    summary = (tmp_path / "summary.md").read_text()
    assert "## Unverified sentences (shipped as written)" in summary
    assert declared[0] in summary
    assert json.loads((tmp_path / "summary.json").read_text())["report"]["meta"]


def test_a_report_declaring_nothing_adds_no_declared_section(tmp_path) -> None:
    result = asyncio.run(
        run_harness(
            ResearchRequest("q", depth="exhaustive"),
            transport=FakeTransport(_frames()),
            output_dir=tmp_path,
        )
    )

    assert result.telemetry["unverified_sentence_count"] == 0
    assert "Unverified sentences" not in (tmp_path / "summary.md").read_text()


def test_report_length_is_telemetry_and_never_an_invariant() -> None:
    """ "Exhaustive" buys more research turns, not a longer report.

    A per-tier word band made padding the cheapest way to pass, so there is no
    length invariant at any tier; the word count is still recorded.
    """
    short = _frames()
    # Ten sentences where the old `exhaustive` band demanded 7,600+ words.
    short[2]["answer"]["sections"][0]["markdown"] = " ".join(
        f"Finding {index} remains supported by the cited source [[p0]], with distinct "
        "context for this evaluation."
        for index in range(10)
    )

    result = asyncio.run(
        run_harness(ResearchRequest("q", depth="exhaustive"), transport=FakeTransport(short))
    )

    assert "depth_length_target" not in result.invariants
    assert result.telemetry["report_words"] < 1000
    assert result.ok


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


def test_replay_restores_model_io_and_inspect_seams() -> None:
    cassette = Cassette()
    cassette.record("research.frames", {"query": "q"}, _frames())
    cassette.record(
        "research.model_io",
        {"query": "q"},
        [{"stage": "research_turn", "model": "m", "response": {"text": "visible"}}],
    )
    cassette.record(
        "research.inspect",
        {"query": "q"},
        {"events": [{"seq": 1, "kind": "model_io"}], "model_io": []},
    )
    result = asyncio.run(run_harness(ResearchRequest("q"), transport=ReplayTransport(cassette)))
    assert result.model_io == [
        {"stage": "research_turn", "model": "m", "response": {"text": "visible"}}
    ]
    assert result.inspect_trace == {"events": [{"seq": 1, "kind": "model_io"}], "model_io": []}


def test_replay_restores_provider_attempts_separately_from_model_io(tmp_path) -> None:
    cassette = Cassette()
    cassette.record("research.frames", {"query": "q"}, _frames())
    started = {
        "stage": "router",
        "attempt": 1,
        "provider": "fake",
        "model": "m",
        "outcome": "started",
        "latency_ms": None,
        "error_class": None,
        "retry_scheduled": None,
    }
    attempt = {
        "stage": "router",
        "attempt": 1,
        "provider": "fake",
        "model": "m",
        "outcome": "error",
        "latency_ms": 180000,
        "error_class": "LLMTransientError",
        "retry_scheduled": True,
    }
    cassette.record(
        "research.inspect",
        {"query": "q"},
        {
            "events": [
                {"seq": 1, "kind": "model_attempt", **started},
                {"seq": 2, "kind": "model_attempt", **attempt},
            ],
            "model_attempts": [started, attempt],
            "model_io": [],
        },
    )
    result = asyncio.run(
        run_harness(
            ResearchRequest("q"),
            transport=ReplayTransport(cassette),
            output_dir=tmp_path,
        )
    )
    assert result.model_io == []
    assert result.provider_attempts == [started, attempt]
    assert result.telemetry["provider_attempt_count"] == 1
    assert result.telemetry["provider_attempt_event_count"] == 2
    assert result.telemetry["provider_retry_count"] == 1
    attempt_rows = [
        json.loads(line) for line in (tmp_path / "provider_attempts.jsonl").read_text().splitlines()
    ]
    assert attempt_rows == [started, attempt]
    timeline = (tmp_path / "timeline.md").read_text()
    assert "provider attempt" in timeline
    assert "LLMTransientError" in timeline


def test_live_timeout_still_fetches_inflight_provider_attempt(monkeypatch) -> None:
    import httpx

    class _Response:
        def __init__(self, body):
            self._body = body
            self.is_success = True

        def raise_for_status(self):
            return None

        def json(self):
            return self._body

    class _Client:
        def __init__(self, *args, **kwargs):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            return _Response({"conversation_id": "conv-timeout"})

        async def get(self, path):
            assert path == "/api/debug/evidence/conv-timeout"
            row = {
                "stage": "router",
                "attempt": 1,
                "provider": "fake",
                "model": "m",
                "outcome": "started",
                "latency_ms": None,
                "error_class": None,
                "retry_scheduled": None,
            }
            return _Response(
                {
                    "inspect_trace": {
                        "events": [{"seq": 1, "kind": "model_attempt", **row}],
                        "model_attempts": [row],
                        "model_io": [],
                    }
                }
            )

    class _WebSocket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def recv(self):
            return json.dumps({"type": "state", "status": "running"})

        async def send(self, payload):
            return None

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(0.05)
            raise StopAsyncIteration

    async def _credentials(_self, _request):
        return (
            {"Cookie": "disco_session=test", "Origin": "http://server"},
            {"X-Disco-CSRF": "csrf"},
        )

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    monkeypatch.setattr(
        transport_parts.LiveWebSocketTransport,
        "_session_credentials",
        _credentials,
    )
    monkeypatch.setattr(transport_parts, "_connect", lambda *args, **kwargs: _WebSocket())
    transport = transport_parts.LiveWebSocketTransport("http://server")
    observer = Observation()

    with pytest.raises(TimeoutError):
        asyncio.run(
            transport.collect(ResearchRequest("q", timeout_s=0.001, capture_inspect=True), observer)
        )
    assert observer.provider_attempts[0]["outcome"] == "started"


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


def test_redaction_preserves_ordinary_citation_urls() -> None:
    urls = [
        "https://example.com/ai-safety-report-global-risks-and-governance/",
        "https://example.com/2026-ai-laws-update-key-regulations-and-guidance/",
    ]
    assert redact(urls) == urls


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
                "kind": "research_checkpoint",
                "query": "q",
                "passages": [],
                "trail": [],
                "completed_queries": [],
            },
        },
        {"type": "state", "status": "paused"},
    ]
    result = asyncio.run(run_harness(ResearchRequest("q"), transport=FakeTransport(frames)))
    assert result.report["kind"] == "research_checkpoint"
    assert result.invariants["stopped_checkpoint_valid"]
    assert result.ok


def test_empty_report_is_a_checkpoint_only_when_bounded_by_stopped() -> None:
    observer = Observation()
    empty_unstopped = {"query": "q", "summary": "", "sections": [], "passages": []}
    with pytest.raises(ValueError, match="executive summary"):
        render_report_markdown(empty_unstopped)
    checks = check_invariants(empty_unstopped, "", observer)
    assert not checks["final_report_exists"]
    assert not checks["executive_summary_substantive"]
    assert checks["stopped_checkpoint_valid"]  # the stopped rule simply does not apply

    stopped_with_body = {
        "kind": "research_checkpoint",
        "query": "q",
        "summary": "",
        "sections": [{"id": "s0", "title": "T", "markdown": "left-over body"}],
        "passages": [],
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
        "sections": [{"id": "s0", "title": "Findings", "markdown": " ".join([sentence] * 200)}],
        "passages": [{"id": "p0", "source_url": "https://example.test/source"}],
    }
    markdown = "# Title\n\n## Executive summary\n\nSummary [[p0]]\n\n#### Findings\n\n" + sentence
    checks = check_invariants(report, markdown, observer)
    assert not checks["repetition_acceptable"]
    assert not checks["heading_hierarchy_valid"]


def test_an_uncited_executive_summary_fails_the_batch() -> None:
    """Observed live (rewrite2 run-05): a rework re-ask answered with the
    ten-word fragment below and the splice shipped it as the summary. Long
    enough for the substantive floor; not the report's answer."""
    observer = Observation()
    body = "The convergence holds across the observed window [[p0]]. " * 12
    report = {
        "summary": "The cap, or a more stable and more virt national.",
        "sections": [{"id": "s0", "title": "Findings", "markdown": body}],
        "passages": [{"id": "p0", "source_url": "https://example.test/source"}],
    }

    checks = check_invariants(report, "# Report\n\n## Findings\n\n" + body, observer)

    assert checks["executive_summary_substantive"]
    assert not checks["executive_summary_cited"]

    report["summary"] = "The convergence holds, and both records measure it directly [[p0]]."
    assert check_invariants(report, "# Report\n\n## Findings\n\n" + body, observer)[
        "executive_summary_cited"
    ]


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


def test_batch_runs_concurrently_with_bounded_isolated_transports(tmp_path) -> None:
    active = 0
    peak = 0
    lock = asyncio.Lock()

    async def frames_for(request):
        nonlocal active, peak
        async with lock:
            active += 1
            peak = max(peak, active)
        await asyncio.sleep(0.01)
        async with lock:
            active -= 1
        frames = _frames()
        frames[2]["answer"]["query"] = request.query
        return frames

    queries = [ResearchRequest(f"q-{i}", depth="exhaustive") for i in range(5)]
    results = asyncio.run(
        run_batch(
            queries,
            transport_factory=lambda _request: FakeTransport(frames_for),
            output_dir=tmp_path,
            max_concurrency=2,
        )
    )
    assert peak == 2
    assert [result.request["query"] for result in results] == [f"q-{i}" for i in range(5)]
    assert len({result.run_id for result in results}) == 5
    assert all(result.ok for result in results)
    assert all((tmp_path / f"run-{i:02d}" / "model_io.jsonl").exists() for i in range(1, 6))
    manifest = json.loads((tmp_path / "batch_summary.json").read_text())
    assert manifest["runs_total"] == 5
    assert manifest["runs_passed"] == 5
    assert manifest["concurrency"] == 2


def test_batch_groups_repeated_queries_for_reliability_and_variance(tmp_path) -> None:
    results = asyncio.run(
        run_batch(
            [ResearchRequest("same", depth="exhaustive") for _ in range(3)],
            transport_factory=lambda _request: FakeTransport(_frames()),
            output_dir=tmp_path,
            max_concurrency=3,
        )
    )
    assert all(result.ok for result in results)
    manifest = json.loads((tmp_path / "batch_summary.json").read_text())
    grouped = manifest["aggregate"]["query_reliability"]["same"]
    assert grouped["runs"] == 3
    assert grouped["passed"] == 3
    assert grouped["pass_rate"] == 1.0
    assert grouped["report_words"]["variance"] == 0.0
    assert grouped["thrash_clean_rate"] == 1.0
    assert manifest["aggregate"]["thrash_clean_rate"] == 1.0
    assert manifest["aggregate"]["quality"]["distinct_works_p50"] == 1
    assert "Query reliability" in (tmp_path / "batch_summary.md").read_text()


def test_batch_isolates_worker_failure_and_keeps_sibling_artifact(tmp_path) -> None:
    async def frames_for(request):
        if request.query == "bad":
            raise RuntimeError("synthetic transport failure")
        return _frames()

    results = asyncio.run(
        run_batch(
            [
                ResearchRequest("bad", depth="exhaustive"),
                ResearchRequest("good", depth="exhaustive"),
            ],
            transport_factory=lambda _request: FakeTransport(frames_for),
            output_dir=tmp_path,
            max_concurrency=2,
        )
    )
    assert results[0].ok is False
    assert results[1].ok is True
    assert (tmp_path / "run-01" / "summary.json").exists()
    assert (tmp_path / "run-02" / "report.md").exists()


def test_batch_isolates_failure_artifact_write_error(monkeypatch, tmp_path) -> None:
    original = run_parts.write_artifacts

    def fail_first(result, output_dir):
        if str(output_dir).endswith("run-01"):
            raise OSError("synthetic disk failure")
        return original(result, output_dir)

    monkeypatch.setattr(run_parts, "write_artifacts", fail_first)
    results = asyncio.run(
        run_batch(
            [
                ResearchRequest("first", depth="exhaustive"),
                ResearchRequest("second", depth="exhaustive"),
            ],
            transport_factory=lambda _request: FakeTransport(_frames()),
            output_dir=tmp_path,
            max_concurrency=2,
        )
    )
    assert [result.ok for result in results] == [False, True]
    assert "failed to write failure artifacts" in results[0].errors[-1]
    assert (tmp_path / "run-02" / "report.md").exists()


def test_model_io_is_written_and_thrash_metrics_are_present(tmp_path) -> None:
    frames = _frames()
    frames.insert(
        1,
        {"type": "model_io", "model_io": {"model": "m", "response": {"text": "search"}}},
    )
    frames.insert(2, {"type": "probe", "phase": "search", "query": "same"})
    frames.insert(3, {"type": "probe", "phase": "search", "query": "same"})
    result = asyncio.run(
        run_harness(
            ResearchRequest("q", depth="exhaustive"),
            transport=FakeTransport(frames),
            output_dir=tmp_path,
        )
    )
    assert result.telemetry["model_io_count"] == 1
    assert result.thrash["signals"]["max_repeated_query"] == 2
    assert (tmp_path / "model_io.jsonl").read_text().count("\n") == 1
    assert json.loads((tmp_path / "thrash.json").read_text())["passed"]


def test_report_quality_metrics_use_work_identity_and_claims() -> None:
    frames = _frames()
    answer = frames[2]["answer"]
    answer["passages"].append(
        {
            "id": "p1",
            "source_url": "https://publisher.test/paper/10.1234/example",
            "source_title": "Mirror",
            "text": "The result.",
        }
    )
    answer["passages"][0]["source_url"] = "https://doi.org/10.1234/example"
    answer["claims"] = [
        {
            "section_id": "s0",
            "claim": {
                "text": "The measured result improved by 42% in version 2.1.",
                "cited_passage_ids": ["p0", "p1"],
            },
            "verdict": "supported",
        }
    ]

    result = asyncio.run(
        run_harness(ResearchRequest("q", depth="exhaustive"), transport=FakeTransport(frames))
    )

    quality = result.telemetry["quality"]
    assert quality["passage_count"] == 2
    assert quality["distinct_work_count"] == 1
    assert quality["high_specificity_claims"] == 1
    assert quality["single_work_specific_claims"] == 1
    assert quality["dominant_work_claim_share"] == 1.0


def test_diagnostic_thrash_does_not_fail_a_valid_report(tmp_path) -> None:
    frames = _frames()
    frames[1:1] = [
        {"type": "probe", "phase": "search", "query": f"pivot-{index}", "rows": 0}
        for index in range(3)
    ]
    result = asyncio.run(
        run_harness(
            ResearchRequest("q", depth="exhaustive"),
            transport=FakeTransport(frames),
            output_dir=tmp_path,
        )
    )

    assert result.ok
    assert result.invariants["thrash_clean"] is False
    assert any(finding["code"] == "NO_PROGRESS" for finding in result.thrash["findings"])
    assert json.loads((tmp_path / "thrash.json").read_text())["passed"] is False


def test_provider_error_still_fails_a_valid_report() -> None:
    frames = _frames()
    frames.insert(1, {"type": "error", "error": "provider rejected the request"})
    result = asyncio.run(run_harness(ResearchRequest("q"), transport=FakeTransport(frames)))

    assert not result.ok
    assert result.report["sections"]
    assert result.errors == ["provider rejected the request"]


def test_nonterminal_search_diagnostic_is_not_a_run_error() -> None:
    observer = Observation()
    observer.record(
        {
            "type": "event",
            "event": {
                "kind": "observation",
                "tool_result": {
                    "success": True,
                    "structured": {
                        "yield_reason": "extraction_failure",
                        "detail": "No usable evidence: extraction failure.",
                        "hits": [
                            {
                                "snippet": "The source discusses an exception.",
                                "status": "error",
                            }
                        ],
                        "retrieval_trace": {
                            "extraction": {
                                "statuses": [
                                    {
                                        "url": "https://example.test/paper",
                                        "status": "error",
                                        "fetched_ok": False,
                                    }
                                ]
                            }
                        },
                    },
                },
            },
        }
    )

    assert observer.errors == []


def test_provider_control_failure_does_not_create_secondary_report_error() -> None:
    """A terminal provider/control failure has no report to validate.

    The harness must preserve the real failure as the only error rather than
    trying to serialize the empty normalized placeholder as a ReportEvent.
    """
    frames = [
        {"type": "state", "status": "running"},
        {"type": "error", "error": "research control call failed"},
        {"type": "state", "status": "error"},
    ]

    result = asyncio.run(run_harness(ResearchRequest("q"), transport=FakeTransport(frames)))

    assert not result.ok
    assert result.errors == ["research control call failed"]
    assert "ReportContractError" not in "\n".join(result.errors)
    assert result.report["summary"] == ""
    assert result.report["sections"] == []
    assert result.report_markdown == ""


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


def test_live_terminal_classifier_stops_on_pause_and_checkpoint() -> None:
    assert _is_terminal_research_frame({"type": "event", "event": {"kind": "research_checkpoint"}})
    assert _is_terminal_research_frame(
        {"type": "event", "event": {"kind": "status", "status": "PAUSED"}}
    )
    assert _is_terminal_research_frame({"type": "state", "status": "paused"})
