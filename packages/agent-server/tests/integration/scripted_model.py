"""Scripted LLM provider — W8 evidence-harness integration layer.

Conforms to the real ``ModelProvider`` protocol (current/packages/core/src/disco/core/llm/provider.py)
so it wires through the REAL ``DefaultLLMRouter`` → ``RouterAgent.step()`` boundary,
NOT by calling internal loop functions directly.

Interface targeted:
  - ``ModelProvider.name: str``
  - ``ModelProvider.complete(req: CompletionRequest, *, model: str) -> CompletionResponse``
  - ``ModelProvider.stream_complete(req: CompletionRequest, *, model: str)
        -> AsyncIterator[StreamChunk]``
  - ``ModelProvider.supports(requirement: Requirement, *, model: str) -> bool``

The router calls ``provider.stream_complete(exec_req, model=entry.model_id)``
(routing.py line ~413) and assembles the final ``CompletionResponse`` from the
terminal ``StreamChunk.final``.  A step that carries zero ``ProposedToolCall``s
produces a tool-less prose turn; the agent interprets that as finishing (for the
``ResearchAgent`` / ``prose_finishes=True`` case) or as a no-op (for
``BuildAgent``).  Always include an explicit ``finish`` call in the step list to
terminate a build-surface run cleanly.

Role filtering
--------------
The scripted step sequence targets the ``AGENT_DRIVER`` role only.  Background
roles (``SUMMARIZER``, ``QUERY_REWRITER``, ``RAG_ANSWERER``, ``NLI_VERIFIER``)
are served a silent no-op response without consuming a scripted step.  This
prevents background services (e.g. the ``TitleService`` that calls the
``SUMMARIZER`` role on every kick) from interfering with the scripted sequence.

Usage::

    from disco.core.llm import ProposedToolCall
    from .scripted_model import ScriptedProvider

    steps = [
        ("writing output", [ProposedToolCall(tool_name="file_write",
                                             arguments={"path": "out.txt",
                                                        "content": "hello"})]),
        ("all done",       [ProposedToolCall(tool_name="finish",
                                             arguments={"summary": "done"})]),
    ]
    provider = ScriptedProvider(steps)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from disco.core.llm import (
    CompletionResponse,
    StreamChunk,
    TokenUsage,
)
from disco.core.llm.types import ModelRole, ProposedToolCall, Requirement

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from disco.core.llm.types import CompletionRequest

# A step is (assistant_text, list_of_tool_calls).
# An empty tool-call list means a tool-less prose turn.
Step = tuple[str, list[ProposedToolCall]]

# Roles whose calls should NOT consume a scripted step.  Background services
# (TitleService, RouterSummarizer, retrieval answerers) use these roles and
# must not shift the agent-driver step index.
_BACKGROUND_ROLES = frozenset(
    {
        ModelRole.SUMMARIZER,
        ModelRole.QUERY_REWRITER,
        ModelRole.RAG_ANSWERER,
        ModelRole.NLI_VERIFIER,
    }
)


def _noop_response(req: CompletionRequest, model: str) -> CompletionResponse:
    """A silent, tool-free response for background-role calls that should not
    consume a scripted step (summarizer, rewriter, etc.)."""
    return CompletionResponse(
        text="",
        tool_calls=[],
        usage=TokenUsage(input_tokens=0, output_tokens=0),
        finish_reason="stop",
        model_used=model,
        request_id=getattr(req, "request_id", None),
        routing=None,
    )


class ScriptedProvider:
    """A deterministic ``ModelProvider`` that replays a caller-supplied script.

    Each entry in *steps* is ``(text, tool_calls)`` where *tool_calls* is a
    list of ``ProposedToolCall`` objects (or an empty list for a prose-only
    turn).  Responses are served in order; the last step is repeated when the
    script is exhausted (guards against loops that call the model more times
    than the script accounts for, which would otherwise crash with an
    ``IndexError``).

    Only ``AGENT_DRIVER`` role calls consume a scripted step.  All other roles
    (``SUMMARIZER``, ``QUERY_REWRITER``, etc.) receive a silent no-op response
    so that background services (TitleService, condenser summarization) do not
    shift the step index and corrupt the scripted sequence.

    The provider emits through the real ``DefaultLLMRouter.stream_complete``
    path — the same path the production model uses — so every loop invariant
    (tool-call parsing, iteration counting, event emission) is exercised
    without network access or a real LLM.
    """

    name = "scripted"

    def __init__(self, steps: list[Step]) -> None:
        if not steps:
            raise ValueError("ScriptedProvider requires at least one step")
        self._steps = list(steps)
        self.call_count = 0

    def _response_for(self, req: CompletionRequest, model: str) -> CompletionResponse:
        """Return the next scripted response (agent-driver only), clamped to the
        last step.  Non-driver roles receive a no-op response without advancing
        the step counter."""
        if req.profile.role in _BACKGROUND_ROLES:
            return _noop_response(req, model)
        idx = min(self.call_count, len(self._steps) - 1)
        self.call_count += 1
        text, tool_calls = self._steps[idx]
        return CompletionResponse(
            text=text,
            tool_calls=list(tool_calls),
            usage=TokenUsage(input_tokens=1, output_tokens=1),
            finish_reason="tool_calls" if tool_calls else "stop",
            model_used=model,
            request_id=getattr(req, "request_id", None),
            routing=None,  # router attaches RoutingDecision (RT1)
        )

    async def complete(self, req: CompletionRequest, *, model: str) -> CompletionResponse:
        return self._response_for(req, model)

    async def stream_complete(
        self, req: CompletionRequest, *, model: str
    ) -> AsyncIterator[StreamChunk]:
        # Yield a single terminal chunk carrying the full response.
        # The router's stream_complete path (routing.py) iterates this
        # generator and attaches the RoutingDecision to chunk.final.
        yield StreamChunk(
            done=True,
            final=self._response_for(req, model),
        )

    def supports(self, requirement: Requirement, *, model: str) -> bool:  # noqa: ARG002
        return True
