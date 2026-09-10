"""Write a Deep Research report again from a pool the engine already saved.

A writer change used to be judged by re-running the whole research loop, which
costs about ninety minutes and a different evidence pool every time. The engine
now saves the writer's ENTIRE input as one file (``retrieval.deep_research.
pool``); this module runs the writer over that file with the server's live
router and NLI, through the SAME emit callback and the SAME report assembly a
normal run uses. The client therefore sees the ordinary event stream and the
ordinary terminal ReportEvent — the only difference is that no research turn
happened, so two writer runs over one pool differ only by the writer.

Sequencing mirrors ``execute.run_execute`` and reuses its collaborators, so the
replay cannot drift from the run it is standing in for.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from disco.core import ConversationStatus, ErrorEvent
from disco.retrieval import InMemoryVectorStore
from disco.retrieval.deep_research import (
    DeepResearchRun,
    SavedPool,
    model_activity_events,
)

from .execute import (
    _append_terminal_error,
    build_emit_callback,
    build_retrieval_deps,
    build_router_and_engine,
    publish_result,
    run_preflight,
)
from .failures import preflight_failure

if TYPE_CHECKING:
    from ..deep_research_service import DeepResearchService

_LOG = logging.getLogger(__name__)


async def run_write_from_pool(
    service: DeepResearchService, conversation_id: str, pool: SavedPool
) -> None:
    """Replay the writer over ``pool``, ending in the same terminal pair a run
    does: a ReportEvent + FINISHED, or an ErrorEvent + ERROR.

    Stop is not wired. A replay holds no evidence a checkpoint could carry that
    the pool file does not already hold, so the answer to "stop this" is to run
    the entry point again rather than to resume it.
    """
    try:
        await _write_to_report(service, conversation_id, pool)
    except Exception as exc:  # noqa: BLE001 — every failure becomes a named run error
        _LOG.exception("write-from-pool failed for %s", conversation_id)
        service.forget(conversation_id)
        await _append_terminal_error(service, conversation_id, exc)


async def _write_to_report(
    service: DeepResearchService, conversation_id: str, pool: SavedPool
) -> None:
    """Build the deps, preflight, write, persist the terminal events."""
    deps, space_ids, forbidden_space_ids = await build_retrieval_deps(service, conversation_id)
    if forbidden_space_ids:
        _LOG.warning(
            "dropping unowned space_ids for %s: %s",
            conversation_id,
            ", ".join(forbidden_space_ids),
        )

    # The writer needs the same live driver and NLI a run needs, and a dead one
    # would otherwise surface as a mid-write failure rather than a named refusal.
    preflight_reason = await run_preflight(service, conversation_id, deps, space_ids)
    if preflight_reason is not None:
        service.forget(conversation_id)
        await service._store.append(
            conversation_id,
            ErrorEvent(
                code="deep_research_preflight",
                detail=preflight_reason,
                failure=preflight_failure(preflight_reason),
            ),
        )
        await service._lifecycle_commands.append_status(
            conversation_id, ConversationStatus.ERROR, detail=preflight_reason[:200]
        )
        return

    await service._lifecycle_commands.append_status(
        conversation_id, ConversationStatus.RUNNING, detail="research"
    )

    router, retrieval_engine = build_router_and_engine(service, conversation_id, deps)
    # The pool's own tier resolves the writer's bound (`depth.bounds_for`, via
    # the run's constructor), so a replayed exhaustive report is written under
    # the envelope the original run had rather than the default one.
    run = DeepResearchRun(
        query=pool.query,
        router=router,
        retrieval_engine=retrieval_engine,
        embedder=deps.get("embedder"),
        vector_store=InMemoryVectorStore(),
        nli=deps["nli"],
        depth=pool.depth_tier,
        conversation_id=conversation_id,
        recency_window=pool.recency_window,
        corpus_ids=space_ids,
    )
    emit = build_emit_callback(service, conversation_id)
    with model_activity_events(emit):
        result = await run.write_from_pool(pool.outcome, emit=emit)

    await publish_result(service, conversation_id, result)
