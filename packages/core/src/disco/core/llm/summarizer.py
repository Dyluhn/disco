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

# A-S4: a STRUCTURED summary instruction. The old freeform "preserve facts,
# decisions, open threads" produced an opaque blob that could silently drop the
# exact things a long build can't lose — file paths, the plan's state, and which
# approaches already FAILED (so the agent doesn't repeat them). This forces those
# load-bearing categories to survive condensation.
_SUMMARIZE_INSTRUCTION = (
    "Condense the conversation above into a STRUCTURED summary. Preserve, under "
    "these exact headings (omit a heading only if truly empty):\n"
    "FILES: every file created or modified, by path, with a one-line note of what it contains.\n"
    "DECISIONS: key choices made (tech stack, architecture, naming) and why.\n"
    "PROGRESS: which plan steps are done vs still open.\n"
    "FAILED: approaches that were TRIED AND FAILED, and the reason — so they are "
    "not repeated.\n"
    "OPEN: unresolved threads / what remains.\n"
    "Be concise but do not drop file paths, the plan state, or failure reasons."
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
