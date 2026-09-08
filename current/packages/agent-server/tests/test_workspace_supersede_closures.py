"""Which unpaired actions a newer instruction is allowed to supersede.

A follow-up on a finished deep-research report is a workspace ingress, and the
ingress closes every unpaired ``ActionEvent`` as ``execution_superseded``. On a
research conversation that is every turn counter, phase marker, token heartbeat
and section checkpoint the server wrote to narrate its own run — 303 of them on
the largest saved report, appended in one burst, which is 309 frames into a
256-slot subscriber queue and closes every listening socket with ``1013 event
stream fell behind``.

Deep research's trace actions were already exempt on the kill path
(``run_kill_service``) and the resume path (``resume_service``); this is the
third control that closes admitted-but-unpaired actions, and it did not know.
"""

from __future__ import annotations

from disco.agent_server.workspace_mutations import _WorkspaceMutations
from disco.core import ActionEvent, Event, ObservationEvent, ToolCall, ToolResult
from disco.retrieval.deep_research import RESEARCH_TRACE_ACTIONS


def _action(tool_name: str) -> ActionEvent:
    return ActionEvent(
        thought=f"{tool_name} marker",
        tool_call=ToolCall(tool_name=tool_name, arguments={}),
    )


def _observation(action: ActionEvent) -> ObservationEvent:
    return ObservationEvent(
        action_id=action.id,
        tool_result=ToolResult(
            call_id=action.tool_call.call_id,
            tool_name=action.tool_call.tool_name,
            success=True,
            content="done",
        ),
    )


def test_a_research_run_narrating_itself_is_not_superseded_by_a_follow_up() -> None:
    """The whole trace vocabulary dangles by design and closes to nothing."""
    history: list[Event] = [_action(name) for name in sorted(RESEARCH_TRACE_ACTIONS)]

    assert _WorkspaceMutations._dangling_action_closures(history) == []


def test_one_real_dangling_tool_call_among_the_trace_still_closes() -> None:
    """303 trace markers plus one interrupted tool call yields ONE closure."""
    unpaired_tool_call = _action("write_file")
    history: list[Event] = [
        *(_action("turn") for _ in range(168)),
        *(_action("model_activity") for _ in range(87)),
        *(_action("phase") for _ in range(48)),
        unpaired_tool_call,
    ]

    closures = _WorkspaceMutations._dangling_action_closures(history)

    assert len(closures) == 1
    assert closures[0].action_id == unpaired_tool_call.id
    assert closures[0].tool_call_id == unpaired_tool_call.tool_call.call_id
    assert closures[0].error == "execution_superseded"


def test_a_tool_call_that_already_has_its_result_is_not_superseded() -> None:
    """The pairing rule the trace exemption sits beside is unchanged."""
    paired = _action("read_file")
    history: list[Event] = [paired, _observation(paired)]

    assert _WorkspaceMutations._dangling_action_closures(history) == []
