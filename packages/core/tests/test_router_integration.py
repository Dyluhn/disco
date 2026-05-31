"""Cross-contract integration — llm-router-contract.md §10.6 (the acceptance gate).

Proves the LLM-router contract and the event/state contract compose: build
`view.messages` (event contract) → a CompletionRequest → a faked tool call comes
back → it maps cleanly to the event contract's ToolCall/ActionEvent, with
request_id carried into ActionEvent.llm_response_id. No real model involved.
"""

from __future__ import annotations

from llm_fakes import FakeModelProvider, build_router
from perpleximanus.core import (
    ActionEvent,
    EventSource,
    LLMMessage,
    MessageEvent,
    ToolCall,
    View,
)
from perpleximanus.core.llm import (
    CapabilityProfile,
    CompletionRequest,
    ModelRole,
    ProposedToolCall,
)


async def test_view_to_request_to_action_event_round_trip():
    # 1. Event contract: a conversation's View → the messages the LLM sees.
    events = [
        MessageEvent(
            source=EventSource.USER, message=LLMMessage(role="user", content="list the files")
        ).model_copy(update={"seq": 1}),
    ]
    view = View.of(events)
    assert [m.content for m in view.messages] == ["list the files"]

    # 2. Router contract: the loop builds an AGENT_DRIVER request from the View.
    proposed = ProposedToolCall(
        tool_name="shell", arguments={"cmd": "ls -la"}, provider_call_id="prov_77"
    )
    driver = FakeModelProvider("ollama", text="I'll list the files.", tool_calls=[proposed])
    router, _sink, _ = build_router(local=driver)
    req = CompletionRequest(
        profile=CapabilityProfile(role=ModelRole.AGENT_DRIVER),
        messages=view.messages,
        request_id="req-123",
    )
    resp = await router.complete(req)

    # RT1: routing + usage present.
    assert resp.routing is not None and resp.usage is not None
    assert resp.request_id == "req-123"  # correlation id round-tripped
    assert len(resp.tool_calls) == 1

    # 3. Map the router's ProposedToolCall back into the event contract's types,
    #    exactly as the agent loop will (§11 of the router contract).
    pc = resp.tool_calls[0]
    tool_call = ToolCall(tool_name=pc.tool_name, arguments=pc.arguments)
    action = ActionEvent(
        thought=resp.text,
        tool_call=tool_call,
        llm_response_id=resp.request_id,  # carried into the event, per §2 note
    )

    assert action.tool_call.tool_name == "shell"
    assert action.tool_call.arguments == {"cmd": "ls -la"}
    assert action.thought == "I'll list the files."
    assert action.llm_response_id == "req-123"  # the contracts compose
    # And the ActionEvent renders back to an LLM assistant message (event contract).
    assert action.to_llm_message().role == "assistant"
