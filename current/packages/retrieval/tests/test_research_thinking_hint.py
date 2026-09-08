"""Deep-research call boundaries preserve configured reasoning defaults."""

from collections.abc import AsyncIterator
from typing import Any

import pytest
from disco.core import LLMMessage
from disco.core.llm import CompletionRequest, CompletionResponse, TokenUsage
from disco.retrieval.deep_research import _turn_protocol, _writer_parts, _writer_review
from disco.retrieval.deep_research import decompose as decompose_mod


class _CaptureRouter:
    def __init__(self) -> None:
        self.requests: list[CompletionRequest] = []

    async def complete(
        self, request: CompletionRequest, *, context: Any = None
    ) -> CompletionResponse:
        del context
        self.requests.append(request)
        return CompletionResponse(
            text="one research angle",
            tool_calls=[],
            usage=TokenUsage(input_tokens=1, output_tokens=1),
            finish_reason="stop",
            model_used="capture",
            request_id=request.request_id,
            routing=None,
        )

    async def stream_complete(
        self, request: CompletionRequest, *, context: Any = None
    ) -> AsyncIterator[Any]:
        del request, context
        if False:
            yield None


@pytest.mark.asyncio
async def test_all_deep_research_call_owners_leave_reasoning_to_the_provider() -> None:
    router = _CaptureRouter()
    messages = [LLMMessage(role="user", content="Assess the evidence.")]

    await decompose_mod.decompose_query(router, "the state of X", max_subq=1)
    await _turn_protocol._complete_turn(router, messages, namespace="test", max_tokens=100)
    await _writer_review._writer_call(
        router,
        messages,
        max_tokens=100,
        temperature=0.0,
        conversation_id=None,
        stage="report_draft",
        attempt=1,
    )
    await _writer_parts.review_call(router, messages, max_tokens=100, metadata=None)

    assert len(router.requests) == 4
    assert [request.enable_thinking for request in router.requests] == [None] * 4
