"""Content-equality tests — event-state-contract.md §8.4.

event_content_eq must ignore every enumerated volatile field and be sensitive
to every semantic field. The stuck detector's correctness rests on this; the
test is table-driven (mutate each field, assert eq/neq).
"""

from __future__ import annotations

from perpleximanus.core import (
    ActionEvent,
    AgentErrorEvent,
    ConversationStatus,
    EventSource,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    StatusEvent,
    ToolCall,
    ToolResult,
    event_content_eq,
)

# ---- volatile fields are ignored --------------------------------------------


def test_actions_equal_despite_all_volatile_differences():
    """Two actions with identical semantic content but different id/seq/
    timestamp/call_id/llm_response_id are content-equal."""
    a = ActionEvent(thought="same", tool_call=ToolCall(tool_name="shell", arguments={"cmd": "ls"}))
    b = ActionEvent(
        thought="same",
        tool_call=ToolCall(tool_name="shell", arguments={"cmd": "ls"}),
        llm_response_id="resp_999",
    )
    b = b.model_copy(update={"seq": 42})
    # ids, call_ids, timestamps all differ by construction; still equal.
    assert a.id != b.id
    assert a.tool_call.call_id != b.tool_call.call_id
    assert event_content_eq(a, b)


def test_observations_equal_despite_volatile_correlation_ids():
    o1 = ObservationEvent(
        tool_result=ToolResult(call_id="c1", tool_name="shell", success=True, content="out"),
        action_id="evt_a",
    )
    o2 = ObservationEvent(
        tool_result=ToolResult(call_id="c2", tool_name="shell", success=True, content="out"),
        action_id="evt_b",
    )
    assert event_content_eq(o1, o2)  # action_id / call_id are volatile


# ---- semantic fields are significant ----------------------------------------


def test_action_thought_difference_breaks_equality():
    a = ActionEvent(thought="X", tool_call=ToolCall(tool_name="shell", arguments={}))
    b = ActionEvent(thought="Y", tool_call=ToolCall(tool_name="shell", arguments={}))
    assert not event_content_eq(a, b)


def test_action_tool_name_difference_breaks_equality():
    a = ActionEvent(thought="t", tool_call=ToolCall(tool_name="shell", arguments={}))
    b = ActionEvent(thought="t", tool_call=ToolCall(tool_name="browser", arguments={}))
    assert not event_content_eq(a, b)


def test_action_arguments_difference_breaks_equality():
    a = ActionEvent(thought="t", tool_call=ToolCall(tool_name="shell", arguments={"cmd": "ls"}))
    b = ActionEvent(thought="t", tool_call=ToolCall(tool_name="shell", arguments={"cmd": "rm"}))
    assert not event_content_eq(a, b)


def test_observation_content_difference_breaks_equality():
    o1 = observation_with(content="a")
    o2 = observation_with(content="b")
    assert not event_content_eq(o1, o2)


def test_observation_success_difference_breaks_equality():
    assert not event_content_eq(observation_with(success=True), observation_with(success=False))


def test_agent_error_message_significant():
    e1 = AgentErrorEvent(error="timeout")
    e2 = AgentErrorEvent(error="refused")
    assert not event_content_eq(e1, e2)
    assert event_content_eq(AgentErrorEvent(error="x"), AgentErrorEvent(error="x"))


def test_message_role_and_content_significant():
    m1 = MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content="hi"))
    m2 = MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content="bye"))
    assert not event_content_eq(m1, m2)
    assert event_content_eq(
        m1, MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content="hi"))
    )


# ---- cross-type / source guards ---------------------------------------------


def test_different_types_never_equal():
    a = ActionEvent(thought="t", tool_call=ToolCall(tool_name="shell", arguments={}))
    m = MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content="t"))
    assert not event_content_eq(a, m)


def test_different_source_never_equal():
    m_user = MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content="x"))
    m_agent = MessageEvent(
        source=EventSource.AGENT, message=LLMMessage(role="assistant", content="x")
    )
    assert not event_content_eq(m_user, m_agent)


def test_generic_fallback_for_status_events():
    """Non-special-cased types fall back to dump-comparison minus volatiles."""
    s1 = StatusEvent(status=ConversationStatus.RUNNING)
    s2 = StatusEvent(status=ConversationStatus.RUNNING)
    s3 = StatusEvent(status=ConversationStatus.PAUSED)
    assert event_content_eq(s1, s2)
    assert not event_content_eq(s1, s3)


# helper local to this module
def observation_with(content: str = "out", success: bool = True) -> ObservationEvent:
    return ObservationEvent(
        tool_result=ToolResult(call_id="c", tool_name="shell", success=success, content=content),
        action_id="evt_a",
    )
