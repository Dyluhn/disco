"""Deep Research execution-phase collaborators.

Extracted from ``DeepResearchService._execute_deep_research``
(PKG-11-RETRIEVAL wave 1, PY-0192/PY-0197/PY-0198): the post-approval
driver's distinct concerns — dependency/space resolution, preflight, engine
assembly, event-encoding (the emit callback), checkpoint-resume rebuilding,
and the run/cleanup lifecycle — each get their own narrow collaborator here.
``run_execute`` is the sequencing of these steps, not a relocated copy of the
original body. Every step takes ``service`` and resolves collaborators off
it at call time, so instance-level test monkeypatches
(``monkeypatch.setattr(rt.deep_research, "_execute_deep_research", ...)``) keep
resolving correctly regardless of this module boundary.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any, Literal

from disco.core import (
    ActionEvent,
    ConversationStatus,
    ErrorEvent,
    Event,
    EventSource,
    MessageEvent,
    ObservationEvent,
    PlanEvent,
    ReportEvent,
    ToolCall,
    ToolResult,
)
from disco.core.llm import DefaultLLMRouter, ModelRole
from disco.retrieval import DefaultRetrievalEngine, InMemoryVectorStore, RouterQueryRewriter
from disco.retrieval.deep_research import DeepResearchRun, DepthTier, ReportFromRun
from disco.retrieval.deep_research.concurrency import gather_concurrency_for
from disco.retrieval.models import Passage, SearchHit

if TYPE_CHECKING:
    from ..deep_research_service import DeepResearchService

_LOG = logging.getLogger(__name__)


def resolve_query(events: list[Event], plan: PlanEvent) -> str:
    """The query is the user's last (pre-plan) message; the plan summary is
    the fallback for the (should-not-happen) case of no user message."""
    return next(
        (
            e.message.content
            for e in events
            if isinstance(e, MessageEvent) and e.source == EventSource.USER
        ),
        plan.summary,
    )


async def build_retrieval_deps(
    service: DeepResearchService, conversation_id: str
) -> tuple[dict[str, Any], frozenset[str], tuple[str, ...]]:
    """Resolve the research-source deps + validated space_ids for this run.

    Returns ``(deps, space_ids, forbidden_space_ids)`` — the caller logs and
    drops ``forbidden_space_ids`` itself, so the warning keeps coming from
    ``deep_research_service``'s own logger."""
    research_sources = service._settings.get_research_sources(conversation_id)
    search_override = service._search_override_for_sources(research_sources)
    deps = service._research(search_override=search_override)
    space_ids = service._spaces.get_space_ids(conversation_id)
    space_owner_id = service._store.conversation_owner_id_sync(conversation_id)
    space_ids, forbidden_space_ids = service._validated_space_ids(
        space_ids,
        owner_id=space_owner_id,
    )
    return deps, space_ids, forbidden_space_ids


async def run_preflight(
    service: DeepResearchService,
    conversation_id: str,
    deps: dict[str, Any],
    space_ids: frozenset[str],
) -> str | None:
    """W-35/W-33: pre-flight the driver + the REQUIRED encoders BEFORE the
    engine starts. A dead driver or a degraded/empty remote reranker/NLI
    would otherwise run a long, expensive job that silently produces a wrong
    report. P1-3: the DR engine synthesises/judges with RAG_ANSWERER — probe
    THAT role, not AGENT_DRIVER (which DR generation does not use)."""
    override = service._settings._get_model_override(conversation_id)
    required_encoders = ("reranker", "nli", "embedder") if space_ids else ("reranker", "nli")
    return await service._preflight.check(
        conversation_id, override=override, role=ModelRole.RAG_ANSWERER
    ) or await service._preflight_encoders(deps, required=required_encoders)


