"""The provider adapter boundary — llm-router-contract.md §3.

A `ModelProvider` executes a `CompletionRequest` against one backend (a local
server, or OpenRouter). This is where ALL provider-specific shaping lives;
everything above it (router, policy, prompts) is provider-neutral.

v1 ships the protocol here. The concrete HTTP adapters — `ollama`/`llamacpp`
(local) and `openrouter` (overflow) — are [INTERIOR] implementations whose model
ids, base URLs, and auth are all [VERIFY] at build (and whose OpenRouter key
comes from the secrets store, never current/prompts/sandbox). They are intentionally NOT
built here: they require network + the operator's real model config, and the
whole router is provable headless against a fake provider (§10). Adding an
adapter = implementing this protocol + classifying that provider's
context-window error into `LLMContextWindowExceeded` (errors.py §6.1).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from .types import CompletionRequest, CompletionResponse, Requirement, StreamChunk


@runtime_checkable
class ModelProvider(Protocol):
    """[CONTRACT] One backend. Translates the neutral request to the provider's
    API and the provider's response back to CompletionResponse. Raises the typed
    errors in errors.py. Returns `CompletionResponse.routing == None` — the
    router attaches the RoutingDecision (RT1)."""

    name: str  # "ollama" | "llamacpp" | "openrouter"

    async def complete(self, req: CompletionRequest, *, model: str) -> CompletionResponse: ...

    # Plain `def` returning an AsyncIterator: the async-generator signature
    # (iterate with `async for`, never `await`). See the note on
    # routing.LLMRouter.stream_complete for why this departs from the contract's
    # literal `async def`.
    def stream_complete(
        self, req: CompletionRequest, *, model: str
    ) -> AsyncIterator[StreamChunk]: ...

    def supports(self, requirement: Requirement, *, model: str) -> bool: ...
