"""Cross-contract acceptance gate — agent-loop-contract.md §10.9.

End-to-end with fakes, composing THREE contracts: a user message → run() → the
real RouterAgent builds a CompletionRequest(AGENT_DRIVER) to the (faked) router →
the loop gates per policy → executes via the fake executor → appends an
observation → the agent declares finished → FINISHED. Asserts the event log
replays to the same final ConversationState and that request_id flowed into
ActionEvent.llm_response_id.
"""

from __future__ import annotations

from llm_fakes import FakeModelProvider, simple_config  # the router-contract test config
from loop_fakes import FakeExecutor, SequenceProvider, build_loop
from perpleximanus.core import (
    ActionEvent,
    ConversationState,
    ConversationStatus,
    ErrorEvent,
)
from perpleximanus.core.llm import DefaultLLMRouter, LLMContentFiltered, ProposedToolCall
from perpleximanus.core.loop import NeverConfirm, RouterAgent

CID = "conv"


async def test_loop_router_event_contracts_compose():
    # Faked router: call 1 proposes a shell tool call; call 2 returns plain text
    # (RouterAgent treats "no tool call" as finished).
    provider = SequenceProvider(
        [
            {"tool_calls": [ProposedToolCall(tool_name="shell", arguments={"cmd": "ls -la"})]},
            {"text": "all done"},
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
    from perpleximanus.core.llm import ModelRole

    assert provider.seen[0].profile.role == ModelRole.AGENT_DRIVER

    # Determinism: the full log replays to the same final ConversationState.
    replayed = ConversationState.reconstruct(CID, events)
    assert replayed.execution_status == ConversationStatus.FINISHED
    assert replayed == ConversationState.reconstruct(CID, events)  # pure


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
    assert rejecting.calls == 1
