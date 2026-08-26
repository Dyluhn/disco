"""Wiring the search/extract tools to the engine + the Research flow — §5/§6.

The shippable form of what the §8.5 acceptance gate did inline:
`retrieval_capability_handlers` builds the orchestrator-side capability handlers
the `search`/`extract` tools call (tool contract §6/§9) — register them with a
`CapabilityBroker` so the tools reach the providers without any provider key
entering the sandbox (the key lives provider-side, never in the handler args or
result). `research_answer` is the §6 standard-answer composition: retrieve →
ground.

No import of the `tools` package — handlers are plain async callables the
orchestrator registers; this keeps `retrieval` a sibling that depends only on
`core`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Literal

from .engine import RetrievalEngine
from .grounding import GroundingPipeline
from .models import GroundedAnswer, RetrievalRequest
from .providers import ExtractionProvider, SearchProvider

CapabilityHandler = Callable[..., Awaitable[Any]]


def compose_search_providers(
    configured: SearchProvider,
    additions: Sequence[SearchProvider],
) -> SearchProvider:
    """Compose a configured provider with stable, additive source providers.

    The configured provider is always first.  Provider names are the stable
    source identities at this boundary, so repeated source selections (or a
    source that is already configured) cannot create a duplicate search leg.
    An empty addition list is deliberately an identity operation: callers keep
    the exact configured provider object and therefore all of its wiring state.
    """
    if not additions:
        return configured

    from .source_adapters import MultiSearchProvider

    def flatten(provider: SearchProvider) -> list[SearchProvider]:
        # A per-run override is itself commonly a MultiSearchProvider.  Keep
        # the composition one level deep so a configured source selected again
        # is deduplicated by its real provider name and queried only once.
        if isinstance(provider, MultiSearchProvider):
            flattened: list[SearchProvider] = []
            for child in provider._providers:
                flattened.extend(flatten(child))
            return flattened
        return [provider]

    providers: list[SearchProvider] = []
    seen: set[str] = set()
    flattened_additions = [
        child for addition in additions for child in flatten(addition)
    ]
    for provider in [*flatten(configured), *flattened_additions]:
        identity = str(getattr(provider, "name", type(provider).__name__)).strip().lower()
        if identity and identity not in seen:
            seen.add(identity)
            providers.append(provider)

    if len(providers) == 1:
        return configured
    return MultiSearchProvider(tuple(providers))


def retrieval_capability_handlers(
    search: SearchProvider, extraction: ExtractionProvider
) -> dict[str, CapabilityHandler]:
    """[CONTRACT] Build the 'search'/'extract' capability handlers backing the
    tool-contract tools onto these providers. Returns plain handlers to register
    with a CapabilityBroker; provider credentials stay inside the providers
    (orchestrator-held), never in the args/results that reach the sandbox (§6)."""

    async def search_handler(*, query: str, limit: int = 8) -> list[dict[str, str]]:
        hits = await search.search(query, limit=limit)
        # Discovery only: urls + (untrusted) snippets — never cited as content.
        return [{"url": h.url, "title": h.title, "snippet": h.snippet} for h in hits]

    async def extract_handler(*, url: str) -> dict[str, Any]:
        doc = await extraction.extract(url)
        return {
            "url": doc.url,
            "title": doc.title,
            "content": doc.content,
            "fetched_ok": doc.fetched_ok,
            "status": doc.status,
            # The provider's concrete failure cause (e.g. an egress-policy denial
            # for a loopback preview URL). Dropping it left the model a bare
            # "error" and invited a retry of an impossible call (pilot seed
            # 405717); the tool layer renders this message verbatim.
            "error": doc.error,
        }

    return {"search": search_handler, "extract": extract_handler}


async def research_answer(
    query: str,
    engine: RetrievalEngine,
    grounding: GroundingPipeline,
    *,
    depth: Literal["shallow", "standard", "deep"] = "standard",
    top_k: int = 8,
    corpus_ids: frozenset[str] = frozenset(),
) -> GroundedAnswer:
    """[CONTRACT §6] The standard-answer Research flow: retrieve with provenance,
    then run the grounding pipeline (constrained generation + NLI verification +
    self-correction). The loop drives *when* to call this; it owns no retrieval
    logic. Deep Research is the same composition at `depth='deep'`."""
    result = await engine.retrieve(
        RetrievalRequest(query=query, depth=depth, top_k=top_k, corpus_ids=corpus_ids)
    )
    return await grounding.answer(query, result)
