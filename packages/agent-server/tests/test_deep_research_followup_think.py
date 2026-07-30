from __future__ import annotations

from typing import Any

import pytest
from disco.agent_server.deep_research_service import DeepResearchService
from disco.agent_server.lifecycle_command_service import LifecycleCommandService
from disco.core import ConversationStatus, EventSource, LLMMessage, MessageEvent, ReportEvent


class _Resp:
    def __init__(self, text: str) -> None:
        self.text = text


class _Router:
    def __init__(self, text: str) -> None:
        self.text = text

    async def complete(self, req: Any) -> _Resp:
        return _Resp(self.text)


class _Store:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def append(self, conversation_id: str, event: Any) -> Any:
        self.events.append(event)
        return event


class _Runtime:
    def __init__(self, text: str) -> None:
        self._store = _Store()
        self._router = _Router(text)

        store = self._store

        class _LifecycleCommands:
            async def append_status(
                self,
                conversation_id: str,
                event: Any,
                *,
                detail: str | None = None,
            ) -> Any:
                if isinstance(event, ConversationStatus):
                    event = LifecycleCommandService.build_status(event, detail=detail)
                return await store.append(conversation_id, event)

        self._lifecycle_commands = _LifecycleCommands()

    def _router_now(self) -> _Router:
        return self._router


def _prior_report() -> ReportEvent:
    return ReportEvent(
        source=EventSource.AGENT,
        query="What is the current state of local LLMs?",
        summary="Qwen models are competitive at different hardware tiers.",
        sections=[],
        passages=[
            {
                "id": "p1",
                "source_title": "Local LLM Hardware Requirements 2026",
                "source_url": "https://example.com/local-llm-hardware",
                "text": "Qwen 3:30B MoE uses about 19 GB at Q4.",
            }
        ],
        all_hits=[],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_answer", "stored_answer"),
    [
        (
            "<think>private reasoning</think>Use Qwen 3:30B for value [[p1]].",
            "Use Qwen 3:30B for value [[p1]].",
        ),
        ("<think>private reasoning only", ""),
    ],
)
async def test_follow_up_synthesis_strips_think_before_storing(
    raw_answer: str,
    stored_answer: str,
) -> None:
    rt = _Runtime(raw_answer)
    svc = DeepResearchService(rt)
    prior = _prior_report().model_copy(update={"seq": 1})
    user = MessageEvent(
        source=EventSource.USER,
        message=LLMMessage(role="user", content="Which Qwen model is best?"),
    ).model_copy(update={"seq": 2})

    await svc._follow_up_deep_research("c1", [prior, user], prior)

    stored_messages = [
        e for e in rt._store.events if isinstance(e, MessageEvent) and e.source == EventSource.AGENT
    ]
    assert [e.message.content for e in stored_messages] == [stored_answer]
