"""DeepResearchRun — the orchestrator wiring the research agent to the writer.

v2 (PKG-35): research is an agentic loop (`agent.run_research_agent`) — the
lead model sees the evidence map every turn and decides searches, pivots, and
sufficiency; the system enforces only hard budgets. Writing is one
whole-report pass with a fixed-rubric review (`writer.write_report`). There
is no plan gate: research starts immediately and the model's brief is the
first streamed output.

The run has exactly three outcomes: a complete report, an exception surfaced
by the agent-server as an ErrorEvent + ERROR status, or a Stop checkpoint
(`bounded_by="stopped"`, no report prose) the user can resume.
Hard research bounds are surfaced honestly via `bounded_by`; the writing
phase is bounded by its own bounded retries, never a shrinking deadline.
Progress events flow through the injected `emit` callback so the agent-server
appends them as ActionEvents / ObservationEvents.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from disco.core import ReportEvent, ReportSection, ResearchCheckpointEvent
from disco.core.llm import LLMRouter

from ..engine import RetrievalEngine
from ..models import Passage as RetrievalPassage
from ..ranking import Embedder
from ..vectorstore import VectorStore
from ._engine_parts._report import collect_report_data
from .agent import ResearchOutcome, run_research_agent
from .depth import DepthBound, DepthTier, bounds_for
from .writer import WrittenReport, write_report

_LOG = logging.getLogger(__name__)

# Callable types for mid-run steer / inject hooks (D3).
# Both default to None in every public-API call → the OFF-path is byte-identical
# to a run without hooks installed.
PopSteersFn = Callable[[], list[str]] | None
PopInjectedSourcesFn = Callable[[], list[RetrievalPassage]] | None

# Same EmitFn shape across the submodule. The agent-server installs a callback
# that takes (event_kind: str, payload: dict) and appends an ActionEvent or
# ObservationEvent to the conversation log so the UI sees progress live.
EmitFn = Callable[[str, dict[str, Any]], Awaitable[None]]


class ReportFromRun:
    """A small assembled-result transport used between the run and the
    agent-server. The agent-server turns this into a ReportEvent it appends
    to the conversation log. Kept separate from the Core ReportEvent so the
    engine can carry retrieval-typed Passages without leaking the model into
    core's event shape."""

    def __init__(
        self,
        *,
        query: str,
        summary: str,
        sections: list[ReportSection],
        cited_passages: list[RetrievalPassage],
        reviewed_passages: list[RetrievalPassage],
        all_hits: list[Any],
        unsupported_count: int,
        bounded_by: str | None,
        depth_tier: str,
        claims: list[dict[str, Any]] | None = None,
        completed_probes: list[str] | None = None,
        pending_probes: list[str] | None = None,
        review_notes: list[str] | None = None,
        research_trail: list[dict[str, Any]] | None = None,
        recency_window: Literal["month", "week"] | None = None,
    ) -> None:
        self.query = query
        self.summary = summary
        self.sections = sections
        self.cited_passages = cited_passages
        self.reviewed_passages = reviewed_passages
        self.all_hits = all_hits
        self.unsupported_count = unsupported_count
        self.bounded_by = bounded_by
        self.depth_tier = depth_tier
        self.claims = list(claims or [])
        self.completed_probes = list(completed_probes or [])
        self.pending_probes = list(pending_probes or [])
        # Residual fixed-rubric misses from the writer's review loop —
        # metadata only, never prose edits (decision #4).
        self.review_notes = list(review_notes or [])
        self.research_trail = list(research_trail or [])
        self.recency_window: Literal["month", "week"] | None = recency_window
        # Synthesis owns how much of this envelope the evidence can support;
        # retrieval never borrows it.  The concrete tier values are available
        # from ``DeepResearchRun.bound.report_spec`` and this transport carries
        # the same shape for downstream report assembly.
        self.report_spec: dict[str, int] | None = None

    def to_event(self) -> ReportEvent | ResearchCheckpointEvent:
        """Convert into the Core ReportEvent for persistence on the log.
        Retrieval-typed Passage/SearchHit are dumped to plain dicts so `core`
        stays free of any `retrieval` import."""
        from ._report_event import fit_report_event, fit_research_checkpoint_event

        if self.bounded_by == "stopped":
            checkpoint = ResearchCheckpointEvent(
                query=self.query,
                passages=[
                    p.model_dump()
                    for p in [*self.cited_passages, *self.reviewed_passages]
                ],
                all_hits=[h.model_dump() for h in self.all_hits],
                trail=self.research_trail,
                completed_queries=self.completed_probes,
                depth_tier=self.depth_tier,
                recency_window=self.recency_window,
            )
            return fit_research_checkpoint_event(checkpoint)

        event = ReportEvent(
            query=self.query,
            summary=self.summary,
            sections=self.sections,
            passages=[p.model_dump() for p in self.cited_passages],
            reviewed_passages=[p.model_dump() for p in self.reviewed_passages],
            all_hits=[h.model_dump() for h in self.all_hits],
            claims=self.claims,
            unsupported_count=self.unsupported_count,
            bounded_by=self.bounded_by,
            depth_tier=self.depth_tier,
            completed_probes=self.completed_probes,
            pending_probes=self.pending_probes,
        )
        if self.review_notes:
            event = event.model_copy(
                update={"meta": {**event.meta, "review_notes": list(self.review_notes)}}
            )
        return fit_report_event(event)


