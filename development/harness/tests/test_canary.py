"""Canary judgement logic (plan Phase 8) — tested against the VERBATIM frame
protocol emitted by `retrieval/streaming.py` (state → token → final → state, or
an error frame). Proves the canary alerts on the real degradation modes: a dead
dependency, a pipeline error, and the silent "up but not grounding" case.

Run: PYTHONPATH=. uv run pytest development/harness/tests/test_canary.py
"""

from __future__ import annotations

from harness.canary import evaluate_health, evaluate_research_frames

# A real grounded run, as the WS emits it (shapes from streaming.py:285-355).
_GROUNDED = [
    {"type": "state", "status": "running"},
    {"type": "token", "token": "Paris", "block_id": "answer"},
    {"type": "token", "token": " is the capital.", "block_id": "answer"},
    {
        "type": "final",
        "answer": {
            # The real /ws/research final-frame shape: text lives in `blocks` with
            # inline `cited_passage_ids` (NOT `answer_markdown`, which is the offline
            # GroundingPipeline shape — accepting only that field is what made the
            # canary mis-report a genuinely-grounded live answer as "empty prose").
            "query": "What is the capital of France?",
            "blocks": [
                {
                    "kind": "prose",
                    "id": "b0",
                    "text": "Paris is the capital [[src1_p0]].",
                    "cited_passage_ids": ["src1_p0"],
                }
            ],
            "claims": [
                {
                    "claim": {"text": "Paris is the capital", "cited_passage_ids": ["src1_p0"]},
                    "verdict": "supported",
                }
            ],
            "passages": [{"id": "src1_p0", "source_url": "https://x.test", "text": "..."}],
            "all_hits": [{"url": "https://x.test"}],
            "unsupported_count": 0,
        },
    },
    {"type": "state", "status": "finished"},
]


# ---- health ----------------------------------------------------------------


def test_health_ok():
    r = evaluate_health(200, {"status": "ok", "checks": {"store": "ok"}})
    assert r.ok


def test_health_degraded_503_fails():
    r = evaluate_health(503, {"status": "degraded", "checks": {"store": "error: locked"}})
    assert not r.ok and "degraded" in r.detail


def test_health_unexpected_200_body_fails():
    # a 200 with a non-ok status (half-up server) must still alert.
    r = evaluate_health(200, {"status": "starting"})
    assert not r.ok


# ---- research --------------------------------------------------------------


def test_research_grounded_passes():
    r = evaluate_research_frames(_GROUNDED)
    assert r.ok and "1 sources" in r.detail


def test_research_pipeline_error_fails():
    frames = [
        {"type": "state", "status": "running"},
        {"type": "error", "message": "Found 5 sources but couldn't read any of them right now"},
    ]
    r = evaluate_research_frames(frames)
    assert not r.ok and "couldn't read" in r.detail


def test_research_zero_sources_fails():
    # the SILENT degradation: a final frame arrives, but cites nothing (not grounded).
    frames = [
        {"type": "final", "answer": {"answer_markdown": "Paris.", "passages": [], "all_hits": []}},
        {"type": "state", "status": "finished"},
    ]
    r = evaluate_research_frames(frames)
    assert not r.ok and "ZERO sources" in r.detail


def test_research_no_final_frame_fails():
    # stream cut off before completing → alert (the agent-server-died-mid-run case).
    frames = [{"type": "state", "status": "running"}, {"type": "token", "token": "Par"}]
    r = evaluate_research_frames(frames)
    assert not r.ok and "no final frame" in r.detail
