"""WF-4 workflow controls and output-contract finish gates."""

from __future__ import annotations

import pytest
from disco.core import (
    ActionEvent,
    ConversationStatus,
    EventSource,
    MessageEvent,
    StatusEvent,
    ToolCall,
)
from disco.core.llm import ToolSpec
from disco.core.loop import AgentStep
from disco.core.workflow import (
    WorkflowDefinition,
    WorkflowOutputContract,
    WorkflowRun,
    WorkflowVerify,
)
from loop_fakes import (
    FakeExecutor,
    ScriptedAgent,
    action_step,
    assert_blocked_question_landing,
    build_loop,
)

pytestmark = pytest.mark.asyncio


def _schema() -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    }


def _workflow_run(
    *,
    path_template: str = "outputs/{name}.md",
    params: dict[str, object] | None = None,
    tools: tuple[str, ...] = (),
    finalizer: str = "finish",
) -> WorkflowRun:
    return WorkflowRun(
        run_id="wf_run_1",
        definition=WorkflowDefinition(
            name="wf",
            card="Write one contracted workflow output file.",
            params_model_schema=_schema(),
            tools=tools,
            output_contract=WorkflowOutputContract(
                path_template=path_template,
                format="markdown",
            ),
            verify=WorkflowVerify(checks=("output_exists",), finalizer=finalizer),
        ),
        params=params or {"name": "report"},
    )


class _WorkflowSandbox:
    workspace_path = None

    def __init__(self, files: set[str] | None = None) -> None:
        self.files = files or set()

    async def file_exists(self, path: str) -> bool:
        return path in self.files


class _WorkflowExecutor(FakeExecutor):
    def __init__(self, *, files: set[str] | None = None) -> None:
        super().__init__(
            tools=[
                ToolSpec(name="skip", description="skip workflow", parameters_schema={}),
                ToolSpec(
                    name="needs_input",
                    description="workflow needs input",
                    parameters_schema={},
                ),
            ]
        )
        self._sandbox = _WorkflowSandbox(files)

    @property
    def sandbox(self) -> _WorkflowSandbox:
        return self._sandbox


def _workflow_control_executor() -> FakeExecutor:
    return FakeExecutor(
        tools=[
            ToolSpec(name="skip", description="skip workflow", parameters_schema={}),
            ToolSpec(
                name="needs_input",
                description="workflow needs input",
                parameters_schema={},
            ),
        ]
    )


def _finish_call(summary: str = "done") -> AgentStep:
    return AgentStep(
        thought=summary,
        tool_call=ToolCall(tool_name="finish", arguments={"summary": summary}),
    )


def _tool_schema(loop: object, name: str) -> dict[str, object]:
    specs = [tool for tool in loop._tools_for_step() if tool.name == name]  # type: ignore[attr-defined]
    assert len(specs) == 1
    return specs[0].parameters_schema


async def test_sealed_workflow_finish_surface_omits_verify_without_shell() -> None:
    loop, _ = build_loop(
        ScriptedAgent([_finish_call()]),
        workflow_run=_workflow_run(finalizer="ready_for_workflow_output"),
        finish_alias="ready_for_workflow_output",
    )

    assert "verify" not in _tool_schema(loop, "finish")["properties"]
    assert "verify" not in _tool_schema(loop, "ready_for_workflow_output")["properties"]


async def test_sealed_workflow_finish_surface_includes_verify_with_shell() -> None:
    loop, _ = build_loop(
        ScriptedAgent([_finish_call()]),
        workflow_run=_workflow_run(tools=("shell",)),
    )

    assert "verify" in _tool_schema(loop, "finish")["properties"]


async def test_sealed_workflow_finish_cannot_smuggle_unapproved_shell_probe() -> None:
    agent = ScriptedAgent(
        [
            AgentStep(
                thought="done",
                tool_call=ToolCall(
                    tool_name="finish",
                    arguments={"summary": "done", "verify": "touch escaped.txt"},
                ),
            )
        ]
    )
    loop, store = build_loop(
        agent,
        executor=_WorkflowExecutor(files={"outputs/report.md"}),
        workflow_run=_workflow_run(),
    )

    await loop.run()

    events = await store.get_events("conv")
    assert not any(
        isinstance(event, ActionEvent) and event.meta.get("verify_probe") for event in events
    )
    ignored = [
        event
        for event in events
        if isinstance(event, MessageEvent)
        and event.meta.get("workflow_finish_verify_ignored") is True
    ]
    assert len(ignored) == 1
    assert any(
        isinstance(event, StatusEvent) and event.status == ConversationStatus.FINISHED
        for event in events
    )


