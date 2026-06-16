"""Depth tiers — the bound the run runs under.

Three tiers mirror the Gemini/Perplexity moderate/max controls. Each bound is a
*hard cap*; when any cap is hit, the run produces a partial-but-honest report
with `bounded_by` set, never hangs. The tier choice is the user's lever (more
sources/depth = more inference time on local models).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class DepthTier(str, Enum):
    """The three Deep Research tiers. Mirrored by the UI's tier selector
    (set at submit time). Values are wire-stable (the agent-server stores
    them as strings on the conversation runtime)."""

    QUICK = "quick"
    STANDARD_DEEP = "standard_deep"
    EXHAUSTIVE = "exhaustive"


@dataclass(frozen=True)
class DepthBound:
    """The cost/time envelope a Deep Research run runs under. All caps are
    enforced at the engine layer; the first cap hit terminates the run and
    populates `bounded_by` on the ReportEvent so the user sees what stopped it
    (not silent truncation). max_subquestions also bounds the plan width — a
    too-wide decompose returns truncated steps with the same `bounded_by` mark."""

    max_sources: int  # whole-run cap: passages accumulated in the corpus
    max_rounds_per_subq: int  # retrieve-reason-refine round budget per sub-q
    max_wall_clock_s: int  # whole-run wall clock cap
    max_subquestions: int  # plan width cap
    discover_limit: int  # search results requested per query (per round)
    extract_cap: int  # max extractions per round (controls page fetching)
    rerank_top_k: int  # passages kept per round after rerank


_TIERS: dict[DepthTier, DepthBound] = {
    # Quick: fast verification runs, ~90s on local Qwen. ~3 sub-questions, 1
    # round each. Suitable for "what's the consensus on X" — small enough to be
    # near-realtime but with multi-section synthesis.
    DepthTier.QUICK: DepthBound(
        max_sources=10,
        max_rounds_per_subq=1,
        max_wall_clock_s=600,
        max_subquestions=3,
        discover_limit=8,
        extract_cap=4,
        rerank_top_k=4,
    ),
    # Standard-deep: the everyday Deep Research run. ~7-9 minutes on local Qwen
    # when the auxiliary roles are routed to a light model; longer when those
    # roles share Qwen (the conservative case). 6 sub-questions × up to 4
    # rounds. ~40 sources accumulated. The default. Bound raised in the tuning
    # pass so a moderate plan COMPLETES (covers all 6 sub-questions) — the
    # prior 300s cap consistently cut off at sub-question 3, exactly when the
    # prioritized decomposition would have started covering background.
    DepthTier.STANDARD_DEEP: DepthBound(
        max_sources=40,
        max_rounds_per_subq=4,
        max_wall_clock_s=600,
        max_subquestions=6,
        discover_limit=10,
        extract_cap=6,
        rerank_top_k=6,
    ),
    # Exhaustive: long-form survey, 15-30+ minutes on local Qwen. 12 sub-
    # questions × up to 5 rounds. ~150 sources. Map-reduce earns its place.
    DepthTier.EXHAUSTIVE: DepthBound(
        max_sources=150,
        max_rounds_per_subq=5,
        max_wall_clock_s=1800,
        max_subquestions=12,
        discover_limit=12,
        extract_cap=8,
        rerank_top_k=8,
    ),
}


def bounds_for(tier: DepthTier | str) -> DepthBound:
    """The DepthBound for a tier. Accepts the enum OR its string value so
    the agent-server can resolve from a plain string on the request body."""
    if isinstance(tier, str):
        tier = DepthTier(tier)
    return _TIERS[tier]
