"""Structured observability (core.obs) + the trace-capture helper (harness.logcap)
— the substrate the eval/fault/replay harnesses assert against.

Run: PYTHONPATH=. uv run pytest harness/tests/test_obs.py
"""

from __future__ import annotations

from perpleximanus.core.obs import log_event, log_span

from harness.logcap import capture_spans, ends, sequence


def test_log_span_emits_start_and_end_with_measured_fields():
    with capture_spans() as spans:
        with log_span("agent.step", role="agent_driver") as s:
            s["out_tokens"] = 42  # measured inside the body lands on the `end` record
    starts = [x for x in spans if x.get("event") == "start"]
    e = ends(spans, "agent.step")
    assert starts and e, "span must emit both start and end"
    assert e[0]["role"] == "agent_driver"
    assert e[0]["out_tokens"] == 42
    assert "ms" in e[0] and isinstance(e[0]["ms"], int)  # duration measured


def test_span_sequence_is_ordered():
    with capture_spans() as spans:
        with log_span("a"):
            pass
        with log_span("b"):
            pass
    assert sequence(spans) == ["a", "b"]  # the pipeline-order assertion primitive


def test_log_event_is_a_point():
    with capture_spans() as spans:
        log_event("ddgs.search", rows=10)
    pts = [x for x in spans if x.get("event") == "point"]
    assert pts and pts[0]["span"] == "ddgs.search" and pts[0]["rows"] == 10
