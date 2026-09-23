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
    """The cost/time envelope a Deep Research run runs under.

    The research agent owns coverage and chooses its pivots.  The effort
    minimums are host-side guards; they do not create fixed subquestions.
    """

    max_sources: int  # one-execution cap: unique web passages; uploads are excluded
    # Compatibility-only fields retained while callers migrate off the retired
    # planner vocabulary.  The agent never reads these values.
    max_rounds_per_subq: int
    # One-execution cap on MODEL RESEARCH TURNS. The budget currency is work,
    # not wall-clock seconds: this platform's models decode at 20-45 tok/s and
    # legitimately think for minutes, so a seconds budget would price hardware,
    # not waste. Nothing in the research loop is bounded by time.
    max_research_turns: int
    max_subquestions: int
    # Search results KEPT from the one search each query gets. The engine issues
    # the model's query once and keeps this many of what came back; a SearXNG
    # call returns roughly 30 rows, so this is the tier's breadth dial. It used
    # to be small because `deep` searched four paraphrases of the same question
    # and fused them — paying four provider calls for breadth one call already
    # had. Extraction is a separate, smaller budget (`extract_cap`): a wider
    # discover_limit gives the ranker more to choose from, it does not fetch
    # more pages.
    discover_limit: int
    extract_cap: int  # max extractions per round (controls page fetching)
    rerank_top_k: int  # passages kept per round after rerank
    # Scales the prompt's depth guidance (`agent._depth_directive`). It no
    # longer selects a query-transformation strategy: every tier issues the
    # model's query as written, once.
    retrieval_depth: Literal["shallow", "standard", "deep"] = "standard"
    # The writer's per-call OUTPUT ceiling, in report-body tokens (the think
    # headroom rides on top; `writer._writer_max_tokens`). It is a transport
    # provision, never a target: no tier assigns a word floor or a word range,
    # and the model is never told this number. A report is as long as the
    # evidence and the question make it. The tiers differ in RESEARCH — more
    # turns, more sources — which is what "exhaustive" means; the 8,000-word
    # floor this field used to sit beside produced the padded, repetitive
    # reports the phase-6 blind eval scored at the bottom on reads/flows.
    writing_budget_tokens: int = 0
    # Characters of the evidence pool the writer is shown. An exhaustive run
    # admits two million characters across twenty-five sources; a pool budget
    # sized for eight of them divides into shares too small to carry a source's
    # notes, and E2's decisive "projected 2030" note was dropped by exactly
    # that arithmetic. The ceiling is the model window, not preference: see
    # `_writer_evidence` for the measured chars-per-token this is derived from.
    evidence_char_budget: int = 220_000
    min_evidence_passages: int = 0
    min_evidence_themes: int = 0
    min_evidence_sources: int = 0
    initial_probe_count: int = 0
    minimum_research_turns: int = 1
    minimum_useful_sources: int = 1
    review_decisions: int = 4  # two initial decisions plus two for post-repair verification

    @property
    def report_spec(self) -> dict[str, int]:
        """Stable integration shape for synthesis/report assembly."""
        return {"reserved_writing_tokens": self.writing_budget_tokens}

    @property
    def evidence_capacity_spec(self) -> dict[str, int]:
        """Compatibility view for callers migrating off the retired planner."""
        return {
            "min_evidence_passages": self.min_evidence_passages,
            "min_evidence_themes": self.min_evidence_themes,
            "min_evidence_sources": self.min_evidence_sources,
        }


_TIERS: dict[DepthTier, DepthBound] = {
    # Quick: fast verification runs. 4 starting probes (up to 6 total), 2
    # rounds each. Suitable for "what's the consensus on X" — small enough to
    # be near-realtime but with multi-section synthesis. Research is bounded at
    # 8 model turns; the three tiers' budgets are distinct and ordered
    # (quick < standard < exhaustive).
    DepthTier.QUICK: DepthBound(
        max_sources=30,
        max_rounds_per_subq=2,
        max_research_turns=8,
        max_subquestions=6,
        discover_limit=8,
        extract_cap=4,
        rerank_top_k=4,
        retrieval_depth="shallow",
        writing_budget_tokens=6_000,
        min_evidence_passages=8,
        min_evidence_themes=3,
        min_evidence_sources=5,
        initial_probe_count=4,
        minimum_research_turns=2,
        minimum_useful_sources=6,
    ),
    # Standard-deep: the everyday Deep Research run. 7 starting probes (up to
    # 16 total after adaptive expansion) × up to 4 rounds; up to 90 admitted
    # passages. The default tier.
    DepthTier.STANDARD_DEEP: DepthBound(
        max_sources=90,
        max_rounds_per_subq=4,
        max_research_turns=16,
        max_subquestions=16,
        discover_limit=20,
        extract_cap=6,
        rerank_top_k=6,
        retrieval_depth="standard",
        writing_budget_tokens=14_000,
        review_decisions=4,
        min_evidence_passages=20,
        min_evidence_themes=5,
        min_evidence_sources=12,
        initial_probe_count=7,
        minimum_research_turns=4,
        minimum_useful_sources=12,
    ),
    # Exhaustive: long-form survey. 10 starting probes (up to 32 total after
    # adaptive expansion) × up to 6 rounds; up to 240 admitted passages.
    DepthTier.EXHAUSTIVE: DepthBound(
        max_sources=240,
        max_rounds_per_subq=6,
        max_research_turns=32,
        max_subquestions=32,
        discover_limit=32,
        extract_cap=8,
        rerank_top_k=8,
        retrieval_depth="deep",
        writing_budget_tokens=24_000,
        evidence_char_budget=260_000,
        review_decisions=5,
        min_evidence_passages=40,
        min_evidence_themes=8,
        min_evidence_sources=24,
        initial_probe_count=10,
        minimum_research_turns=6,
        minimum_useful_sources=20,
    ),
}


def bounds_for(tier: DepthTier | str) -> DepthBound:
    """The DepthBound for a tier. Accepts the enum OR its string value so
    the agent-server can resolve from a plain string on the request body."""
    if isinstance(tier, str):
        tier = DepthTier(tier)
    return _TIERS[tier]
