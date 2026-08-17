"""Transcript invariants at extracted loop collaborator boundaries."""

from __future__ import annotations

import ast
import asyncio
import inspect
import textwrap
from pathlib import Path

from disco.core import (
    ActionEvent,
    AgentErrorEvent,
    Event,
    ObservationEvent,
    ToolCall,
    ToolResult,
)
from disco.core.llm import OperatingMode
from disco.core.loop.control import Disp
from disco.core.loop.conversation_controls import ConversationControls
from disco.core.loop.observe import Observer, _has_confirmed_prior_read
from disco.core.loop.tool_visibility import ToolVisibility
from disco.core.loop.transition import TransitionCoordinator

_LOOP_ROOT = Path(inspect.getfile(TransitionCoordinator)).resolve().parent


def _action(tool_name: str, path: str) -> ActionEvent:
    return ActionEvent(
        thought="test",
        tool_call=ToolCall(tool_name=tool_name, arguments={"path": path}),
    )


def test_provenance_search_skips_newer_unconfirmed_matching_action() -> None:
    path = "src/app.py"
    confirmed_read = _action("file_read", path)
    assert confirmed_read.tool_call is not None
    confirmation = ObservationEvent(
        action_id=confirmed_read.id,
        tool_result=ToolResult(
            call_id=confirmed_read.tool_call.call_id,
            tool_name="file_read",
            success=True,
            content="bytes",
        ),
    )
    unconfirmed_read = _action("file_read", path)
    current_write = _action("file_write", path)

    assert _has_confirmed_prior_read(
        [confirmed_read, confirmation, unconfirmed_read, current_write],
        path,
        before_id=current_write.id,
    )


async def test_observer_execution_honors_bound_restart_override() -> None:
    class Sandbox:
        generation = 1

    class Executor:
        sandbox = Sandbox()

        async def execute(self, _call: ToolCall) -> ToolResult:
            raise RuntimeError("expected")

    class Loop:
        _assist = False
        executor = Executor()

        def __init__(self) -> None:
            self.events: list[object] = []

        async def _events(self) -> list[object]:
            # `_events` is declared on LoopEventPort, which `_prepare_observation`
            # is typed against; this fake simply never provided it, because the
            # assist gate returned before reaching it. The freshness memo (A11 §1)
            # is not assist-gated, so the declared method is now actually called.
            return list(self.events)

        async def _prepare_executor(self) -> None:
            return None

        async def _emit(self, event: object) -> object:
            self.events.append(event)
            return event

    class RecordingObserver(Observer):
        def __init__(self, loop: Loop) -> None:
            self._loop = loop
            self.restarts: list[tuple[object | None, int]] = []

        async def maybe_emit_sandbox_restart(
            self,
            sandbox: object | None,
            generation_before: int,
        ) -> None:
            self.restarts.append((sandbox, generation_before))

    loop = Loop()
    observer = RecordingObserver(loop)
    await observer.execute_and_observe(_action("shell", "ignored"))

    assert observer.restarts == [(loop.executor.sandbox, 1)]


def test_planning_visibility_retains_anonymous_legacy_tool_when_capability_unknown() -> None:
    anonymous_tool = object()

    class Executor:
        @staticmethod
        def available_tools() -> list[object]:
            return [anonymous_tool]

    class Loop:
        executor = Executor()
        mode = OperatingMode.PLANNING
        _planning_tools = frozenset()
        _plan_tool = "submit_plan"
        _autonomous = True

    visibility = ToolVisibility(Loop())

    assert anonymous_tool in visibility.tools_for_step(mode=OperatingMode.PLANNING)


def test_pause_requests_boundary_without_publishing_status() -> None:
    tree = ast.parse(textwrap.dedent(inspect.getsource(ConversationControls.pause)))
    calls = {
        ast.unparse(node.func)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }

    assert "self._loop._pause_requested.set" in calls
    assert "self._loop._retry_interrupt.set" in calls
    assert all(not call.endswith("._emit") for call in calls)


def test_extracted_dispatch_gates_declare_disp_contracts() -> None:
    gate_files = (
        "meta_tool_common.py",
        "phase_gates.py",
        "planning_gates.py",
        "replanning.py",
    )
    offenders: list[str] = []
    found = 0
    for name in gate_files:
        tree = ast.parse((_LOOP_ROOT / name).read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not (node.name.startswith("gate_") or node.name.startswith("_gate_")):
                continue
            found += 1
            annotation = ast.unparse(node.returns) if node.returns is not None else ""
            if "Disp" not in annotation:
                offenders.append(f"{name}:{node.lineno}:{node.name}:{annotation or 'missing'}")
            if any(
                isinstance(child, ast.Return) and child.value is None
                for child in ast.walk(node)
            ):
                offenders.append(f"{name}:{node.lineno}:{node.name}:bare-return")

    assert found >= 10
    assert offenders == []


async def test_selected_actions_close_before_the_next_action() -> None:
    class Driver:
        @staticmethod
        def planning_allowed_tool_names() -> frozenset[str]:
            return frozenset()

    class Loop:
        mode = OperatingMode.LONG_HORIZON
        _driver = Driver()

        def __init__(self) -> None:
            self._lock = asyncio.Lock()
            self.events: list[Event] = []

        async def _emit(self, event: Event) -> Event:
            self.events.append(event)
            return event

        async def _execute_and_observe(self, action: ActionEvent) -> None:
            assert action.tool_call is not None
            if len([event for event in self.events if isinstance(event, ActionEvent)]) == 1:
                outcome: Event = ObservationEvent(
                    action_id=action.id,
                    tool_result=ToolResult(
                        call_id=action.tool_call.call_id,
                        tool_name=action.tool_call.tool_name,
                        success=True,
                        content="ok",
                    ),
                )
            else:
                outcome = AgentErrorEvent(
                    error="expected",
                    action_id=action.id,
                    tool_call_id=action.tool_call.call_id,
                )
            self.events.append(outcome)

        async def _maybe_apply_read_churn_valve(self, _action: ActionEvent) -> Disp:
            return Disp.FALLTHROUGH

    loop = Loop()
    transition = TransitionCoordinator(loop)  # type: ignore[arg-type]
    first = _action("file_read", "first.py")
    second = _action("file_write", "second.py")

    assert await transition._execute_selected_action(first) is Disp.FALLTHROUGH
    assert await transition._execute_selected_action(second) is Disp.FALLTHROUGH
    assert [type(event) for event in loop.events] == [
        ActionEvent,
        ObservationEvent,
        ActionEvent,
        AgentErrorEvent,
    ]
    assert loop.events[1].action_id == first.id  # type: ignore[union-attr]
    assert loop.events[3].action_id == second.id  # type: ignore[union-attr]
