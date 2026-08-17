"""Plan-bounding + gather-task dispatch, extracted from
`DeepResearchRun._prepare_plan` / `._start_gather_tasks` / `._start_one_steer_task`.

Every function here needs the live `DeepResearchRun` and takes it as an
explicit `run` parameter (never a method) — see `_engine_parts/__init__.py`.

Calls `gather_for_subquestion` through `engine.gather_for_subquestion(...)`
(module-attribute lookup at call time) rather than importing it directly:
`current/packages/retrieval/tests/test_pipeline_invariants.py` and
`current/packages/agent-server/tests/test_upload_corpus.py` monkeypatch
`disco.retrieval.deep_research.engine.gather_for_subquestion`, and a direct
`from ..gather import gather_for_subquestion` binding here would capture the
pre-patch function object and silently defeat that patch.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import TYPE_CHECKING, Any

from disco.core import ReportSection
from disco.core.llm import CallContext

from ...models import Passage as RetrievalPassage
from .. import engine
from ..decompose import SubQuestion
from ..gather import GatherLegContext, SubQuestionResult

if TYPE_CHECKING:
    from ..engine import DeepResearchRun, EmitFn

GatherTask = tuple[SubQuestion, "asyncio.Task[SubQuestionResult]", str, str, GatherLegContext]


async def prepare_plan(
    run: DeepResearchRun,
    plan_steps: list[str],
    *,
    resume_sections: list[ReportSection] | None,
    resume_passages: list[RetrievalPassage] | None,
    resume_all_hits: list[Any] | None,
    emit: EmitFn,
) -> tuple[
    str | None,
    list[SubQuestion],
    list[ReportSection],
    list[RetrievalPassage],
    list[Any],
]:
    """Bound the plan width to the depth tier, carry forward already-completed
    (resumed) sections, and emit the gather-phase + low-RAM transparency
    events. Returns `(bounded_by, pending, sections, carried_passages,
    carried_hits)` — the explicit state the rest of the run threads through."""
    bounded_by: str | None = None
    # honor the depth bound on plan width too
    if len(plan_steps) > run._bound.max_subquestions:
        plan_steps = plan_steps[: run._bound.max_subquestions]
        bounded_by = "subquestions"

    # Carry forward already-completed sections (resume). Match by title — the
    # plan step titles ARE the section titles, so a section present means that
    # sub-question is done and must not be re-gathered.
    sections: list[ReportSection] = list(resume_sections or [])
    done_titles = {s.title for s in sections}
    carried_passages: list[RetrievalPassage] = list(resume_passages or [])
    carried_hits: list[Any] = list(resume_all_hits or [])
    pending = [SubQuestion(title=t) for t in plan_steps if t not in done_titles]
    run._scheduled_subquestions = len(sections) + len(pending)
    # Graceful low-RAM transparency (#26): the gather concurrency was derived
    # from available RAM. Surface it — and whether it's actually *constraining*
    # parallelism (cap < legs) — so a memory-reduced run is VISIBLE, never a
    # silent degradation. `concurrency=None` means unbounded (big box / remote).
    memory_bounded = run._gather_concurrency is not None and run._gather_concurrency < len(pending)
    await emit(
        "phase",
        {
            "phase": "gather",
            "subquestions": len(pending),
            "resumed_sections": len(sections),
            "concurrency": run._gather_concurrency,
            "memory_bounded": memory_bounded,
        },
    )
    if memory_bounded:
        await emit(
            "observation",
            {
                "subquestion": None,
                "ok": True,
                "detail": (
                    f"running {run._gather_concurrency} of {len(pending)} "
                    f"research legs at a time to stay within the available "
                    f"memory budget (reduced parallelism, not reduced coverage)"
                ),
            },
        )
    return bounded_by, pending, sections, carried_passages, carried_hits


def _build_leg_context(
    run: DeepResearchRun, subq: SubQuestion
) -> tuple[str, str, GatherLegContext]:
    """Race Fix: section_id and namespace collision defense. Stability for
    resume. Per-leg ISOLATED sub-context (C14): each leg gets its OWN
    subq_id / namespace / call_context — see `GatherLegContext`'s docstring
    for the full isolation contract. Returns `(subq_id, subq_namespace,
    leg_context)`."""
    subq_hash = hashlib.sha256(subq.title.encode()).hexdigest()[:8]
    subq_namespace = f"{run._namespace}/{subq_hash}"
    subq_id = f"s{subq_hash}"
    leg_call_context = CallContext(conversation_id=f"{run._namespace}/{subq_id}")
    leg_context = GatherLegContext(
        subq_id=subq_id,
        namespace=subq_namespace,
        call_context=leg_call_context,
    )
    return subq_id, subq_namespace, leg_context


def start_gather_tasks(
    run: DeepResearchRun,
    pending: list[SubQuestion],
    *,
    emit: EmitFn,
) -> list[GatherTask]:
    """RP-04 producer: partition the source budget across the pending
    sub-questions and start every gather leg concurrently. When
    `run._gather_concurrency` is set (bundled in-process-encoder tier) a
    Semaphore caps how many legs run the heavy body at once — peak-memory
    guard; `None` ⇒ fully concurrent. All tasks are created up-front so the
    consumer can drain them in order. Returns the ordered task list, each
    entry `(subq, task, subq_id, subq_namespace, leg_context)`."""
    # The source budget governs unique web passages gathered by this execution.
    # A resumed invocation gets a fresh allowance because reports persist only
    # their cited subset, not enough information to reconstruct prior discovery.
    remaining = run._bound.max_sources

    # RP-04: Pipeline restructure.
    # 1. Partition budget upfront (Race Fix).
    n_pending = len(pending)
    budget_per = remaining // n_pending if n_pending > 0 else 0
    extra_budget = remaining % n_pending if n_pending > 0 else 0

    # 2. Start all GATHER tasks concurrently (Producer).
    # Retrieval work (search, fetch, extract, embed, rerank) runs concurrently.
    #
    # BUNDLED-tier peak-memory guard: when `run._gather_concurrency` is set
    # (in-process-encoder tier only), a Semaphore caps how many legs run the
    # heavy body AT ONCE. All tasks are still created up-front so the
    # consumer below can drain them in order; the ones past the cap simply
    # block on `sem.acquire()` until a slot frees — bounding peak working
    # set to ~K legs instead of all `n_pending`. `None` ⇒ no semaphore ⇒
    # fully concurrent (paid / self-host / remote-encoder tier — unchanged).
    leg_sem = (
        asyncio.Semaphore(run._gather_concurrency) if run._gather_concurrency is not None else None
    )

    async def _gather_leg(_subq: SubQuestion, **kw: Any) -> SubQuestionResult:
        if leg_sem is not None:
            async with leg_sem:
                return await engine.gather_for_subquestion(_subq, **kw)
        return await engine.gather_for_subquestion(_subq, **kw)

    gather_tasks: list[GatherTask] = []
    for i, subq in enumerate(pending):
        subq_budget = budget_per + (1 if i < extra_budget else 0)
        subq_id, subq_namespace, leg_context = _build_leg_context(run, subq)

        task = asyncio.create_task(
            _gather_leg(
                subq,
                engine=run._engine,
                router=run._router,
                embedder=run._embedder,
                vector_store=run._vector_store,
                namespace=subq_namespace,
                bound=run._bound,
                emit=emit,
                remaining_source_budget=subq_budget,
                leg_context=leg_context,
                source_budget=run._source_budget,
                recency_window=run._recency_window,
                corpus_ids=run._corpus_ids,
                # G1/DR-4 F2: seed each leg with any pre-attached upload
                # passages so they are available during synthesis.
                extra_passages=list(run._upload_passages),
            )
        )
        gather_tasks.append((subq, task, subq_id, subq_namespace, leg_context))
    return gather_tasks


def start_one_steer_task(
    run: DeepResearchRun,
    subq: SubQuestion,
    source_budget: int,
    *,
    emit: EmitFn,
    extra_passages: list[Any] | None = None,
) -> GatherTask:
    """Start a single gather task for a mid-run steer sub-question.

    Uses the same leg-construction logic as `start_gather_tasks` but accepts
    an explicit `source_budget` so the caller can give the steer leg a
    proportional fraction of the total budget (rather than the full
    `max_sources` amount). The semaphore from the parent run is NOT
    re-installed here — steer legs are created sequentially at a section
    boundary, never in a burst, so peak-memory contention is the same as a
    single normal leg.

    `extra_passages` (A4.4) seeds the leg's corpus. It defaults to
    `run._upload_passages` (the D3 steer behaviour — unchanged), but the
    iterative-refine path passes a section's ORIGINAL cited passages instead
    so a re-search leg sees the COMBINED corpus (fresh evidence + the
    section's prior sources) when it re-synthesizes."""
    if extra_passages is None:
        extra_passages = list(run._upload_passages)
    subq_id, subq_namespace, leg_context = _build_leg_context(run, subq)

    async def _gather_leg(_subq: SubQuestion, **kw: Any) -> SubQuestionResult:
        return await engine.gather_for_subquestion(_subq, **kw)

    task = asyncio.create_task(
        _gather_leg(
            subq,
            engine=run._engine,
            router=run._router,
            embedder=run._embedder,
            vector_store=run._vector_store,
            namespace=subq_namespace,
            bound=run._bound,
            emit=emit,
            remaining_source_budget=source_budget,
            leg_context=leg_context,
            source_budget=run._source_budget,
            recency_window=run._recency_window,
            corpus_ids=run._corpus_ids,
            # G1/DR-4 F2: seed steer legs with upload passages by default;
            # the A4.4 refine path overrides this with a section's originals.
            extra_passages=extra_passages,
        )
    )
    return (subq, task, subq_id, subq_namespace, leg_context)
