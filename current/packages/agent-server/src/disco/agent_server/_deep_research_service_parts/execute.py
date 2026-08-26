"""Deep Research execution-phase collaborators.

Extracted from ``DeepResearchService._execute_deep_research``
(PKG-11-RETRIEVAL wave 1, PY-0192/PY-0197/PY-0198), reworked for v2 (PKG-35):
the run driver's distinct concerns — query assembly, dependency/space
resolution, preflight, engine assembly, event-encoding (the emit callback),
checkpoint-resume rebuilding, and the run/cleanup lifecycle — each get their
own narrow collaborator here. ``run_execute`` is the sequencing of these
steps. With the plan gate gone (decision #7) the run takes the QUERY straight
from the conversation's user messages — no approved PlanEvent exists or is
needed. Every step takes ``service`` and resolves collaborators off it at
call time, so instance-level test monkeypatches
(``monkeypatch.setattr(rt.deep_research, "_execute_deep_research", ...)``) keep
resolving correctly regardless of this module boundary.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal

from disco.core import (
    ActionEvent,
    ConversationStatus,
    ErrorEvent,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    ResearchCheckpointEvent,
    ToolCall,
    ToolResult,
)
from disco.core.llm import DefaultLLMRouter, ModelRole
from disco.retrieval import DefaultRetrievalEngine, InMemoryVectorStore, RouterQueryRewriter
from disco.retrieval.deep_research import DeepResearchRun, DepthTier, ReportFromRun
from disco.retrieval.models import Passage, SearchHit

if TYPE_CHECKING:
    from ..deep_research_service import DeepResearchService

_LOG = logging.getLogger(__name__)


def build_query_with_constraints(events: list[Event]) -> str | None:
    """The research QUESTION is the FIRST user message; every LATER pre-run
    user message (typed before the run launched, or while it was paused) is
    folded in as an explicit instruction so the run honors it. Returns None
    when there is no user text yet (nothing to research; wait). Mid-run
    guidance never lands here — it routes into the live steer queue."""
    user_texts = [
        (e.message.content or "").strip()
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.USER
        and (e.message.content or "").strip()
    ]
    if not user_texts:
        return None
    query = user_texts[0]
    if len(user_texts) <= 1:
        return query
    constraints = "\n".join(f"- {t}" for t in user_texts[1:])
    return (
        f"{query}\n\n"
        f"The user added these instructions after the initial question — "
        f"the research MUST honor them:\n{constraints}"
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
    router = service._drivers.router(pick=service._settings._get_model_override(conversation_id))
    retrieval_engine = DefaultRetrievalEngine(
        search=search,
        extraction=extraction,
        reranker=deps["reranker"],
        embedder=deps.get("embedder"),
        rewriter=RouterQueryRewriter(router),
        vector_store=service._spaces.space_vector_store(),
    )
    return router, retrieval_engine


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
    space_ids: frozenset[str],
) -> DeepResearchRun:
    # G1/DR-4 F2: load any pre-attached upload passages (text files the user
    # attached in the initial box before submitting). Empty when no text
    # files were uploaded (the OFF path — byte-identical to pre-DR-4 code).
    # The v2 engine ignores the old gather-concurrency knob (the agent loop's
    # per-turn retrieval replaced the plan-wide gather fan-out), so the
    # RAM-aware cap computation is gone with it.
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
        recency_window=recency_window,
        # G1/DR-4 F2: seed the evidence pool with upload passages.
        upload_passages=upload_passages or None,
        corpus_ids=space_ids,
    )


def build_emit_callback(service: DeepResearchService, conversation_id: str):
    """Emit callback: every engine event becomes an Action/Observation pair
    on the conversation log so the UI's activity feed reflects progress.

    CORRELATION: a turn can issue several searches concurrently, so "the last
    ActionEvent appended" may belong to a different query when an observation
    arrives. The payload's stable query label (`subquestion`) plus turn number
    (`round`) correlate each observation to its own action in this run-scoped
    map."""
    action_by_key: dict[str, str] = {}

    def _corr_key(payload: dict[str, Any]) -> str | None:
        subq = payload.get("subquestion")
        if subq is None:
            return None
        rnd = payload.get("round")
        # Legacy gap_reason has no round → correlate to that label's latest search.
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
                # No query label in the payload (or pre-correlation emit) —
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
        if kind == "brief":
            # Decision #7: the brief is the model's first VISIBLE output —
            # besides the trace action above it lands as an assistant chat
            # message, so the conversation opens with the model's reading of
            # the question and its planned angles.
            await service._store.append(
                conversation_id,
                MessageEvent(
                    source=EventSource.AGENT,
                    message=LLMMessage(
                        role="assistant",
                        content=str(payload.get("text", "")),
                    ),
                ),
            )

    return emit


def rebuild_resume_state(
    resume_from: ResearchCheckpointEvent | None,
) -> tuple[
    list[Any] | None,
    list[Passage] | None,
    list[SearchHit] | None,
    list[str] | None,
    list[str] | None,
    list[dict[str, Any]] | None,
]:
    """Checkpointed resume: rebuild the retrieval-typed passages/hits from
    the prior stopped run's ResearchCheckpointEvent. The engine seeds its
    evidence pool and complete research trail, then writes the report fresh
    from the combined pool."""
    if resume_from is None:
        return None, None, None, None, None, None
    resume_passages = [Passage.model_validate(p) for p in resume_from.passages]
    resume_all_hits = [SearchHit.model_validate(h) for h in resume_from.all_hits]
    return (
        None,
        resume_passages,
        resume_all_hits,
        list(resume_from.completed_queries),
        None,
        list(resume_from.trail),
    )


def build_steer_hooks(service: DeepResearchService, conversation_id: str):
    """D3: pop_* closures drain the per-cid steer/inject queues at each
    turn boundary of the agent loop. The WS handler enqueues into these
    queues; the queues are removed in ``run_engine``'s ``finally`` so the
    presence of a key = "a DR run is currently in flight for this cid"."""

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
    resume_sections: list[Any] | None,
    resume_passages: list[Passage] | None,
    resume_all_hits: list[SearchHit] | None,
    resume_completed_probes: list[str] | None,
    resume_pending_probes: list[str] | None,
    resume_research_trail: list[dict[str, Any]] | None,
    emit: Any,
) -> ReportFromRun:
    """Run the engine and always clean up its per-run LIVE state, regardless
    of outcome. Exceptions propagate to ``run_execute``'s single failure
    handler (ErrorEvent + StatusEvent(ERROR)). The conversation's depth/
    recency/upload selections are NOT cleared here — a paused run needs them
    to resume with the same settings."""
    from ..runtime import _release_process_memory

    # Fresh cancel flag for this execution; the engine polls it at every
    # turn boundary so Stop actually halts the run (→ a resumable checkpoint).
    flag = service._cancellations.begin(conversation_id)

    # D3: initialise per-cid steer/inject queues on the research owner.
    service._live_state.begin(conversation_id)
    pop_steers, pop_injected_sources = build_steer_hooks(service, conversation_id)

    try:
        return await run.run(
            emit=emit,
            should_cancel=flag.is_set,
            resume_sections=resume_sections,
            resume_passages=resume_passages,
            resume_all_hits=resume_all_hits,
            resume_completed_probes=resume_completed_probes,
            resume_pending_probes=resume_pending_probes,
            resume_research_trail=resume_research_trail,
            pop_steers=pop_steers,
            pop_injected_sources=pop_injected_sources,
        )
    finally:
        service._cancellations.clear(conversation_id)
        # D3: remove per-cid queues so the WS handler knows no DR run is
        # active for this cid (enqueue_steer returns False → the WS handler falls
        # back to the normal user-turn path).
        service._live_state.forget(conversation_id)
        # Return the run's transient working set (fetch buffers + ONNX batch
        # temporaries, already freed by the time run.run() returned) back to
        # the OS, so a long-lived server doesn't accumulate a permanent RSS
        # floor from bursty DR runs. The retention half of the OOM fix.
        _release_process_memory()


def _tier_for_run(
    service: DeepResearchService,
    conversation_id: str,
    resume_from: ResearchCheckpointEvent | None,
) -> DepthTier:
    """Resolve the run's depth tier — from the checkpoint on resume.

    Depth/recency/uploads live in server memory only, and a resume can happen
    after that memory was cleared (or after a restart). The checkpoint event
    durably carries the tier the user chose, so a stopped Quick or Exhaustive
    run never silently resumes as Standard."""
    if resume_from is not None:
        try:
            return DepthTier(resume_from.depth_tier)
        except ValueError:
            pass
    return service._depth_for(conversation_id)


async def run_execute(
    service: DeepResearchService,
    conversation_id: str,
    *,
    resume_from: ResearchCheckpointEvent | None = None,
) -> None:
    """The run driver with exactly three outcomes: a ReportEvent + FINISHED,
    a ResearchCheckpointEvent + PAUSED, or an ErrorEvent + ERROR. Every failure —
    provider config, preflight, the engine, or persisting the final event —
    lands in the single failure handler here, so a run can never end without
    a terminal status."""
    try:
        await _run_to_report(service, conversation_id, resume_from=resume_from)
    except Exception as exc:  # noqa: BLE001 — every failure becomes a named run error
        _LOG.exception("deep research run failed for %s", conversation_id)
        service.forget(conversation_id)
        try:
            await service._store.append(
                conversation_id,
                ErrorEvent(
                    code="deep_research_failed",
                    detail=f"{type(exc).__name__}: {exc}",
                ),
            )
            await service._lifecycle_commands.append_status(
                conversation_id, ConversationStatus.ERROR, detail=str(exc)[:200]
            )
        except Exception:  # noqa: BLE001 — the failure record itself failed; only log
            _LOG.exception("failed to record deep research failure for %s", conversation_id)


async def _run_to_report(
    service: DeepResearchService,
    conversation_id: str,
    *,
    resume_from: ResearchCheckpointEvent | None = None,
) -> None:
    """Build deps, preflight, run the engine, persist the terminal events.

    ``resume_from`` is a prior stopped run's ResearchCheckpointEvent: its
    gathered evidence + issued queries (and depth tier) are carried into the
    engine so resume continues from the checkpoint instead of redoing the
    searches that already ran."""
    # The query is the conversation's first user message plus any pre-run
    # additions (decision #7: no PlanEvent exists — the question IS the launch).
    events = await service._store.get_events(conversation_id)
    query = build_query_with_constraints(events) or (
        resume_from.query if resume_from is not None else None
    )
    if not query:
        raise ValueError("deep research needs a user question before it can run")
    tier = _tier_for_run(service, conversation_id, resume_from)
    recency_window = (
        resume_from.recency_window
        if resume_from is not None and resume_from.recency_window is not None
        else service._recency_for(conversation_id)
    )

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
        service.forget(conversation_id)
        await service._store.append(
            conversation_id,
            ErrorEvent(code="deep_research_preflight", detail=preflight_reason),
        )
        await service._lifecycle_commands.append_status(
            conversation_id, ConversationStatus.ERROR, detail=preflight_reason[:200]
        )
        return

    # Gateless kickoff: RUNNING lands only after the preflight passes, so a
    # dead driver/encoder can never leave the conversation pinned RUNNING.
    await service._lifecycle_commands.append_status(
        conversation_id, ConversationStatus.RUNNING, detail="research"
    )

    router, retrieval_engine = build_router_and_engine(service, conversation_id, deps)
    run = build_run(
        service,
        conversation_id,
        query=query,
        router=router,
        retrieval_engine=retrieval_engine,
        deps=deps,
        tier=tier,
        recency_window=recency_window,
        space_ids=space_ids,
    )

    emit = build_emit_callback(service, conversation_id)
    (
        resume_sections,
        resume_passages,
        resume_all_hits,
        resume_completed_probes,
        resume_pending_probes,
        resume_research_trail,
    ) = rebuild_resume_state(resume_from)

    result = await run_engine(
        service,
        conversation_id,
        run,
        resume_sections=resume_sections,
        resume_passages=resume_passages,
        resume_all_hits=resume_all_hits,
        resume_completed_probes=resume_completed_probes,
        resume_pending_probes=resume_pending_probes,
        resume_research_trail=resume_research_trail,
        emit=emit,
    )

    # Emit exactly one terminal product or checkpoint event. If the user pressed
    # Stop, the run halted before writing and emits a ResearchCheckpointEvent →
    # emit PAUSED (resumable) and KEEP the depth/recency/upload selections so
    # resume runs with the same settings. A finished run clears them.
    terminal_event = result.to_event()
    await service._store.append(conversation_id, terminal_event)
    stopped = isinstance(terminal_event, ResearchCheckpointEvent)
    await service._lifecycle_commands.append_status(
        conversation_id,
        ConversationStatus.PAUSED if stopped else ConversationStatus.FINISHED,
        detail="stopped" if stopped else None,
    )
    if not stopped:
        service._state.forget(conversation_id)
