"""Engine-rekick fix (PART 1) — `signals.has_unprocessed_user_message` must NOT
count a PURE terminal/status marker as the progress that "consumes" a user turn.

Root cause this guards: the engine `AgentLoop._run` appends the terminal marker
(FINISHED/STUCK/ERROR) at run end. A follow-up appended while the run finalizes
lands just BEFORE that marker. The old predicate counted RUNNING/FINISHED/STUCK/
ERROR StatusEvents as "progress", so the marker masked the follow-up as already
processed forever — the run() re-entry + the runtime re-kick both no-op'd and the
turn was stranded. With status markers excluded, only REAL work (tool calls /
observations / plan events / assistant messages) consumes a turn, so a follow-up
after a terminal marker (no real work since) is correctly UNPROCESSED.
"""

from __future__ import annotations

from disco.core import (
    ActionEvent,
    ConversationStatus,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    PlanEvent,
    StatusEvent,
    ToolCall,
    ToolResult,
)
from disco.core.events import PlanStep
from disco.core.loop import signals


def _seq(events: list[Event]) -> list[Event]:
    """Assign monotonically increasing seqs (the store does this on append)."""
    return [e.model_copy(update={"seq": i + 1}) for i, e in enumerate(events)]


def _user(text: str = "do the thing") -> MessageEvent:
    return MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content=text))


def _agent(text: str = "working on it") -> MessageEvent:
    return MessageEvent(
        source=EventSource.AGENT, message=LLMMessage(role="assistant", content=text)
    )


def _action(tool: str = "file_write") -> ActionEvent:
    return ActionEvent(
        thought="acting",
        tool_call=ToolCall(tool_name=tool, arguments={"path": "x", "content": "y"}),
    )


def _obs(tool: str = "file_write") -> ObservationEvent:
    return ObservationEvent(
        tool_result=ToolResult(call_id="c1", tool_name=tool, success=True, content="ok"),
        action_id="a1",
    )


def _plan() -> PlanEvent:
    return PlanEvent(summary="plan", steps=[PlanStep(title="step 1")], revision=1)


def _status(status: ConversationStatus, detail: str | None = None) -> StatusEvent:
    return StatusEvent(status=status, detail=detail)


def _reminder() -> MessageEvent:
    # A synthetic system-reminder injection — role="user" in the LLM view but
    # EventSource.ENVIRONMENT, so it is NOT a real user turn.
    return MessageEvent(
        source=EventSource.ENVIRONMENT,
        message=LLMMessage(
            role="user",
            content="<system-reminder>\n[F9 dedup: file_read(x)] …\n</system-reminder>",
        ),
    )


# --- un-masking: terminal marker after the user turn does NOT consume it -------


def test_followup_after_finished_is_unprocessed():
    events = _seq(
        [_action(), _obs(), _user("now also add a footer"), _status(ConversationStatus.FINISHED)]
    )
    assert signals.has_unprocessed_user_message(events) is True


def test_followup_after_stuck_is_unprocessed():
    events = _seq([_action(), _user("change the title"), _status(ConversationStatus.STUCK)])
    assert signals.has_unprocessed_user_message(events) is True


def test_followup_after_error_is_unprocessed():
    events = _seq([_action(), _user("try a different approach"), _status(ConversationStatus.ERROR)])
    assert signals.has_unprocessed_user_message(events) is True


def test_followup_after_running_marker_is_unprocessed():
    # The engine emits RUNNING at entry; that pure status marker must not mask a
    # follow-up either.
    events = _seq([_action(), _user("and fix the colors"), _status(ConversationStatus.RUNNING)])
    assert signals.has_unprocessed_user_message(events) is True


# --- negative: a turn that WAS processed (real work since) is NOT unprocessed --


def test_processed_turn_is_not_unprocessed():
    events = _seq([_user("do it"), _action(), _obs(), _status(ConversationStatus.FINISHED)])
    assert signals.has_unprocessed_user_message(events) is False


def test_agent_reply_after_user_is_not_unprocessed():
    events = _seq(
        [_user("question?"), _agent("here is the answer"), _status(ConversationStatus.FINISHED)]
    )
    assert signals.has_unprocessed_user_message(events) is False


def test_plan_event_after_user_is_not_unprocessed():
    # A plan emitted in response to the user counts as real work.
    events = _seq([_user("build a site"), _plan(), _status(ConversationStatus.RUNNING)])
    assert signals.has_unprocessed_user_message(events) is False


# --- synthetic no-op turns must NOT count as processing the user turn ----------


def test_system_reminder_after_user_does_not_process_the_turn():
    # A system-reminder / F9 injection lands after the follow-up but is NOT real
    # work — the user turn stays UNPROCESSED.
    events = _seq(
        [
            _action(),
            _obs(),
            _user("also add dark mode"),
            _reminder(),
            _status(ConversationStatus.FINISHED),
        ]
    )
    assert signals.has_unprocessed_user_message(events) is True


def test_no_user_message_is_not_unprocessed():
    events = _seq([_action(), _obs(), _status(ConversationStatus.FINISHED)])
    assert signals.has_unprocessed_user_message(events) is False


# --- latest_unprocessed_user_text stays consistent with the fixed basis -------


def test_latest_unprocessed_user_text_reads_the_stranded_followup():
    events = _seq(
        [_action(), _obs(), _user("add a footer please"), _status(ConversationStatus.FINISHED)]
    )
    assert signals.latest_unprocessed_user_text(events) == "add a footer please"


def test_latest_unprocessed_user_text_none_when_processed():
    events = _seq([_user("do it"), _action(), _obs(), _status(ConversationStatus.FINISHED)])
    assert signals.latest_unprocessed_user_text(events) is None