@pytest.mark.parametrize("autonomous", [False, True])
async def test_workflow_skip_finishes_without_output_both_flavors(autonomous: bool) -> None:
    agent = ScriptedAgent(
        [
            action_step("skip", {"reason": "input row was already processed"}),
            AgentStep(thought="Skipped because the input row was already processed."),
        ]
    )
    loop, store = build_loop(
        agent,
        autonomous=autonomous,
        executor=_workflow_control_executor(),
        workflow_run=_workflow_run(),
    )

    await loop.run()

    events = await store.get_events("conv")
    statuses = [e for e in events if isinstance(e, StatusEvent)]
    assert statuses[-1].status == ConversationStatus.FINISHED
    assert statuses[-1].detail == "workflow_skipped"
    assert all(s.detail != "workflow_output_contract_refused" for s in statuses)
    explanations = [
        e
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.AGENT
        and (e.meta or {}).get("workflow_control") == "skip"
    ]
    assert explanations


async def test_workflow_needs_input_interactive_uses_ask_landing() -> None:
    agent = ScriptedAgent(
        [
            action_step(
                "needs_input",
                {
                    "reason": "missing source file",
                    "required_action": "Upload source.csv",
                },
            ),
            AgentStep(thought="I need source.csv before I can continue. Can you upload it?"),
        ]
    )
    loop, store = build_loop(
        agent,
        executor=_workflow_control_executor(),
        workflow_run=_workflow_run(),
    )

    await loop.run()

    events = await store.get_events("conv")
    content = assert_blocked_question_landing(
        events,
        legacy_detail="workflow_needs_input",
    )
    assert "Upload source.csv" in content
    assert any(isinstance(e, StatusEvent) and e.detail == "workflow_needs_input" for e in events)


async def test_workflow_needs_input_autonomous_terminal_explanation() -> None:
    agent = ScriptedAgent(
        [
            action_step(
                "needs_input",
                {
                    "reason": "connector approval missing",
                    "required_action": "Approve the GitHub connector",
                },
            ),
            AgentStep(thought="I need connector approval before continuing."),
        ]
    )
    loop, store = build_loop(
        agent,
        autonomous=True,
        executor=_workflow_control_executor(),
        workflow_run=_workflow_run(),
    )

    await loop.run()

    events = await store.get_events("conv")
    content = assert_blocked_question_landing(
        events,
        legacy_detail="workflow_needs_input",
        flavor="terminal",
    )
    assert "Approve the GitHub connector" in content


async def test_workflow_output_contract_refuses_without_release() -> None:
    agent = ScriptedAgent(
        [
            _finish_call(),
            _finish_call(),
            _finish_call(),
            action_step("skip", {"reason": "output is intentionally unavailable"}),
            AgentStep(thought="Skipped because the output is unavailable."),
        ]
    )
    loop, store = build_loop(
        agent,
        executor=_WorkflowExecutor(files=set()),
        workflow_run=_workflow_run(),
    )

    await loop.run()

    events = await store.get_events("conv")
    statuses = [e for e in events if isinstance(e, StatusEvent)]
    details = [s.detail for s in statuses]
    assert details.count("workflow_output_contract_refused") == 3
    assert "workflow_output_contract_release" not in details
    assert details[-1] == "workflow_skipped"
    assert statuses[-1].status == ConversationStatus.FINISHED
    reminders = [
        e.message.content
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and e.message is not None
        and "finish refused:" in e.message.content
    ]
    assert reminders
    assert "Write it (file_write), then call finish." in reminders[-1]


async def test_workflow_output_contract_finish_passes_when_path_exists() -> None:
    agent = ScriptedAgent([_finish_call()])
    loop, store = build_loop(
        agent,
        executor=_WorkflowExecutor(files={"outputs/report.md"}),
        workflow_run=_workflow_run(),
    )

    await loop.run()

    events = await store.get_events("conv")
    statuses = [e for e in events if isinstance(e, StatusEvent)]
    details = [s.detail for s in statuses]
    assert "workflow_output_contract_passed" in details
    assert "workflow_output_contract_refused" not in details
    assert statuses[-1].status == ConversationStatus.FINISHED
