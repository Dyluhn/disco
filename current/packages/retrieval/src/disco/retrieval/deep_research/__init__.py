"""Deep Research — the long-horizon research subsystem.

v2 (PKG-35): an **agentic research loop → one whole-report writer** arc. The
lead model directs research itself (`agent.py`) — it sees the evidence map
every turn, decides searches, pivots, and sufficiency, and works under hard
budgets only (live countdown surfaced every turn). Writing is one
whole-report pass reviewed against a fixed rubric with precise deficiency
feedback (`writer.py`); hard structure and grounding defects must be repaired
before the report can ship. A run either produces that report, raises
(surfaced as a run error), or checkpoints on user Stop — there is no alternate
report constructor and no plan-approval gate.

Public API:
- `DepthTier`, `DepthBound`, `bounds_for(tier)` — the cost/time bound config.
- `decompose_query(router, query, max_subq)` / `SubQuestion` — retained for
  the agent-server's plan flow (legacy surface; the v2 run ignores plan
  steps).
- `DeepResearchRun` — the orchestrator. Built by the agent-server runtime
  with injected providers + an `emit` callback so the loop's event log gets
  every Action / Observation / Report event as the run progresses.
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
