"""RouterSummarizer — llm-router-contract.md §9.1 (v1.1: async seam).

Implements the event/state contract's `Summarizer` protocol by routing a
SUMMARIZER-role completion. Uses the cheap local model (never the agent model),
per BoD §7.3. This is the concrete object the memory condenser depends on.

`summarize` is **async** (event-state-contract v1.2 §5.2): it makes an async
router call, awaited by the condenser, which is itself awaited by the loop's
`_materialize_view`. The earlier sync shim (which deadlocked inside the loop's
running event loop) is gone — the seam is async end-to-end.
"""

from __future__ import annotations

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

    async def summarize(self, messages: list[LLMMessage]) -> str:
        req = CompletionRequest(
            profile=CapabilityProfile(role=ModelRole.SUMMARIZER),
            messages=[*messages, LLMMessage(role="user", content=_SUMMARIZE_INSTRUCTION)],
            temperature=0.0,
        )
        resp = await self._router.complete(req)
        return resp.text
