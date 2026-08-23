"""Depth tiers — the bound the run runs under.

Three tiers mirror the Gemini/Perplexity moderate/max controls. Each bound is a
*hard cap*; when any cap is hit, the run produces a partial-but-honest report
with `bounded_by` set, never hangs. The tier choice is the user's lever (more
sources/depth = more inference time on local models).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal


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

    max_sources: int  # one-execution cap: unique web passages; uploads are excluded
    max_rounds_per_subq: int  # retrieve-reason-refine round budget per sub-q
    max_wall_clock_s: int  # whole-run wall clock cap
    max_subquestions: int  # plan width cap
    discover_limit: int  # search results requested per query (per round)
    extract_cap: int  # max extractions per round (controls page fetching)
    rerank_top_k: int  # passages kept per round after rerank
    retrieval_depth: Literal["shallow", "standard", "deep"] = "standard"
    # Report-writing targets are a separate envelope from research admission.
    # Retrieval may stop at its source/round caps; it must not consume this
    # reserved writing capacity or cause padding when evidence is narrow.
    report_min_words: int = 0
    report_max_words: int = 0
    writing_budget_tokens: int = 0
    # Evidence-capacity thresholds are deliberately separate from source and
    # sub-question caps.  They tell the controller when it has enough *shape*
    # to sustain the requested report, not when a fixed number of probes ran.
    min_evidence_passages: int = 0
    min_evidence_themes: int = 0
    min_evidence_sources: int = 0
    initial_probe_count: int = 3

    @property
    def report_spec(self) -> dict[str, int]:
        """Stable integration shape for synthesis/report assembly."""
        return {
            "min_words": self.report_min_words,
            "max_words": self.report_max_words,
            "reserved_writing_tokens": self.writing_budget_tokens,
        }

    @property
    def evidence_capacity_spec(self) -> dict[str, int]:
        """Thresholds used by adaptive gathering, separate from writing."""
        return {
            "min_evidence_passages": self.min_evidence_passages,
            "min_evidence_themes": self.min_evidence_themes,
            "min_evidence_sources": self.min_evidence_sources,
        }


_TIERS: dict[DepthTier, DepthBound] = {
    # Quick: fast verification runs. 4 starting probes (up to 6 total), 2
    # rounds each. Suitable for "what's the consensus on X" — small enough to
    # be near-realtime but with multi-section synthesis. The research wall
    # clock is 360s (minus the writing reserve); the three tiers' budgets are
    # distinct and ordered (quick < standard < exhaustive).
    DepthTier.QUICK: DepthBound(
        max_sources=30,
        max_rounds_per_subq=2,
        max_wall_clock_s=360,
        max_subquestions=6,
        discover_limit=8,
        extract_cap=4,
        rerank_top_k=4,
        retrieval_depth="shallow",
        report_min_words=1500,
        report_max_words=2500,
        writing_budget_tokens=4000,
        min_evidence_passages=8,
        min_evidence_themes=3,
        min_evidence_sources=5,
        initial_probe_count=4,
    ),
    # Standard-deep: the everyday Deep Research run. 7 starting probes (up to
    # 16 total after adaptive expansion) × up to 4 rounds; up to 90 admitted
    # passages. The default tier.
    DepthTier.STANDARD_DEEP: DepthBound(
        max_sources=90,
        max_rounds_per_subq=4,
        max_wall_clock_s=900,
        max_subquestions=16,
        discover_limit=10,
        extract_cap=6,
        rerank_top_k=6,
        retrieval_depth="standard",
        report_min_words=4000,
        report_max_words=7000,
        writing_budget_tokens=11200,
        min_evidence_passages=20,
        min_evidence_themes=5,
        min_evidence_sources=12,
        initial_probe_count=7,
    ),
    # Exhaustive: long-form survey. 10 starting probes (up to 32 total after
    # adaptive expansion) × up to 6 rounds; up to 240 admitted passages.
    DepthTier.EXHAUSTIVE: DepthBound(
        max_sources=240,
        max_rounds_per_subq=6,
        max_wall_clock_s=2400,
        max_subquestions=32,
        discover_limit=12,
        extract_cap=8,
        rerank_top_k=8,
        retrieval_depth="deep",
        report_min_words=8000,
        report_max_words=12000,
        writing_budget_tokens=19200,
        min_evidence_passages=40,
        min_evidence_themes=8,
        min_evidence_sources=24,
        initial_probe_count=10,
    ),
}


def bounds_for(tier: DepthTier | str) -> DepthBound:
    """The DepthBound for a tier. Accepts the enum OR its string value so
    the agent-server can resolve from a plain string on the request body."""
    if isinstance(tier, str):
        tier = DepthTier(tier)
    return _TIERS[tier]
