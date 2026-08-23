"""Deep Research — the long-horizon research subsystem.

Composes the existing pieces (retrieval engine, grounding pipeline, NLI,
vector store, query rewriter) into the **plan → adaptive gathering →
evidence-led outline → per-section grounded synthesis → executive summary**
arc. The agent loop owns the plan-approval gate and event streaming; this
package owns the research process itself. A run either produces that report,
raises (surfaced as a run error), or checkpoints on user Stop — there is no
alternate report constructor.

Public API:
- `DepthTier`, `DepthBound`, `bounds_for(tier)` — the cost/time bound config.
- `decompose_query(router, query, max_subq)` — initial plan-step decomposition.
- `DeepResearchRun` — the orchestrator. Built by the agent-server runtime with
  injected providers + an `emit` callback so the loop's event log gets every
  Action / Observation / Plan / Report event as the run progresses.
"""

from __future__ import annotations

from .decompose import SubQuestion, decompose_query
from .depth import DepthBound, DepthTier, bounds_for
from .engine import DeepResearchRun, ReportFromRun

__all__ = [
    "DeepResearchRun",
    "DepthBound",
    "DepthTier",
    "ReportFromRun",
    "SubQuestion",
    "bounds_for",
    "decompose_query",
]
