"""Cluster 1 — context survival: A-S1 model-aware threshold, A-S2 Snip,
A-S4 structured summarizer, D pin-plan + recency recitation."""

from __future__ import annotations

from disco.core import (
    ActionEvent,
    ObservationEvent,
    PlanEvent,
    ToolCall,
    ToolResult,
)
from disco.core.events import snip_content
from disco.core.view import LLMSummarizingCondenser, View


def _seq(events):
    return [e.model_copy(update={"seq": i}) for i, e in enumerate(events, start=1)]


# ---- A-S1: model-aware condensation threshold -------------------------------


def test_condenser_derives_thresholds_from_context_window():
    # C9: thresholds derive from the window AND grow above the working budget up to
    # a sane ceiling (4× = 96k soft / 128k hard). A 200k window → 0.65× = 130k
    # exceeds the 24k budget, so the threshold grows to min(130k, 96k) = 96k —
    # NOT pinned to 24k. Cost still scales with input tokens per call (the cap
    # exists) but big-window models get to use their room.
    c = LLMSummarizingCondenser(context_window=200_000)
    assert c._max == 96_000  # min(130k, 4*24k ceiling), NOT 24_000
    assert c._hard == 128_000  # min(160k, 4*32k ceiling), NOT 32_000


def test_condenser_falls_back_to_safe_defaults_without_window():
    c = LLMSummarizingCondenser()
    assert c._max == 24_000 and c._hard == 32_000


def test_condenser_explicit_overrides_win():
    c = LLMSummarizingCondenser(context_window=200_000, max_tokens=50, hard_max_tokens=60)
    assert c._max == 50 and c._hard == 60


def test_should_condense_fires_at_derived_soft_and_hard():
    # 100k window → 0.65× = 65k (above 24k budget, below 96k ceiling) → soft=65k;
    # 0.80× = 80k → hard=80k. should_condense waits for these (not the old 24k/32k).
    c = LLMSummarizingCondenser(context_window=100_000)
    view = View(messages=[], visible_seqs=[], total_events=0, forgotten_count=0)
    assert c.should_condense(view, token_count=10_000) is None  # under soft
    assert c.should_condense(view, token_count=66_000).soft is True  # past soft 65k
    assert c.should_condense(view, token_count=81_000).soft is False  # past hard 80k


# ---- A-S2: Snip large observations ------------------------------------------


def test_snip_content_leaves_small_untouched():
    assert snip_content("short", max_chars=100, head=50, tail=20) == "short"


def test_snip_content_trims_with_recoverable_marker():
    big = "A" * 5000 + "B" * 5000
    out = snip_content(big, max_chars=8000, head=5000, tail=2000)
    assert len(out) < len(big)
    assert "snipped" in out
    assert "file_read" in out
    assert out.startswith("A")
    assert out.rstrip().endswith("B")


def test_observation_event_snips_large_content_in_llm_message():
    huge = "x" * 20_000
    obs = ObservationEvent(
        tool_result=ToolResult(call_id="c", tool_name="shell", success=True, content=huge),
        action_id="a",
    )
    msg = obs.to_llm_message()
    assert len(msg.content) < len(huge)
    assert "snipped" in msg.content
    # The raw event payload is NOT mutated — snip is render-only (reversible).
    assert obs.tool_result.content == huge


# ---- D: pin the PlanEvent against condensation ------------------------------


def test_latest_plan_is_pinned_and_never_forgotten():
    from disco.core.events import CondensationEvent

    events = _seq(
        [
            PlanEvent(summary="build it", steps=[{"title": "a"}], revision=1),
            ObservationEvent(
                tool_result=ToolResult(call_id="c", tool_name="shell", success=True, content="x"),
                action_id="a1",
            ),
        ]
    )
    # A tombstone that forgets seq 1..2 (covering the plan).
    tomb = CondensationEvent(
        forgotten_start_seq=1, forgotten_end_seq=2, summary="[summary]", summary_role="user"
    ).model_copy(update={"seq": 3})
    view = View.of([*events, tomb])
    # The plan's restated steps must still be present (pinned), despite the range.
    joined = " ".join(m.content for m in view.messages)
    assert "build it" in joined


# ---- D: recency recitation appended at the tail -----------------------------


def test_recitation_appended_at_tail_with_checklist():
    events = _seq(
        [
            PlanEvent(
                summary="ship the page",
                steps=[{"title": "scaffold"}, {"title": "style"}, {"title": "deploy"}],
                revision=1,
            ),
            ActionEvent(
                thought="step 1 done",
                tool_call=ToolCall(tool_name="plan_step", arguments={"index": 1, "state": "done"}),
            ),
        ]
    )
    view = View.of(events)
    last = view.messages[-1].content
    assert "<current-objective>" in last
    assert "ship the page" in last
    assert "1/3 done" in last
    assert "✓ 1. scaffold" in last
    assert "□ 2. style" in last
    assert "Next incomplete step: 2" in last


def test_no_recitation_without_a_plan():
    events = _seq(
        [
            ObservationEvent(
                tool_result=ToolResult(call_id="c", tool_name="shell", success=True, content="x"),
                action_id="a",
            )
        ]
    )
    view = View.of(events)
    assert not any("<current-objective>" in m.content for m in view.messages)
