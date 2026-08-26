from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from disco.agent_server.deep_research_provider import DeepResearchProvider
from disco.agent_server.deep_research_service import DeepResearchService
from disco.agent_server.lifecycle_command_service import LifecycleCommandService
from disco.core import (
    ConversationStatus,
    EventSource,
    LLMMessage,
    MessageEvent,
    ReportEvent,
    ReportSection,
)


class _Resp:
    def __init__(self, text: str) -> None:
        self.text = text


class _Router:
    def __init__(self, text: str) -> None:
        self.text = text
        self.requests: list[Any] = []

    async def complete(self, req: Any) -> _Resp:
        self.requests.append(req)
        return _Resp(self.text)


class _Store:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def append(self, conversation_id: str, event: Any) -> Any:
        self.events.append(event)
        return event


class _Drivers:
    def __init__(self, router: _Router) -> None:
        self._router = router

    def router(self, pick: Any = None) -> _Router:
        # The follow-up path pins the conversation's leader-model override.
        del pick
        return self._router


class _NLI:
    def entail(self, premise: str, hypothesis: str) -> str:
        overlap = set(premise.lower().split()) & set(hypothesis.lower().split())
        return "entail" if overlap else "neutral"

    def score(self, premise: str, hypothesis: str) -> float:
        return 1.0 if self.entail(premise, hypothesis) == "entail" else 0.0


class _Harness:
    def __init__(self, text: str) -> None:
        self.store = _Store()
        self.drivers = _Drivers(_Router(text))

        store = self.store

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

        self.lifecycle_commands = _LifecycleCommands()


def _prior_report() -> ReportEvent:
    return ReportEvent(
        source=EventSource.AGENT,
        query="What is the current state of local LLMs?",
        summary="Qwen models are competitive at different hardware tiers.",
        sections=[
            ReportSection(
                id="s0",
                title="Hardware tiers",
                markdown="Qwen models are competitive at different hardware tiers.",
            )
        ],
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
        pytest.param(
            "<think>private reasoning only",
            "I couldn't produce a source-supported answer to that follow-up from the saved "
            "report. Its cited sources may not cover the question.",
            id="<think>private reasoning only-",
        ),
        (
            "Use Qwen 3:30B for value [[p1]]. It is unquestionably the best model.",
            "Use Qwen 3:30B for value [[p1]].",
        ),
    ],
)
async def test_follow_up_synthesis_strips_think_before_storing(
    raw_answer: str,
    stored_answer: str,
) -> None:
    harness = _Harness(raw_answer)
    svc = DeepResearchService(
        store=harness.store,
        lifecycle_commands=harness.lifecycle_commands,  # type: ignore[arg-type]
        drivers=harness.drivers,  # type: ignore[arg-type]
        settings=MagicMock(),
        preflight=MagicMock(),
        spaces=MagicMock(),
        cancellations=MagicMock(),
        provider=DeepResearchProvider(
            MagicMock(), MagicMock(), MagicMock(), injected_providers={"nli": _NLI()}
        ),
    )
    prior = _prior_report().model_copy(update={"seq": 1})
    user = MessageEvent(
        source=EventSource.USER,
        message=LLMMessage(role="user", content="Which Qwen model is best?"),
    ).model_copy(update={"seq": 2})

    await svc._follow_up_deep_research("c1", [prior, user], prior)

    assert harness.drivers._router.requests[0].enable_thinking is False

    stored_messages = [
        e
        for e in harness.store.events
        if isinstance(e, MessageEvent) and e.source == EventSource.AGENT
    ]
    assert [e.message.content for e in stored_messages] == [stored_answer]


def test_follow_up_history_keeps_prior_turns_but_excludes_current_question() -> None:
    from disco.agent_server._deep_research_service_parts.followup import _follow_up_history

    prior = _prior_report().model_copy(update={"seq": 1})
    events = [
        prior,
        MessageEvent(
            source=EventSource.USER,
            message=LLMMessage(role="user", content="First follow-up"),
        ).model_copy(update={"seq": 2}),
        MessageEvent(
            source=EventSource.AGENT,
            message=LLMMessage(role="assistant", content="First grounded answer [[p1]]."),
        ).model_copy(update={"seq": 3}),
        MessageEvent(
            source=EventSource.USER,
            message=LLMMessage(role="user", content="Current follow-up"),
        ).model_copy(update={"seq": 4}),
    ]
    history = _follow_up_history(events, prior)
    assert "First follow-up" in history
    assert "First grounded answer" in history
    assert "Current follow-up" not in history


@pytest.mark.asyncio
async def test_follow_up_uses_reviewed_passage_and_preserves_provenance() -> None:
    from disco.agent_server._deep_research_service_parts.followup import _saved_passage_model

    harness = _Harness("Muse Glimmer was released as open source [[reviewed]].")
    svc = DeepResearchService(
        store=harness.store,
        lifecycle_commands=harness.lifecycle_commands,  # type: ignore[arg-type]
        drivers=harness.drivers,  # type: ignore[arg-type]
        settings=MagicMock(),
        preflight=MagicMock(),
        spaces=MagicMock(),
        cancellations=MagicMock(),
        provider=DeepResearchProvider(
            MagicMock(), MagicMock(), MagicMock(), injected_providers={"nli": _NLI()}
        ),
    )
    prior = ReportEvent(
        source=EventSource.AGENT,
        query="What open-source models shipped?",
        summary="A release was found.",
        sections=[
            ReportSection(
                id="s0", title="Findings", markdown="The saved report has findings."
            )
        ],
        passages=[],
        reviewed_passages=[
            {
                "id": "reviewed",
                "source_title": "",
                "source_url": "https://example.com/release",
                "text": "Muse Glimmer was released as open source.",
                "char_start": 14,
                "char_end": 58,
                "corpus_id": "release-corpus",
                "published_at": "2026-08-11",
            }
        ],
        all_hits=[],
    ).model_copy(update={"seq": 1})
    user = MessageEvent(
        source=EventSource.USER,
        message=LLMMessage(role="user", content="Which model was released?"),
    ).model_copy(update={"seq": 2})

    await svc._follow_up_deep_research("c-reviewed", [prior, user], prior)

    request_text = harness.drivers._router.requests[0].messages[-1].content
    assert "[[reviewed]] (https://example.com/release)" in request_text
    assert "Muse Glimmer was released" in request_text
    restored = _saved_passage_model(prior.reviewed_passages[0])
    assert (restored.char_start, restored.char_end, restored.corpus_id) == (
        14,
        58,
        "release-corpus",
    )
    assert restored.published_at is not None
    assert restored.published_at.isoformat() == "2026-08-11"
    stored = [
        event.message.content
        for event in harness.store.events
        if isinstance(event, MessageEvent) and event.source == EventSource.AGENT
    ]
    assert stored == ["Muse Glimmer was released as open source [[reviewed]]."]
