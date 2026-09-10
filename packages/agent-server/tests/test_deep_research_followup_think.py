from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from disco.agent_server.deep_research_provider import DeepResearchProvider
from disco.agent_server.deep_research_service import DeepResearchService
from disco.agent_server.lifecycle_command_service import LifecycleCommandService
from disco.core import (
    ActionEvent,
    ConversationStatus,
    EventSource,
    LLMMessage,
    MessageEvent,
    ReportEvent,
    ReportSection,
)
from disco.core.llm.stream_progress import reporter_for
from disco.core.llm.types import CompletionResponse, TokenUsage


def _response(text: str, finish_reason: str = "stop") -> CompletionResponse:
    """A REAL CompletionResponse — the follow-up path reads more than `.text`
    (it records the call's model/usage/finish reason in the inspect trace), and
    a stub that carries only the fields today's code happens to touch turns
    every new read into a false AttributeError failure."""
    return CompletionResponse(
        text=text,
        usage=TokenUsage(input_tokens=0, output_tokens=0),
        finish_reason=finish_reason,  # type: ignore[arg-type]
        model_used="test-model",
    )


class _Router:
    def __init__(self, text: str, finish_reason: str = "stop") -> None:
        self.text = text
        self.finish_reason = finish_reason
        self.requests: list[Any] = []

    async def complete(self, req: Any) -> CompletionResponse:
        self.requests.append(req)
        return _response(self.text, self.finish_reason)


class _StreamingRouter(_Router):
    """A router that reports through the REAL progress seam, as the OpenAI
    adapter does: ``reporter_for(req.metadata)``, deltas as they arrive, close.

    `router.complete` is not a buffered POST — it rides the streamed transport
    and already publishes what the socket delivers. That is why this fake is
    the honest one for the follow-up leg: nothing about the provider had to
    change for the heartbeat to have something to report.
    """

    async def complete(self, req: Any) -> CompletionResponse:
        self.requests.append(req)
        reporter = reporter_for(req.metadata)
        if reporter is not None:
            await reporter.observed(40, 0)
            await reporter.observed(60, 0)
            await reporter.close()
        return _response(self.text, self.finish_reason)


