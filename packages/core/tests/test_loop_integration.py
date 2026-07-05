"""Cross-contract acceptance gate — agent-loop-contract.md §10.9.

End-to-end with fakes, composing THREE contracts: a user message → run() → the
real RouterAgent builds a CompletionRequest(AGENT_DRIVER) to the (faked) router →
the loop gates per policy → executes via the fake executor → appends an
observation → the agent declares finished → FINISHED. Asserts the event log
replays to the same final ConversationState and that request_id flowed into
ActionEvent.llm_response_id.
"""

from __future__ import annotations

from disco.core import (
    ActionEvent,
    ConversationState,
    ConversationStatus,
    ErrorEvent,
    EventSource,
    MessageEvent,
)
from disco.core.llm import DefaultLLMRouter, LLMContentFiltered, ProposedToolCall
from disco.core.loop import NeverConfirm, RouterAgent
from llm_fakes import FakeModelProvider, simple_config  # the router-contract test config
from loop_fakes import FakeExecutor, SequenceProvider, build_loop

CID = "conv"


async def test_loop_router_event_contracts_compose():
    # Faked router: call 1 proposes a shell tool call; call 2 calls the `finish`
    # tool — the new AFFIRMATIVE terminal move (GAP B). A tool-less prose turn no
    # longer ends the run; only `finish` does.
    provider = SequenceProvider(
        [
            {"tool_calls": [ProposedToolCall(tool_name="shell", arguments={"cmd": "ls -la"})]},
            {
                "text": "all done",
                "tool_calls": [
                    ProposedToolCall(tool_name="finish", arguments={"summary": "listed the files"})
                ],
            },
        ]
    )
    router = DefaultLLMRouter(simple_config(), {"ollama": provider, "openrouter": provider})
    agent = RouterAgent(router, conversation_id=CID)
    executor = FakeExecutor()
    loop, store = build_loop(agent, executor=executor, policy=NeverConfirm())

    await loop.send_message("list the files")
    state = await loop.run()

    # Terminal: FINISHED.
    assert state.execution_status == ConversationStatus.FINISHED

    # The tool actually executed with the model's arguments.
    assert len(executor.calls) == 1
    assert executor.calls[0].tool_name == "shell"
    assert executor.calls[0].arguments == {"cmd": "ls -la"}

    events = await store.get_events(CID)

    # request_id flowed: router echoed it; the loop carried it into the event.
    action = next(e for e in events if isinstance(e, ActionEvent))
    sent_request_id = provider.seen[0].request_id
    assert sent_request_id is not None
    assert action.llm_response_id == sent_request_id

    # The agent declared AGENT_DRIVER role to the router (capability routing).
    from disco.core.llm import ModelRole

    assert provider.seen[0].profile.role == ModelRole.AGENT_DRIVER

    # Determinism: the full log replays to the same final ConversationState.
    replayed = ConversationState.reconstruct(CID, events)
    assert replayed.execution_status == ConversationStatus.FINISHED
    assert replayed == ConversationState.reconstruct(CID, events)  # pure


async def test_truncated_prose_does_not_end_run_and_injects_continue_reminder():
    """W-31 end-to-end — a prose turn the provider cut off mid-sentence
    (finish_reason=="length", no tool call) must NOT silently end the run.
    WITHOUT the fix a prose-finishing agent (RouterAgent) would FINISH on the
    truncated fragment; WITH it, the loop records the partial text, injects a
    'your message was cut off — continue it' reminder, and re-steps. The model
    then does real work and finishes on a later turn."""
    provider = SequenceProvider(
        [
            # turn 1: cut off mid-sentence with no tool call.
            {"text": "…Let me take a real", "finish_reason": "length"},
            # turn 2: a real action (proves the run continued past the fragment).
            {"tool_calls": [ProposedToolCall(tool_name="shell", arguments={"cmd": "ls -la"})]},
            # turn 3: affirmative finish (work has happened → gates pass).
            {
                "text": "all done",
                "tool_calls": [
                    ProposedToolCall(tool_name="finish", arguments={"summary": "listed the files"})
                ],
            },
        ]
    )
    router = DefaultLLMRouter(simple_config(), {"ollama": provider, "openrouter": provider})
    agent = RouterAgent(router, conversation_id=CID)  # prose_finishes=True — the hard case
    executor = FakeExecutor()
    loop, store = build_loop(agent, executor=executor, policy=NeverConfirm())

    await loop.send_message("build it")
    state = await loop.run()

    # The run did NOT end on the truncated fragment — it continued and finished.
    assert state.execution_status == ConversationStatus.FINISHED
    # The real action actually executed (proof the loop stepped past truncation).
    assert any(c.tool_name == "shell" for c in executor.calls)

    events = await store.get_events(CID)
    # The partial assistant text was recorded, not dropped.
    assert any(
        isinstance(e, MessageEvent)
        and e.source == EventSource.AGENT
        and "Let me take a real" in (e.message.content or "")
        for e in events
    ), "the truncated fragment must be persisted as the assistant's partial turn"
    # A continue reminder was injected on the ENVIRONMENT channel.
    assert any(
        isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and "cut off mid-sentence" in (e.message.content or "")
        for e in events
    ), "a 'continue where you left off' reminder must be injected after truncation"