def build_router_and_engine(
    service: DeepResearchService, conversation_id: str, deps: dict[str, Any]
) -> tuple[DefaultLLMRouter, DefaultRetrievalEngine]:
    """RP-05b §3: deep research's discovery/extraction also flows MCP
    providers through the SAME engine, so deep-research citations can come
    from the MCP tier identically to bundled providers."""
    search, extraction = service._provider.compose_mcp_retrieval(deps)
    router = service._drivers.router(
        pick=service._settings._get_model_override(conversation_id)
    )
    retrieval_engine = DefaultRetrievalEngine(
        search=search,
        extraction=extraction,
        reranker=deps["reranker"],
        embedder=deps.get("embedder"),
        rewriter=RouterQueryRewriter(router),
        vector_store=service._spaces.space_vector_store(),
    )
    return router, retrieval_engine


def compute_gather_cap(service: DeepResearchService, plan_steps: list[str]) -> int | None:
    """RAM-aware peak-memory guard (OOM fix): bound how many gather legs run
    their fetch/extract/embed/rerank body at once. Applies on EVERY tier —
    not OOMing is correctness, not a free-tier perk."""
    in_process_encoders = service._provider.in_process_encoders()
    return gather_concurrency_for(
        in_process_encoders=in_process_encoders,
        n_subquestions=len(plan_steps),
        env=os.environ,
    )


def build_run(
    service: DeepResearchService,
    conversation_id: str,
    *,
    query: str,
    router: DefaultLLMRouter,
    retrieval_engine: DefaultRetrievalEngine,
    deps: dict[str, Any],
    tier: DepthTier,
    recency_window: Literal["month", "week"] | None,
    gather_cap: int | None,
    space_ids: frozenset[str],
) -> DeepResearchRun:
    # G1/DR-4 F2: load any pre-attached upload passages (text files the user
    # attached in the initial box before submitting). Empty when no text
    # files were uploaded (the OFF path — byte-identical to pre-DR-4 code).
    upload_passages = service.get_upload_passages(conversation_id)
    return DeepResearchRun(
        query=query,
        router=router,
        retrieval_engine=retrieval_engine,
        embedder=deps.get("embedder"),
        vector_store=InMemoryVectorStore(),
        nli=deps["nli"],
        depth=tier,
        conversation_id=conversation_id,
        gather_concurrency=gather_cap,
        recency_window=recency_window,
        # G1/DR-4 F2: seed every gather leg with upload passages.
        upload_passages=upload_passages or None,
        corpus_ids=space_ids,
        # A4.4: per-cid iterative-research toggle (default False → OFF path).
        iterative=service._iterative_for(conversation_id),
    )


def build_emit_callback(service: DeepResearchService, conversation_id: str):
    """Emit callback: every engine event becomes an Action/Observation pair
    on the conversation log so the UI's activity feed reflects progress.

    CORRELATION: gather runs sub-questions CONCURRENTLY, so "the last
    ActionEvent appended" is usually some OTHER sub-question's search by the
    time an observation arrives — which left 5 of 6 searches permanently
    "running" in the UI (the observation pointed at the wrong action). The
    engine's payloads already carry `subquestion` (+ `round` for search/
    observation), so we correlate per sub-question here, in this run-scoped
    map — no engine/protocol change."""
    action_by_key: dict[str, str] = {}

    def _corr_key(payload: dict[str, Any]) -> str | None:
        subq = payload.get("subquestion")
        if subq is None:
            return None
        rnd = payload.get("round")
        # gap_reason has no round → correlate to the sub-question's latest search.
        return f"{subq}|{rnd}" if rnd is not None else f"{subq}|latest"

    async def emit(kind: str, payload: dict[str, Any]) -> None:
        # Treat phase + search + synthesize_section as actions (the agent
        # "doing something"), observation kinds as observations (results).
        if kind == "observation" or kind == "gap_reason":
            key = _corr_key(payload)
            action_id = action_by_key.get(key or "") or action_by_key.get(
                f"{payload.get('subquestion')}|latest", ""
            )
            if not action_id:
                # No subquestion in the payload (or pre-correlation emit) —
                # fall back to the previous best-effort "last action".
                last_action = next(
                    (
                        e
                        for e in reversed(await service._store.get_events(conversation_id))
                        if isinstance(e, ActionEvent)
                    ),
                    None,
                )
                action_id = last_action.id if last_action else ""
            await service._store.append(
                conversation_id,
                ObservationEvent(
                    tool_result=ToolResult(
                        call_id=f"call_{kind}",
                        tool_name=kind,
                        success=bool(payload.get("ok", True)),
                        content=str({k: v for k, v in payload.items() if k != "ok"}),
                        structured=payload,
                    ),
                    action_id=action_id,
                ),
            )
            return
        # action-shaped events
        event = ActionEvent(
            thought=f"Deep Research: {kind}",
            tool_call=ToolCall(tool_name=kind, arguments=payload),
        )
        await service._store.append(conversation_id, event)
        key = _corr_key(payload)
        if key is not None:
            action_by_key[key] = event.id
            action_by_key[f"{payload.get('subquestion')}|latest"] = event.id

    return emit