class _BufferedRouter(_Router):
    """A transport with no chunks at all — the Responses API and the one-shot
    fallback for a server that refuses to stream. It makes exactly one report,
    at call start, and it is marked ``streams: false``."""

    async def complete(self, req: Any) -> CompletionResponse:
        self.requests.append(req)
        reporter = reporter_for(req.metadata)
        if reporter is not None:
            await reporter.buffered_start()
        return _response(self.text, self.finish_reason)


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
    def __init__(
        self, text: str, finish_reason: str = "stop", router: _Router | None = None
    ) -> None:
        self.store = _Store()
        self.drivers = _Drivers(router or _Router(text, finish_reason))

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
            "The model returned no answer text for that follow-up, so there was "
            "nothing to check against the report's sources.\n\n"
            "The report, its sources and its exports are unchanged, and no answer "
            "was saved.\n\n"
            "Ask again — the same question is worth a second attempt — or ask a "
            "narrower one. The follow-up box below stays open either way.",
            id="<think>private reasoning only-",
        ),
        (
            "Use Qwen 3:30B for value [[p1]]. It is unquestionably the best model.",
            "Use Qwen 3:30B for value [[p1]].\n\n---\n*Removed 1 of 2 statements: "
            "not supported by this report's sources.*",
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


def _service(harness: _Harness) -> DeepResearchService:
    return DeepResearchService(
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


async def _ask(harness: _Harness) -> list[Any]:
    prior = _prior_report().model_copy(update={"seq": 1})
    user = MessageEvent(
        source=EventSource.USER,
        message=LLMMessage(role="user", content="Which Qwen model is best?"),
    ).model_copy(update={"seq": 2})
    await _service(harness)._follow_up_deep_research("c1", [prior, user], prior)
    return harness.store.events


def _ledger(events: list[Any]) -> dict[str, Any]:
    ledgers = [
        e.tool_call.arguments
        for e in events
        if isinstance(e, ActionEvent) and e.tool_call.tool_name == "follow_up_grounding"
    ]
    assert len(ledgers) == 1, "every follow-up answer carries exactly one ledger"
    return ledgers[0]


@pytest.mark.asyncio
async def test_follow_up_budget_leaves_room_for_a_reasoning_model_to_think() -> None:
    """Regression: every hosted follow-up returned an EMPTY answer.

    `enable_thinking=False` is a llama.cpp/vLLM server extension that a hosted
    OpenAI-compatible provider never receives; the model reasoned anyway and
    billed it against a `max_tokens` sized for the prose alone, so the content
    channel came back empty with `finish_reason="length"` and the user saw a
    refusal blaming the report's sources. Measured on 6/6 live follow-ups
    against a hosted provider before this fix.
    """
    harness = _Harness("Use Qwen 3:30B for value [[p1]].")
    await _ask(harness)
    request = harness.drivers._router.requests[0]
    assert request.enable_thinking is False, "still asked for non-thinking where honored"
    assert request.max_tokens is not None and request.max_tokens > 1_400, (
        "the answer budget must carry think headroom on top of the prose provision"
    )


@pytest.mark.asyncio
async def test_follow_up_empty_completion_says_empty_not_uncovered_sources() -> None:
    """Regression: an empty completion was reported as 'sources may not cover
    the question' — a cause the system never observed."""
    events = await _ask(_Harness("", finish_reason="length"))
    answer = [
        e.message.content
        for e in events
        if isinstance(e, MessageEvent) and e.source == EventSource.AGENT
    ][0]
    assert "returned no answer text" in answer
    assert "output limit" in answer, "a length finish names why the text is missing"
    assert "may not cover the question" not in answer
    assert _ledger(events) == {
        "statements": 0,
        "supported": 0,
        "weak": 0,
        "removed": 0,
        "refused": True,
    }


@pytest.mark.asyncio
async def test_follow_up_keeps_weak_statements_and_reports_what_it_removed() -> None:
    """Regression: retention kept ONLY entailed statements, so an evaluative
    answer — neutral to NLI by construction — was stripped whole and replaced
    by a refusal. The report path keeps `weak`; the follow-up now does too."""
    harness = _Harness(
        "Qwen 3:30B uses about 19 GB at Q4 [[p1]]. "
        "That tier suits a 24 GB card best [[p1]]. "
        "Nothing here is cited at all."
    )
    events = await _ask(harness)
    answer = [
        e.message.content
        for e in events
        if isinstance(e, MessageEvent) and e.source == EventSource.AGENT
    ][0]
    assert "19 GB at Q4 [[p1]]" in answer
    assert "24 GB card best [[p1]]" in answer, "a weak (neutral) statement is kept"
    assert "Nothing here is cited at all" not in answer
    assert "Removed 1 of 3 statements" in answer
    ledger = _ledger(events)
    assert ledger["refused"] is False
    assert ledger["statements"] == ledger["supported"] + ledger["weak"] + ledger["removed"]
    assert ledger["removed"] == 1


@pytest.mark.asyncio
async def test_follow_up_wall_counts_what_failed_and_offers_a_wired_next_step() -> None:
    """Nothing survived: the wall states why with the observed counts, the
    state the run is in, the next action, and what stays available."""
    events = await _ask(
        _Harness("Nothing in this answer carries a citation. Llama wins outright [[p9]].")
    )
    answer = [
        e.message.content
        for e in events
        if isinstance(e, MessageEvent) and e.source == EventSource.AGENT
    ][0]
    assert "None of the 2 statements" in answer
    assert "1 source passages saved with this report" in answer
    assert "1 carried no citation" in answer
    # `[[p9]]` is an id the report never had: NLI never ran on it, so the wall
    # must not call it a contradiction — that would be a verdict nobody produced.
    assert "1 cited a source id this report does not have" in answer
    assert "contradicted" not in answer
    assert "and no answer" in answer and "was saved" in answer
    assert "start a new research run" in answer
    assert "follow-up box below stays open" in answer
    assert _ledger(events)["refused"] is True


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


def _grounding_phases(events: list[Any]) -> list[dict[str, Any]]:
    return [
        event.tool_call.arguments
        for event in events
        if isinstance(event, ActionEvent)
        and event.tool_call.tool_name == "phase"
        and event.tool_call.arguments.get("phase") == "grounding"
    ]


@pytest.mark.asyncio
async def test_follow_up_grounding_reports_every_statement_it_verifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the grounding pass was the follow-up's silent leg.

    Measured on the largest saved report (152 grounding passages): the wire
    went 123 s without a frame between `phase synthesizing` and the grounding
    ledger, of which ~86 s was this pass. The page's stale-frame watchdog is
    45 s, so it closed and reconnected mid-answer every time.

    The pass rides the EXISTING `phase` action — no new progress action name —
    and the payload is exactly `phase`/`done`/`total`, `done` never above
    `total`, the last one equal to it.
    """
    from disco.agent_server._deep_research_service_parts import followup as followup_parts

    monkeypatch.setattr(followup_parts, "_GROUNDING_PROGRESS_INTERVAL_S", 0.0)
    harness = _Harness(
        "Qwen 3:30B uses about 19 GB at Q4 [[p1]]. "
        "Qwen models are competitive at different hardware tiers [[p1]]. "
        "Qwen 3:30B is a MoE model [[p1]]."
    )

    phases = _grounding_phases(await _ask(harness))

    assert [(p["done"], p["total"]) for p in phases] == [(0, 3), (1, 3), (2, 3), (3, 3)]
    assert all(set(p) == {"phase", "done", "total"} for p in phases)


@pytest.mark.asyncio
async def test_follow_up_grounding_still_reports_its_first_and_last_when_throttled() -> None:
    """A pass whose statements verify instantly must not flood the event log.

    The floor between events drops the middle of a fast pass, and the two that
    matter — "it started, of N" and "it finished all N" — always survive.
    """
    harness = _Harness(
        "Qwen 3:30B uses about 19 GB at Q4 [[p1]]. "
        "Qwen models are competitive at different hardware tiers [[p1]]. "
        "Qwen 3:30B is a MoE model [[p1]]."
    )

    phases = _grounding_phases(await _ask(harness))

    assert [(p["done"], p["total"]) for p in phases] == [(0, 3), (3, 3)]


def _model_activity(events: list[Any]) -> list[dict[str, Any]]:
    return [
        event.tool_call.arguments
        for event in events
        if isinstance(event, ActionEvent) and event.tool_call.tool_name == "model_activity"
    ]


@pytest.mark.asyncio
async def test_follow_up_model_call_reports_while_it_is_still_open() -> None:
    """Regression: the follow-up's answer call was a second silent leg.

    Measured on the largest saved report at 29 / 37 / 52 s of wire silence,
    against a client that reconnects after 45 s of it. Nothing about the
    transport was wrong — `router.complete` already publishes what the socket
    delivers — but no observer was installed around the call and the request
    declared no stage, so the heartbeat had nothing to admit and said nothing.
    """
    harness = _Harness("", router=_StreamingRouter("Qwen 3:30B is best [[p1]]."))

    events = await _ask(harness)

    activity = _model_activity(events)
    assert [a["stage"] for a in activity] == ["follow_up", "follow_up"]
    # Measured, never projected: the deltas the fake transport delivered.
    assert [a["tokens_streamed"] for a in activity] == [40, 100]
    assert [a["call_ordinal"] for a in activity] == [1, 1]
    assert all("streams" not in a for a in activity)


@pytest.mark.asyncio
async def test_follow_up_declares_its_stage_so_the_heartbeat_can_admit_it() -> None:
    """`model_activity_stage` emits NOTHING for a call that names no stage, so
    the label the inspect trace already used has to reach the request too."""
    harness = _Harness("Qwen 3:30B is best [[p1]].")

    await _ask(harness)

    assert harness.drivers._router.requests[0].metadata == {"inspect_stage": "follow_up"}


@pytest.mark.asyncio
async def test_follow_up_on_a_buffered_transport_still_reports_once_at_call_start() -> None:
    """A transport with no chunks makes the one report it honestly can."""
    harness = _Harness("", router=_BufferedRouter("Qwen 3:30B is best [[p1]]."))

    activity = _model_activity(await _ask(harness))

    assert [(a["stage"], a["tokens_streamed"], a["streams"]) for a in activity] == [
        ("follow_up", 0, False)
    ]


def test_no_corpus_wall_does_not_send_the_reader_back_to_a_move_that_cannot_work() -> None:
    """Regression: the no-corpus refusal told the reader to "run the research
    again" and nothing else — no current state, no statement of what still
    works, and by omission an invitation to retry the follow-up, which on this
    report always lands on the same wall."""
    from disco.agent_server._deep_research_service_parts.followup import _no_corpus_wall

    wall = _no_corpus_wall()

    assert "saved without its source passages" in wall  # why
    assert "no answer was saved" in wall  # current state
    assert "Run the research again" in wall  # next action
    assert "opens, reads and exports exactly as it is" in wall  # what remains allowed
    assert "Every follow-up on this report reaches this same point" in wall


def test_ungrounded_wall_does_not_count_to_zero_when_there_was_nothing_to_check() -> None:
    """Regression: an answer of bare list markers reaches the ungrounded wall
    with no claims, and the text read "None of the 0 statements ... ()" — a
    verdict breakdown for a check that never ran."""
    from disco.agent_server._deep_research_service_parts.followup import _ungrounded_wall

    wall = _ungrounded_wall([], 152)

    assert "None of the 0" not in wall
    assert "()" not in wall
    assert "no statement in it to check" in wall
    assert "152 source passages" in wall
