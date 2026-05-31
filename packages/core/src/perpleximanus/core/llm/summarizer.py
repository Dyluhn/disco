"""RouterSummarizer — llm-router-contract.md §9.1.

Implements the event/state contract's `Summarizer` protocol by routing a
SUMMARIZER-role completion. Uses the cheap local model (never the agent model),
per BoD §7.3. This is the concrete object the memory condenser depends on.

Note on sync vs async: the event contract's `Summarizer.summarize` is sync at
the call site, but a router call is async. This class exposes BOTH:
  - `asummarize()` — the native async router call.
  - `summarize()` — a sync shim that runs the async call to completion, so it
    drops straight into the sync `Summarizer` protocol. The loop may instead
    await `asummarize()` and hand the resulting string to the condenser ([INTERIOR]
    wiring); the contract is only that summarization is a SUMMARIZER-role call.
"""

from __future__ import annotations

import asyncio

from ..events import LLMMessage
from .routing import LLMRouter
from .types import CapabilityProfile, CompletionRequest, ModelRole

_SUMMARIZE_INSTRUCTION = (
    "Summarize the above concisely, preserving facts, decisions, and open threads."
)


class RouterSummarizer:
    """[CONTRACT] Satisfies the event contract's `Summarizer` protocol."""

    def __init__(self, router: LLMRouter) -> None:
        self._router = router

    def _build_request(self, messages: list[LLMMessage]) -> CompletionRequest:
        return CompletionRequest(
            profile=CapabilityProfile(role=ModelRole.SUMMARIZER),
            messages=[*messages, LLMMessage(role="user", content=_SUMMARIZE_INSTRUCTION)],
            temperature=0.0,
        )

    async def asummarize(self, messages: list[LLMMessage]) -> str:
        resp = await self._router.complete(self._build_request(messages))
        return resp.text

    def summarize(self, messages: list[LLMMessage]) -> str:
        """Sync shim for the event contract's `Summarizer` protocol.

        Raises if called from within a running event loop (use `asummarize`
        there instead) — running an event loop inside a running loop is not
        supported.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.asummarize(messages))
        raise RuntimeError(
            "RouterSummarizer.summarize() called inside a running event loop; "
            "await asummarize() instead."
        )
