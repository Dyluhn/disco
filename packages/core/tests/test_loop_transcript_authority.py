"""Transcript invariants at extracted loop collaborator boundaries."""

from disco.core import ActionEvent, ObservationEvent, ToolCall, ToolResult
from disco.core.llm import OperatingMode
from disco.core.loop.observe import Observer, _has_confirmed_prior_read
from disco.core.loop.tool_visibility import ToolVisibility


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