def rebuild_resume_state(
    resume_from: ReportEvent | None,
) -> tuple[list[Any] | None, list[Passage] | None, list[SearchHit] | None]:
    """Checkpointed resume: rebuild the retrieval-typed passages/hits from
    the prior partial ReportEvent's plain dicts so the engine can carry its
    completed sections forward (and skip those sub-questions)."""
    if resume_from is None:
        return None, None, None
    resume_sections = list(resume_from.sections)
    resume_passages = [Passage.model_validate(p) for p in resume_from.passages]
    resume_all_hits = [SearchHit.model_validate(h) for h in resume_from.all_hits]
    return resume_sections, resume_passages, resume_all_hits


def build_steer_hooks(service: DeepResearchService, conversation_id: str):
    """D3: pop_* closures drain the per-cid steer/inject queues at each
    section boundary. The WS handler enqueues into these queues; the queues
    are removed in ``run_engine``'s ``finally`` so the presence of a key = "a
    DR run is currently in flight for this cid"."""

    def pop_steers() -> list[str]:
        return service._live_state.pop_steers(conversation_id)

    def pop_injected_sources() -> list[Passage]:
        return service._live_state.pop_injected_sources(conversation_id)

    return pop_steers, pop_injected_sources


async def run_engine(
    service: DeepResearchService,
    conversation_id: str,
    run: DeepResearchRun,
    *,
    plan_steps: list[str],
    resume_sections: list[Any] | None,
    resume_passages: list[Passage] | None,
    resume_all_hits: list[SearchHit] | None,
    emit: Any,
) -> ReportFromRun | None:
    """Run the engine and always clean up its per-run state, regardless of
    outcome. Returns the run result, or None on failure (an ErrorEvent has
    already been appended in that case)."""
    from ..runtime import _release_process_memory

    # Fresh cancel flag for this execution; the engine polls it at each
    # sub-question/section boundary so Stop actually halts the run.
    flag = service._cancellations.begin(conversation_id)

    # D3: initialise per-cid steer/inject queues on the research owner.
    service._live_state.begin(conversation_id)
    pop_steers, pop_injected_sources = build_steer_hooks(service, conversation_id)

    try:
        return await run.run(
            plan_steps,
            emit=emit,
            should_cancel=flag.is_set,
            resume_sections=resume_sections,
            resume_passages=resume_passages,
            resume_all_hits=resume_all_hits,
            pop_steers=pop_steers,
            pop_injected_sources=pop_injected_sources,
        )
    except Exception as exc:  # noqa: BLE001 — surface as ErrorEvent
        await service._store.append(
            conversation_id,
            ErrorEvent(
                code="deep_research_failed",
                detail=f"{type(exc).__name__}: {exc}",
            ),
        )
        return None
    finally:
        service._cancellations.clear(conversation_id)
        # D3: remove per-cid queues so the WS handler knows no DR run is
        # active for this cid (it tests `cid in _dr_steer` for routing).
        service.forget(conversation_id)
        # Return the run's transient working set (fetch buffers + ONNX batch
        # temporaries, already freed by the time run.run() returned) back to
        # the OS, so a long-lived server doesn't accumulate a permanent RSS
        # floor from bursty DR runs. The retention half of the OOM fix.
        _release_process_memory()