async def test_unclosed_think_wedge_injects_continue_reminder_and_continues():
    """fix-slides-wedge end-to-end — reproduces Dylan's wedged agent slides build.

    A reasoning model (MiniMax) inlined its whole chain-of-thought as a literal
    `<think>` block in `content`, exhausted the output cap mid-thought — drafting
    an entire deck inside the unclosed block — and returned WITHOUT
    finish_reason=="length". Before the fix that never-finished, tool-less turn
    was mis-read as a clean no-op: the loop silently re-stepped another 2-minute
    reasoning dump that never reached a tool call, so the build looked WEDGED (the
    user had to kill it). WITH the fix the unclosed `<think>` is detected as a
    structural truncation from the raw response, strips the user-visible
    reasoning span, injects the 'cut off — take the action now with a tool call'
    reminder, and re-steps to real work — never a silent spin."""
    provider = SequenceProvider(
        [
            # turn 1: the wedge — an unclosed `<think>` dump, finish_reason "stop".
            {
                "text": "<think>\nLet me design the deck. Slide 1 title, slide 2 the",
                "finish_reason": "stop",
            },
            # turn 2: steered back to a real action (proves the run stepped past it).
            {"tool_calls": [ProposedToolCall(tool_name="shell", arguments={"cmd": "ls -la"})]},
            # turn 3: affirmative finish (work happened → gates pass).
            {
                "text": "all done",
                "tool_calls": [
                    ProposedToolCall(tool_name="finish", arguments={"summary": "built the deck"})
                ],
            },
        ]
    )
    router = DefaultLLMRouter(simple_config(), {"ollama": provider, "openrouter": provider})
    agent = RouterAgent(router, conversation_id=CID)  # prose_finishes=True — the hard case
    executor = FakeExecutor()
    loop, store = build_loop(agent, executor=executor, policy=NeverConfirm())

    await loop.send_message("make slides for the report")
    state = await loop.run()

    # The run did NOT wedge/end on the unclosed-think fragment — it continued.
    assert state.execution_status == ConversationStatus.FINISHED
    assert any(c.tool_name == "shell" for c in executor.calls)

    events = await store.get_events(CID)
    # The raw response still drove truncation detection, but the user-visible
    # assistant message no longer leaks the inline chain-of-thought.
    assert not any(
        isinstance(e, MessageEvent)
        and e.source == EventSource.AGENT
        and "Let me design the deck" in (e.message.content or "")
        for e in events
    ), "the unclosed-think fragment must not be persisted as assistant-visible text"
    # The W-31 continue/take-action reminder was injected (NOT a silent no-op).
    assert any(
        isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and "cut off mid-sentence" in (e.message.content or "")
        for e in events
    ), "an unclosed `<think>` must trigger the 'cut off — take the action now' reminder"


async def test_provider_error_reaches_the_user_with_real_content():
    """Reactive error surfacing acceptance gate: when the driver model's provider
    rejects the call, the loop does NOT crash or flatten it — it emits a terminal
    ErrorEvent whose detail carries the PROVIDER's real reason, and the agent
    server streams that event to the UI. No capability check happened first; the
    request was sent and the provider's 'no' is what surfaced."""
    real_reason = "image input not supported by local-driver-q4"
    # The assigned driver provider rejects the call with a typed provider error.
    rejecting = FakeModelProvider("ollama", raises=LLMContentFiltered(real_reason))
    router = DefaultLLMRouter(
        simple_config(), {"ollama": rejecting, "openrouter": FakeModelProvider("openrouter")}
    )
    agent = RouterAgent(router, conversation_id=CID)
    loop, store = build_loop(agent, executor=FakeExecutor(), policy=NeverConfirm())

    await loop.send_message("describe the attached image")
    state = await loop.run()

    # The loop terminated as ERROR rather than raising out / hanging in RUNNING.
    assert state.execution_status == ConversationStatus.ERROR

    # Exactly one legible ErrorEvent reached the log (→ streamed to the UI).
    errors = [e for e in await store.get_events(CID) if isinstance(e, ErrorEvent)]
    assert len(errors) == 1
    err = errors[0]
    assert err.code == "model_error"
    # The REAL provider reason is intact — not flattened to "model call failed".
    assert real_reason in err.detail
    # The typed classification is preserved alongside the real reason.
    assert "LLMContentFiltered" in err.detail
    assert "model call failed" not in err.detail  # not a generic message
    # The provider WAS actually called (no pre-call capability gating).
    # rp-12 bounded requery: a provider rejection enters the requery ladder
    # (initial + 2 corrective attempts) before surfacing — 3 calls, one error.
    assert rejecting.calls == 3
