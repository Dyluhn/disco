"""Doubles for the whole-report writer's unit tests.

`test_synthesis_repair._ScriptedRouter` replays `(text, finish_reason)` and
counts calls, which is everything the continuation loop needs. The tests one
level down also have to read what the writer SENT — the inspect stage a call
declared itself by, its temperature, and the exact messages — so the router
here keeps every `CompletionRequest` instead of only counting them. A scripted
item may also be an exception, which is how a transport failure is put in front
of one specific call.

Nothing here decides anything: it replays, it records, and it builds the small
evidence pool the writer's entry point refuses to run without.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

from disco.core.llm import (
    CompletionRequest,
    CompletionResponse,
    LLMRouter,
    StreamChunk,
    TokenUsage,
)
from disco.retrieval.deep_research.agent import ResearchOutcome
from disco.retrieval.models import Passage

#: One scripted reply: the text, `(text, finish_reason)`, or an exception to
#: raise instead of answering.
ScriptedReply = str | tuple[str, str] | BaseException


class RecordingRouter(LLMRouter):
    """Replays scripted replies in order and keeps every request that was made."""

    def __init__(self, script: Sequence[ScriptedReply] = ()) -> None:
        self._script = list(script)
        self.requests: list[CompletionRequest] = []

    @property
    def calls(self) -> int:
        return len(self.requests)

    @property
    def stages(self) -> list[str]:
        """The inspect stage each call declared itself by, in call order."""
        return [stage_of(request) for request in self.requests]

    def prompt(self, index: int) -> str:
        """Every message of one call, joined — what the model actually read."""
        return "\n".join(message.content for message in self.requests[index].messages)

    def last_message(self, index: int) -> str:
        return self.requests[index].messages[-1].content

    async def complete(
        self, request: CompletionRequest, *, context: Any = None
    ) -> CompletionResponse:
        del context
        self.requests.append(request)
        reply: ScriptedReply = self._script.pop(0) if self._script else ""
        if isinstance(reply, BaseException):
            raise reply
        text, finish = reply if isinstance(reply, tuple) else (reply, "stop")
        return CompletionResponse(
            text=text,
            tool_calls=[],
            usage=TokenUsage(input_tokens=1, output_tokens=1),
            finish_reason=finish,  # type: ignore[arg-type]
            model_used="fake",
            request_id=request.request_id,
            routing=None,
        )

    async def stream_complete(
        self, request: CompletionRequest, *, context: Any = None
    ) -> AsyncIterator[StreamChunk]:
        async def gen() -> AsyncIterator[StreamChunk]:
            yield StreamChunk(done=True, final=await self.complete(request, context=context))

        return gen()


def stage_of(request: CompletionRequest) -> str:
    """The inspect stage a call declared, which is how the writer names itself
    to the activity heartbeat and the acceptance harness."""
    return (request.metadata or {}).get("inspect_stage", "")


def assessed_review(verdict: str) -> str:
    """Add an explicit positive assessment to the measured-evidence fixture.

    Only callers constructing positive protocol fixtures use this helper;
    RecordingRouter still replays malformed and missing assessments unchanged.
    """
    payload = json.loads(verdict)
    payload["checks"] = [
        {
            "claim_id": "c1",
            "judgment": "supported",
            "evidence_standard": "met",
            "evidence": [{"source_id": "s1", "quote": "reported figures"}],
            "reason": "The retained fixture source describes reported figures and collected data.",
        }
    ]
    return json.dumps(payload)


def collect_emits() -> tuple[list[tuple[str, dict[str, Any]]], Any]:
    """A capture list plus the async emit callback the writer awaits."""
    captured: list[tuple[str, dict[str, Any]]] = []

    async def emit(kind: str, payload: dict[str, Any]) -> None:
        captured.append((kind, payload))

    return captured, emit


def pool(count: int = 2) -> list[Passage]:
    """A small evidence pool whose passage text entails the reports below under
    the word-overlap NLI double."""
    return [
        Passage(
            id=f"p{index}",
            source_url=f"https://example.test/{index}",
            source_title=f"Source {index}",
            text=(
                "Measured evidence about the subject, with reported figures and "
                f"collected data from the observed window {index}."
            ),
        )
        for index in range(1, count + 1)
    ]


def outcome(
    passages: list[Passage] | None = None,
    *,
    trail: list[dict[str, Any]] | None = None,
) -> ResearchOutcome:
    """The research result the writer is handed."""
    return ResearchOutcome(
        brief="The question was read as a measurement question.",
        passages=passages if passages is not None else pool(),
        all_hits=[],
        trail=trail or [],
        bounded_by=None,
        coverage={},
    )


def report_markdown(summary: str, sections: Sequence[tuple[str, str]]) -> str:
    """A draft in the shape the structural check accepts."""
    body = "\n\n".join(f"## {title}\n{text}" for title, text in sections)
    return f"{summary}\n\n{body}" if body else summary


__all__ = [
    "RecordingRouter",
    "ScriptedReply",
    "collect_emits",
    "outcome",
    "pool",
    "report_markdown",
    "stage_of",
]
