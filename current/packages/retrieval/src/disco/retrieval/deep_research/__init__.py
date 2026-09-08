"""Deep Research — the long-horizon research subsystem.

v2 (PKG-35): an **agentic research loop → one whole-report writer** arc. The
lead model directs research itself (`agent.py`) — it sees the evidence map
every turn, decides searches, pivots, and sufficiency, and works under hard
budgets only (live countdown surfaced every turn). Writing is one
whole-report pass, one review against a fixed rubric, and one rework of the
parts the review named (`writer.py`); the system never edits the prose — what
it could not verify ships declared beside the report. A run either produces
that report, raises
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
- `write_pool` / `read_pool` / `load_pool` (`pool.py`) — the saved evidence
  pool: everything `write_report` reads, untruncated and in pool order, so a
  writer change can be replayed against a run that already happened.
- `model_activity_events(emit)` — wrap a run to add the model-call heartbeat.
  The loop cannot see provider chunks; the stream layer can, and this
  publishes what it saw through the same `emit`.
- `STOP_REQUESTED_ACTION` / `stop_requested_payload()` — the non-terminal
  marker the agent-server appends when the user presses Stop on a live run.
  The engine cannot emit it (the flag is set from outside the run), so the
  name and the payload are declared here and used there.
"""

from __future__ import annotations

from ._progress_events import (
    RESEARCH_POOL_ACTION,
    RESEARCH_TRACE_ACTIONS,
    STOP_REQUESTED_ACTION,
    model_activity_events,
    stop_requested_payload,
)
from .decompose import SubQuestion, decompose_query
from .depth import DepthBound, DepthTier, bounds_for
from .engine import DeepResearchRun, ReportFromRun
from .pool import (
    POOL_SCHEMA_VERSION,
    ResearchPoolError,
    SavedPool,
    load_pool,
    pool_dir,
    pool_path,
    read_pool,
    write_pool,
)

__all__ = [
    "POOL_SCHEMA_VERSION",
    "RESEARCH_POOL_ACTION",
    "RESEARCH_TRACE_ACTIONS",
    "STOP_REQUESTED_ACTION",
    "DeepResearchRun",
    "DepthBound",
    "DepthTier",
    "ReportFromRun",
    "ResearchPoolError",
    "SavedPool",
    "SubQuestion",
    "bounds_for",
    "decompose_query",
    "load_pool",
    "model_activity_events",
    "pool_dir",
    "pool_path",
    "read_pool",
    "stop_requested_payload",
    "write_pool",
]
