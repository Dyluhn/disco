from __future__ import annotations

from collections.abc import Iterable

import pytest
from disco.agent_server.preview_manager import preview_projection_digest
from disco.agent_server.preview_projection import (
    ActiveLivePreviewProjection,
    derive_active_live_preview_projection,
)
from disco.core import (
    ActionEvent,
    ConversationStatus,
    Event,
    ObservationEvent,
    StatusEvent,
    ToolCall,
    ToolResult,
)

_NAME = "web"
_PORT = 8000
_COMMAND = "PORT=8000 python3 server.py"
_EXEC_DIR = "/workspace"
_INTENT = {
    "serve_dir": None,
    "command": "python3 server.py",
    "framework": None,
    "cwd": None,
    "launch_kind": "custom",
}


def _sequenced(events: Iterable[Event]) -> tuple[Event, ...]:
    return tuple(
        event.model_copy(update={"seq": index}) for index, event in enumerate(events, start=1)
    )


def _start_pair(
    *,
    success: bool = True,
    launch_kind: str = "custom",
    structured_updates: dict[str, object] | None = None,
) -> tuple[ActionEvent, ObservationEvent]:
    call = ToolCall(
        call_id="call_preview_start",
        tool_name="preview_start",
        arguments={"name": _NAME, "command": "python3 server.py"},
    )
    action = ActionEvent(thought="start the app preview", tool_call=call)
    intent = {**_INTENT, "launch_kind": launch_kind}
    digest = preview_projection_digest(
        name=_NAME,
        port=_PORT,
        command=_COMMAND,
        exec_dir=_EXEC_DIR,
        intent=intent,
    )
    assert digest is not None
    structured: dict[str, object] = {
        "status": "running",
        "name": _NAME,
        "port": _PORT,
        "launch_kind": launch_kind,
        "projection_id": f"pv_{'a' * 32}",
        "intent_digest": digest,
        "sandbox_instance_id": "sbx_exact_generation",
        "sandbox_generation": 7,
        "command": _COMMAND,
        "exec_dir": _EXEC_DIR,
        "intent": intent,
    }
    structured.update(structured_updates or {})
    observation = ObservationEvent(
        action_id=action.id,
        tool_result=ToolResult(
            call_id=call.call_id,
            tool_name="preview_start",
            success=success,
            content="preview running" if success else "preview failed",
            structured=structured,
            error=None if success else "launch_failed",
        ),
    )
    return action, observation


def test_valid_custom_start_derives_exact_active_generation() -> None:
    action, observation = _start_pair()
    events = _sequenced((action, observation))

    projection = derive_active_live_preview_projection(events, terminal_seq=3)

    assert projection == ActiveLivePreviewProjection(
        projection_id=f"pv_{'a' * 32}",
        session_name=_NAME,
        port=_PORT,
        launch_kind="custom",
        intent_digest=preview_projection_digest(
            name=_NAME,
            port=_PORT,
            command=_COMMAND,
            exec_dir=_EXEC_DIR,
            intent=_INTENT,
        ),
        sandbox_instance_id="sbx_exact_generation",
        sandbox_generation=7,
        source_action_id=events[0].id,
        source_action_seq=1,
        source_observation_id=events[1].id,
        source_observation_seq=2,
    )


@pytest.mark.parametrize(
    "case",
    ("malformed", "failed", "static", "reordered", "orphan"),
)
def test_untrusted_start_evidence_never_authorizes_projection(case: str) -> None:
    updates: dict[str, object] | None = None
    success = True
    launch_kind = "custom"
    if case == "malformed":
        updates = {"intent_digest": "0" * 64}
    elif case == "failed":
        success = False
    elif case == "static":
        launch_kind = "static"

    action, observation = _start_pair(
        success=success,
        launch_kind=launch_kind,
        structured_updates=updates,
    )
    if case == "reordered":
        # Sequence authority, not caller list order: the observation precedes
        # the action and therefore cannot attest a launch that did not exist.
        events = (
            observation.model_copy(update={"seq": 1}),
            action.model_copy(update={"seq": 2}),
        )
    elif case == "orphan":
        events = (observation.model_copy(update={"seq": 1}),)
    else:
        events = _sequenced((action, observation))

    assert derive_active_live_preview_projection(events, terminal_seq=3) is None


def test_successful_preview_stop_revokes_the_selected_generation() -> None:
    start_action, start_observation = _start_pair()
    stop_call = ToolCall(
        call_id="call_preview_stop",
        tool_name="preview_stop",
        arguments={"name": _NAME},
    )
    stop_action = ActionEvent(thought="stop the preview", tool_call=stop_call)
    stop_observation = ObservationEvent(
        action_id=stop_action.id,
        tool_result=ToolResult(
            call_id=stop_call.call_id,
            tool_name="preview_stop",
            success=True,
            content="preview stopped",
            structured={"stopped": [_NAME]},
        ),
    )
    events = _sequenced((start_action, start_observation, stop_action, stop_observation))

    assert derive_active_live_preview_projection(events, terminal_seq=5) is None


def test_malformed_newer_success_cannot_leave_an_older_preview_authorized() -> None:
    first_action, first_observation = _start_pair()
    second_action, second_observation = _start_pair(
        structured_updates={"name": None},
    )
    events = _sequenced((first_action, first_observation, second_action, second_observation))

    assert derive_active_live_preview_projection(events, terminal_seq=5) is None


def test_previous_finished_revision_cannot_authorize_the_current_revision() -> None:
    action, observation = _start_pair()
    finished = StatusEvent(status=ConversationStatus.FINISHED)
    events = _sequenced((action, observation, finished))

    assert derive_active_live_preview_projection(events, terminal_seq=4) is None
