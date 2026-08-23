"""Deep Research plan-proposal collaborators.

Extracted from ``DeepResearchService._propose_deep_research_plan``
(PKG-11-RETRIEVAL wave 1, PY-0192/PY-0195/PY-0196): the query/constraint
extraction, the two-role preflight, the pre-decompose setup (tier/bound/
router/recency), and the post-decompose PlanEvent construction + finalize
are each their own narrow step. The ``decompose_query`` call itself stays
inline in ``deep_research_service.py`` — that module imports
``decompose_query`` at module level and tests monkeypatch it there
(``monkeypatch.setattr(drs, "decompose_query", ...)``); relocating the call
site would silently defeat that patch point.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, NamedTuple

from disco.core import (
    ConversationStatus,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    PlanEvent,
    PlanStep,
)
from disco.core.llm import DefaultLLMRouter, ModelRole
from disco.retrieval.deep_research import DepthBound, DepthTier

if TYPE_CHECKING:
    from ..deep_research_service import DeepResearchService


def build_query_with_constraints(events: list[Event]) -> str | None:
    """The research QUESTION is the FIRST user message; every LATER user
    message is plan feedback ("do not include X", "focus on Y"). Fold later
    messages in as explicit constraints so a re-propose after a revision
    plans the REVISED QUESTION, not the revision text itself. Returns None
    when there is no user text yet (nothing to plan; wait)."""
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
        f"The user revised the research plan with these instructions — "
        f"the sub-questions MUST honor them:\n{constraints}"
    )


async def preflight_plan_roles(
    service: DeepResearchService, conversation_id: str, override: str | None
) -> str | None:
    """P1-2: pre-flight RAG_ANSWERER (the role the run uses for generation)
    before setting RUNNING. W-35-fu: also pre-flight QUERY_REWRITER — the
    decompose_query call the caller makes next uses THAT role, not
    RAG_ANSWERER, and a true black-hole (endpoint accepts the socket, never
    responds) would otherwise stall the DR kick unboundedly with the
    conversation pinned RUNNING. When override pins all roles to one model
    this is a cache hit (no added latency)."""
    reason = await service._preflight.check(
        conversation_id, override=override, role=ModelRole.RAG_ANSWERER
    )
    if reason is not None:
        return reason
    return await service._preflight.check(
        conversation_id, override=override, role=ModelRole.QUERY_REWRITER
    )


class DecomposeContext(NamedTuple):
    """Everything the inline ``decompose_query`` call in
    ``deep_research_service.py`` needs, already resolved."""

    tier: DepthTier
    bound: DepthBound
    router: DefaultLLMRouter
    recency_window: Literal["month", "week"] | None


def prepare_decompose_context(
    service: DeepResearchService, conversation_id: str
) -> DecomposeContext:
    """Resolve the depth tier/bound, the decompose router, and the recency
    window. Honor the model pill here too: the post-approval engine already
    passes the override (see ``execute.build_run``) — without it HERE, a
    user's pick would silently apply to gather/synthesis but NOT to the
    decompose/plan step (the exact half-applied-pill bug)."""
    from disco.retrieval.deep_research import bounds_for

    tier = service._depth_for(conversation_id)
    bound = bounds_for(tier)
    router = service._drivers.router(
        pick=service._settings._get_model_override(conversation_id)
    )
    recency_window = service._recency_for(conversation_id)
    return DecomposeContext(tier=tier, bound=bound, router=router, recency_window=recency_window)


async def handle_decompose_failure(
    service: DeepResearchService, conversation_id: str, exc: Exception
) -> None:
    """A decompose failure (e.g. a dead/unauthed QUERY_REWRITER the
    RAG_ANSWERER preflight doesn't cover) must take the conversation to
    ERROR — NOT leave it stuck RUNNING with only a system-reminder (the
    conversation would otherwise spin forever)."""
    await service._store.append(
        conversation_id,
        MessageEvent(
            source=EventSource.ENVIRONMENT,
            message=LLMMessage(
                role="user",
                content=(
                    "<system-reminder>\n"
                    f"Plan decomposition failed: {type(exc).__name__}: {exc}. "
                    "Try a more specific query.\n"
                    "</system-reminder>"
                ),
            ),
        ),
    )
    await service._lifecycle_commands.append_status(
        conversation_id,
        ConversationStatus.ERROR,
        detail=(f"Plan decomposition failed: {type(exc).__name__}: {exc}")[:200],
    )


async def finalize_plan(
    service: DeepResearchService,
    conversation_id: str,
    *,
    query: str,
    tier: DepthTier,
    bound: DepthBound,
    subqs: list[Any],
    prior_plans: int,
) -> None:
    """Build the PlanEvent from the initial research directions, persist it, and
    either auto-approve + run the engine (autonomous/headless surfaces —
    mirrors the Build loop's autonomous plan auto-approve) or park at
    AWAITING_PLAN_APPROVAL for a human to approve."""
    steps = [PlanStep(title=s.title) for s in subqs]
    summary = (
        f"Deep research on: {query.strip()[:140]}. "
        f"The investigation will begin with {len(steps)} broad directions, then "
        f"adapt as evidence reveals stronger leads, gaps, or dead ends "
        f"(tier: {tier.value}; cap: {bound.max_sources} sources)."
    )
    plan = PlanEvent(
        summary=summary,
        steps=steps,
        revision=prior_plans + 1,
        context=(
            f"**Query:** {query.strip()}\n\n"
            f"**Depth tier:** {tier.value}\n\n"
            f"**Starting research directions** (these guide the investigation; "
            f"the final report outline will be written from the findings):\n\n"
            + "\n".join(f"{i + 1}. {s.title}" for i, s in enumerate(subqs))
        ),
    )
    await service._store.append(conversation_id, plan)
    if service._settings._effective_autonomous(conversation_id):
        await service._lifecycle_commands.append_status(
            conversation_id, ConversationStatus.RUNNING, detail="plan_approved"
        )
        await service._execute_deep_research(conversation_id, plan)
        return
    await service._lifecycle_commands.append_status(
        conversation_id, ConversationStatus.AWAITING_PLAN_APPROVAL, detail=plan.id
    )
