"""Cross-contract acceptance gate — agent-loop-contract.md §10.9.

End-to-end with fakes, composing THREE contracts: a user message → run() → the
real RouterAgent builds a CompletionRequest(AGENT_DRIVER) to the (faked) router →
the loop gates per policy → executes via the fake executor → appends an
observation → the agent declares finished → FINISHED. Asserts the event log
replays to the same final ConversationState and that request_id flowed into
ActionEvent.llm_response_id.
"""

from __future__ import annotations

from llm_fakes import simple_config  # the router-contract test config
from loop_fakes import FakeExecutor, SequenceProvider, build_loop
from perpleximanus.core import ActionEvent, ConversationState, ConversationStatus
from perpleximanus.core.llm import DefaultLLMRouter, ProposedToolCall
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