def _trail_queries(trail: list[dict[str, Any]]) -> list[str]:
    """The deduped, ordered queries the agent issued — the checkpoint's
    `completed_probes`, which feed `resume_trail` on resume."""
    queries = [
        str(entry["query"])
        for entry in trail
        if entry.get("kind") == "search" and isinstance(entry.get("query"), str)
    ]
    return list(dict.fromkeys(queries))


class DeepResearchRun:
    """One Deep Research run, end-to-end. Constructed per conversation by the
    agent-server runtime with all dependencies injected. Single-shot:
    `await run.run()` produces the assembled ReportFromRun."""

    def __init__(
        self,
        *,
        query: str,
        router: LLMRouter,
        retrieval_engine: RetrievalEngine,
        embedder: Embedder | None,
        vector_store: VectorStore,
        nli: Any,
        depth: DepthTier | str = DepthTier.STANDARD_DEEP,
        conversation_id: str = "deep_research",
        gather_concurrency: int | None = None,
        recency_window: Literal["month", "week"] | None = None,
        upload_passages: list[Any] | None = None,
        corpus_ids: frozenset[str] = frozenset(),
    ) -> None:
        self._query = query
        self._router = router
        self._engine = retrieval_engine
        # The v2 agent loop retrieves per-query and the writer reads the full
        # pool directly; embedder/vector_store/gather_concurrency stay on the
        # constructor for wiring compatibility with the agent-server runtime.
        self._embedder = embedder
        self._vector_store = vector_store
        self._gather_concurrency = gather_concurrency
        self._nli = nli
        self._bound: DepthBound = bounds_for(depth)
        self._depth = depth if isinstance(depth, str) else depth.value
        self._namespace = conversation_id
        self._recency_window: Literal["month", "week"] | None = recency_window
        # G1/DR-4 F2: pre-attached upload passages seed the evidence pool.
        # None / [] → OFF path.
        self._upload_passages: list[Any] = upload_passages or []
        # Spaces: durable named corpora to include in every RetrievalRequest.
        self._corpus_ids = corpus_ids

    @property
    def bound(self) -> DepthBound:
        return self._bound

    async def run(
        self,
        plan_steps: list[str] | None = None,
        *,
        emit: EmitFn,
        should_cancel: Callable[[], bool] | None = None,
        resume_sections: list[ReportSection] | None = None,
        resume_passages: list[RetrievalPassage] | None = None,
        resume_all_hits: list[Any] | None = None,
        resume_completed_probes: list[str] | None = None,
        resume_pending_probes: list[str] | None = None,
        resume_research_trail: list[dict[str, Any]] | None = None,
        pop_steers: PopSteersFn = None,
        pop_injected_sources: PopInjectedSourcesFn = None,
    ) -> ReportFromRun:
        """Execute the run. Returns the assembled report. `emit` is awaited
        between phases so the agent-server can write events to the
        conversation log; we never write to the store directly.

        `plan_steps` is a legacy input from the retired plan-approval flow:
        accepted for call-site compatibility, ignored beyond logging — the
        agent decides its own searches (decision #7). `should_cancel` makes
        Stop REAL: polled at every turn boundary; when it returns true the
        run halts and returns a CHECKPOINT (`bounded_by="stopped"`, no report
        prose) the agent-server persists as a resumable PAUSED state.

        Resume (checkpointed): the prior stopped run's evidence
        (`resume_passages` / `resume_all_hits`) seeds the pool, and its
        `resume_completed_probes` seed the audit trail; the report is written
        fresh from the combined pool. `resume_sections` /
        `resume_pending_probes` are accepted for signature compatibility (v2
        recompiles the whole report from evidence, never from prior prose).

        D3 mid-run steer / inject: `pop_steers` strings become priority
        guidance in the agent's next turn; `pop_injected_sources` passages
        are admitted directly to the pool. Both default None → OFF-path."""
        if plan_steps:
            _LOG.debug("ignoring %d legacy plan steps (agentic v2 loop)", len(plan_steps))
        if resume_sections or resume_pending_probes:
            _LOG.debug("resume carries evidence forward; prior sections are recompiled")
        resume_trail = list(resume_research_trail or [])
        if not resume_trail and resume_completed_probes:
            resume_trail = [
                {"kind": "search", "query": query, "resumed": True}
                for query in resume_completed_probes
            ]
        outcome = await run_research_agent(
            self._query,
            router=self._router,
            retrieval_engine=self._engine,
            bound=self._bound,
            namespace=self._namespace,
            emit=emit,
            should_cancel=should_cancel,
            pop_steers=pop_steers,
            pop_injected_sources=pop_injected_sources,
            recency_window=self._recency_window,
            corpus_ids=self._corpus_ids,
            upload_passages=list(self._upload_passages) or None,
            resume_passages=resume_passages,
            resume_trail=resume_trail or None,
        )
        carried_hits: list[Any] = list(resume_all_hits or [])
        if outcome.bounded_by == "stopped":
            return self._assemble_checkpoint(outcome, carried_hits)
        written = await write_report(
            self._query,
            outcome,
            router=self._router,
            nli=self._nli,
            bound=self._bound,
            emit=emit,
            recency_window=self._recency_window,
            conversation_id=self._namespace,
        )
        return self._assemble_report(outcome, written, carried_hits)

    def _assemble_checkpoint(
        self, outcome: ResearchOutcome, carried_hits: list[Any]
    ) -> ReportFromRun:
        """A Stop checkpoint, not a report: keep the gathered pool (as
        reviewed passages) and the audit trail (as completed probes for
        resume), write no prose."""
        _cited, reviewed, all_hits = collect_report_data(
            [], outcome.passages, carried_hits, outcome.all_hits
        )
        report = ReportFromRun(
            query=self._query,
            summary="",
            sections=[],
            cited_passages=[],
            reviewed_passages=reviewed,
            all_hits=all_hits,
            unsupported_count=0,
            bounded_by="stopped",
            depth_tier=self._depth,
            claims=[],
            completed_probes=_trail_queries(outcome.trail),
            pending_probes=[],
            research_trail=outcome.trail,
            recency_window=self._recency_window,
        )
        report.report_spec = self._bound.report_spec
        return report

    def _assemble_report(
        self, outcome: ResearchOutcome, written: WrittenReport, carried_hits: list[Any]
    ) -> ReportFromRun:
        """Assemble the final report: split the pool into cited vs reviewed
        against the written sections, dedupe hits (carried first so a resumed
        run's citations resolve), and carry the writer's ledger + residual
        review notes."""
        cited, reviewed, all_hits = collect_report_data(
            written.sections,
            outcome.passages,
            carried_hits,
            outcome.all_hits,
            additional_cited_ids=written.summary_cited_passage_ids,
        )
        report = ReportFromRun(
            query=self._query,
            summary=written.summary,
            sections=written.sections,
            cited_passages=cited,
            reviewed_passages=reviewed,
            all_hits=all_hits,
            unsupported_count=written.unsupported_count,
            bounded_by=outcome.bounded_by,
            depth_tier=self._depth,
            claims=written.claims,
            completed_probes=_trail_queries(outcome.trail),
            pending_probes=[],
            review_notes=written.review_notes,
        )
        report.report_spec = self._bound.report_spec
        return report
