"""[REL-RC-B] fresh_read_autoground_target — auto-read trigger for the FRESH_READ_REQUIRED loop."""
from __future__ import annotations

from disco.core.events import (
    ActionEvent,
    AgentErrorEvent,
    EventSource,
    LLMMessage,
    MessageEvent,
    ConversationStatus,
    ObservationEvent,
    StatusEvent,
    ToolCall,
    ToolResult,
)
from disco.core.loop import signals


def _edit(path, aid):
    return ActionEvent(id=aid, thought="t", tool_call=ToolCall(tool_name="file_edit", arguments={"path": path}))


def _fresh(aid):
    return AgentErrorEvent(error="FRESH_READ_REQUIRED", action_id=aid)


def _user(txt="go"):
    return MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content=txt))


def _ok_obs(aid="x"):
    return ObservationEvent(tool_result=ToolResult(call_id="c", tool_name="file_read", success=True, content="ok"), action_id=aid)


def test_two_same_path_fresh_triggers():
    ev = [_user(), _edit("index.html", "a1"), _fresh("a1"), _edit("index.html", "a2"), _fresh("a2")]
    assert signals.fresh_read_autoground_target(ev) == "index.html"


def test_single_fresh_does_not_trigger():
    ev = [_user(), _edit("index.html", "a1"), _fresh("a1")]
    assert signals.fresh_read_autoground_target(ev) is None


def test_marker_already_present_blocks_reinjection():
    # already auto-read index.html this revision → even with a fresh streak, do NOT re-inject
    ev = [
        _user(),
        StatusEvent(status=ConversationStatus.RUNNING, detail="auto_ground_read:index.html"),
        _ok_obs(),  # the injected read's success obs (resets streak but NOT the marker)
        _edit("index.html", "a3"), _fresh("a3"),
        _edit("index.html", "a4"), _fresh("a4"),
    ]
    assert signals.fresh_read_autoground_target(ev) is None


def test_different_paths_no_premature_trigger():
    # one fresh on each of two paths → neither reaches 2 → no trigger
    ev = [_user(), _edit("a.html", "a1"), _fresh("a1"), _edit("b.html", "a2"), _fresh("a2")]
    assert signals.fresh_read_autoground_target(ev) is None


def test_success_obs_resets_streak():
    # a successful obs between the two fresh errors resets the streak → only 1 counted → no trigger
    ev = [_user(), _edit("index.html", "a1"), _fresh("a1"), _ok_obs(), _edit("index.html", "a2"), _fresh("a2")]
    assert signals.fresh_read_autoground_target(ev) is None  # obs reset → only 1 fresh after it
