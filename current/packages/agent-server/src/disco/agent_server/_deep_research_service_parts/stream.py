"""Deep Research live-answer-stream preparation.

Extracted from ``DeepResearchService.research_stream`` (PKG-11-RETRIEVAL wave
1, PY-0192 — the class-logical cap): streaming is one of the natural seams
of this service (alongside plan/execute/follow-up), and ``research_stream``
was the single largest remaining chunk of the class body. Everything before
the actual token stream — driver preflight, space resolution/validation,
encoder preflight, and composing the search/extraction/router/seed-passages
inputs — is pure preparation with no ``yield`` of its own, so it collapses
into one awaitable that returns either an error frame or the prepared
inputs. The method left on the service still owns the one thing that
actually has to live in a generator: the ``async for ... yield`` loop over
``stream_research_answer``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, NamedTuple

from disco.core.llm import DefaultLLMRouter, ModelRole
from disco.retrieval.models import Passage

if TYPE_CHECKING:
    from ..deep_research_service import DeepResearchService


class StreamInputs(NamedTuple):
    """Everything ``stream_research_answer`` needs, already resolved."""

    deps: dict[str, Any]
    router: DefaultLLMRouter
    search: Any
    extraction: Any
    seed_passages: list[Passage]
    space_ids: frozenset[str]


async def prepare(
    service: DeepResearchService,
    conversation_id: str | None,
    *,
    model_override: str | None,
    think: bool,
    space_ids: frozenset[str],
    owner_id: str | None,
    include_unclaimed_legacy: bool,
    sources: Sequence[str] | None,
) -> tuple[dict[str, Any] | None, StreamInputs | None]:
    """Resolve + preflight everything the live answer stream needs.

    Returns ``(error_frame, None)`` on any preflight/validation failure —
    the caller yields the frame and stops — or ``(None, StreamInputs)`` when
    the stream is clear to start.

    W-35/W-33: before streaming, pre-flight the resolved driver (one cheap
    call) and the required encoders (reranker + NLI). On failure, an
    ``{type: error, message}`` frame NAMES the unreachable driver/encoder —
    instead of a doomed stream on a dead model or a silently-wrong answer
    from a degraded reranker/verifier.

    G1/DR-4 F3: when ``conversation_id`` is provided and the conversation has
    pre-attached upload passages (text files the user uploaded in the initial
    box), they are threaded into the rerank step as ``seed_passages`` so they
    compete for ``top_k`` slots alongside live-web content and can be cited.
    OFF-path (no cid or no uploads): byte-identical to the pre-DR-4 code."""
    search_override = service._search_override_for_sources(sources)
    deps = service._research(search_override=search_override)
    # W-35 + P1-3: pre-flight the model the ANSWER STREAM actually uses for
    # generation — RAG_ANSWERER (streaming.py), NOT AGENT_DRIVER. With those
    # roles assigned to different endpoints, probing AGENT_DRIVER would both
    # FALSE-BLOCK a healthy answerer and MISS a dead one. When a pill is set
    # the override pins every role to the same model, so RAG_ANSWERER still
    # resolves to the picked model.
    driver_reason = await service._preflight.check(
        conversation_id, override=model_override, role=ModelRole.RAG_ANSWERER
    )
    if driver_reason is not None:
        return {"type": "error", "message": driver_reason}, None

    requested_space_ids = space_ids or service._spaces.get_space_ids(conversation_id)
    space_owner_id = owner_id
    if space_owner_id is None and conversation_id:
        space_owner_id = service._store.conversation_owner_id_sync(conversation_id)
    requested_space_ids, forbidden_space_ids = service._validated_space_ids(
        requested_space_ids,
        owner_id=space_owner_id,
        include_unclaimed_legacy=include_unclaimed_legacy,
    )
    if forbidden_space_ids:
        return {
            "type": "error",
            "message": "space_forbidden",
            "space_ids": list(forbidden_space_ids),
        }, None

    # W-33: the live answer grounds on the reranker + NLI verifier. Space
    # grounding also needs the embedder for vector lookup.
    required_encoders = (
        ("reranker", "nli", "embedder") if requested_space_ids else ("reranker", "nli")
    )
    enc_reason = await service._preflight_encoders(deps, required=required_encoders)
    if enc_reason is not None:
        return {"type": "error", "message": enc_reason}, None

    # RP-05b §3: MCP retrieval providers join the citation path here too — the
    # composite hands MCP-discovered hits to the SAME GroundingPipeline.
    search, extraction = service._provider.compose_mcp_retrieval(deps)
    router = service._drivers.router(
        pick=model_override,
        enable_thinking=think,
    )
    # G1/DR-4 F3: load seed passages from the upload corpus for this cid.
    # Empty list when no text files were attached (the OFF path).
    seed_passages = service.get_upload_passages(conversation_id) if conversation_id else []

    return None, StreamInputs(
        deps=deps,
        router=router,
        search=search,
        extraction=extraction,
        seed_passages=seed_passages,
        space_ids=requested_space_ids,
    )