async def run_execute(
    service: DeepResearchService,
    conversation_id: str,
    plan: PlanEvent,
    *,
    resume_from: ReportEvent | None = None,
) -> None:
    """The post-approval driver: run DeepResearchRun on the approved plan,
    emitting Action/Observation events for every retrieval round + section
    synthesis, ending with a ReportEvent + StatusEvent(FINISHED).

    ``resume_from`` is a prior stopped run's partial ReportEvent: its
    completed sections (+ their cited passages / discovered hits) are
    carried into the engine so resume continues from the checkpoint instead
    of redoing the sub-questions that already finished."""
    # Find the query from the user's last (pre-plan) message.
    events = await service._store.get_events(conversation_id)
    query = resolve_query(events, plan)
    plan_steps = [s.title for s in plan.steps]
    tier = service._depth_for(conversation_id)
    recency_window = service._recency_for(conversation_id)

    # Providers come from the existing research-stream plumbing (search,
    # extract, reranker, embedder, nli); the vectorstore is per-run
    # (InMemoryVectorStore). The router honors model_override from the
    # leader pill (the existing _router_now path).
    deps, space_ids, forbidden_space_ids = await build_retrieval_deps(service, conversation_id)
    if forbidden_space_ids:
        _LOG.warning(
            "dropping unowned space_ids for %s: %s",
            conversation_id,
            ", ".join(forbidden_space_ids),
        )

    # W-35/W-33: pre-flight the driver + the REQUIRED encoders BEFORE the engine
    # starts. A dead driver or a degraded/empty remote reranker/NLI would
    # otherwise run a long, expensive job that silently produces a wrong report.
    # On failure: emit a NAMED StatusEvent(ERROR) + ErrorEvent and DO NOT run.
    preflight_reason = await run_preflight(service, conversation_id, deps, space_ids)
    if preflight_reason is not None:
        await service._store.append(
            conversation_id,
            ErrorEvent(code="deep_research_preflight", detail=preflight_reason),
        )
        await service._lifecycle_commands.append_status(
            conversation_id, ConversationStatus.ERROR, detail=preflight_reason[:200]
        )
        return

    router, retrieval_engine = build_router_and_engine(service, conversation_id, deps)
    gather_cap = compute_gather_cap(service, plan_steps)
    run = build_run(
        service,
        conversation_id,
        query=query,
        router=router,
        retrieval_engine=retrieval_engine,
        deps=deps,
        tier=tier,
        recency_window=recency_window,
        gather_cap=gather_cap,
        space_ids=space_ids,
    )

    emit = build_emit_callback(service, conversation_id)
    resume_sections, resume_passages, resume_all_hits = rebuild_resume_state(resume_from)

    result = await run_engine(
        service,
        conversation_id,
        run,
        plan_steps=plan_steps,
        resume_sections=resume_sections,
        resume_passages=resume_passages,
        resume_all_hits=resume_all_hits,
        emit=emit,
    )
    if result is None:
        return

    # Emit the partial-or-final ReportEvent. If the user pressed Stop, the run
    # halted at a checkpoint (bounded_by="stopped") with the partial report
    # preserved → emit PAUSED (resumable), NOT FINISHED.
    await service._store.append(conversation_id, result.to_event())
    stopped = getattr(result, "bounded_by", None) == "stopped"
    await service._lifecycle_commands.append_status(
        conversation_id,
        ConversationStatus.PAUSED if stopped else ConversationStatus.FINISHED,
        detail="stopped" if stopped else None,
    )
