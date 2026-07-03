"""REL-RC-O — dictated quoted content is a finish condition.

Quoted user literals in build prompts/follow-ups are event-derived content
floors. The finish gate refuses while the primary deliverable lacks them, carries
prior revisions forward, and releases loudly at the shared cap discipline.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from disco.core import (
    ConversationStatus,
    EventSource,
    LLMMessage,
    MessageEvent,
    PlanEvent,
    StatusEvent,
    ToolResult,
)
from disco.core.llm import OperatingMode, ToolSpec
from disco.core.loop.finish import _DICTATED_CONTENT_REFUSAL_CAP
from disco.core.loop.plan_conditions import (
    dictated_content_conditions_from_events,
    extract_dictated_content_literals,
)
from disco.core.selection_edit import SourceSelectionRef, build_scoped_edit_directive
from loop_fakes import FakeExecutor, ScriptedAgent, action_step, build_loop


class _Sandbox:
    def __init__(self, root: Path) -> None:
        self.workspace_path = str(root)
        self._root = root

    async def file_exists(self, path: str) -> bool:
        return (self._root / path).is_file()

    async def read_file(self, path: str) -> bytes:
        return (self._root / path).read_bytes()

    async def write_file(self, path: str, data: bytes) -> None:
        target = self._root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    async def list_dir(self, path: str) -> list[str]:
        target = self._root / path
        return [p.name for p in target.iterdir()]


class _FSBuildExecutor(FakeExecutor):
    def __init__(self, root: Path) -> None:
        super().__init__(
            tools=[
                ToolSpec(name="submit_plan", description="plan", parameters_schema={}),
                ToolSpec(name="file_write", description="write", parameters_schema={}),
                ToolSpec(name="shell", description="shell", parameters_schema={}),
            ]
        )
        self.sandbox = _Sandbox(root)
        self.root = root

    async def execute(self, call):
        self.calls.append(call)
        if call.tool_name == "file_write":
            path = self.root / str(call.arguments.get("path") or "")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(call.arguments.get("content") or ""), encoding="utf-8")
        return ToolResult(
            call_id=call.call_id,
            tool_name=call.tool_name,
            success=True,
            content="ok",
        )


def _submit_plan(summary: str = "p") -> object:
    return action_step(
        "submit_plan",
        {
            "summary": summary,
            "steps": [
                {
                    "title": "write artifact",
                    "done_condition": {
                        "kind": "file_exists",
                        "path": "artifact.txt",
                    },
                }
            ],
        },
    )


def _write(content: str) -> object:
    return action_step("file_write", {"path": "artifact.txt", "content": content})


def _finish() -> object:
    return action_step("finish", {"summary": "done"})


def _env_messages(events) -> list[str]:
    return [
        e.message.content
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and e.message is not None
    ]


def _user(text: str, seq: int) -> MessageEvent:
    return MessageEvent(
        source=EventSource.USER,
        message=LLMMessage(role="user", content=text),
    ).model_copy(update={"seq": seq})


def _plan(revision: int, seq: int) -> PlanEvent:
    return PlanEvent(summary=f"rev {revision}", steps=[], revision=revision).model_copy(
        update={"seq": seq}
    )


def _status(detail: str, seq: int) -> StatusEvent:
    return StatusEvent(status=ConversationStatus.RUNNING, detail=detail).model_copy(
        update={"seq": seq}
    )


def _scoped_edit_directive(
    *,
    human_label: str | None,
    instruction: str = "Change it to Midnight Coffee.",
) -> str:
    return build_scoped_edit_directive(
        SourceSelectionRef(oid="index.html:1", file="index.html", line=1),
        instruction,
        human_label=human_label,
    )


def test_extracts_prompt_and_followup_literals_but_skips_commands_and_paths():
    text = (
        'Build a hero "Launch Day" with CTA \'Get Started\', keep "Plans / Pricing", '
        'then run "npm run build" and edit "src/app.js".'
    )
    assert extract_dictated_content_literals(text) == [
        "Launch Day",
        "Get Started",
        "Plans / Pricing",
    ]

    events = [
        _user(text, 1),
        _plan(1, 2),
        _status("plan_approved", 3),
        _user('Change every CTA button to say "Start Now"; run "python -m pytest -q".', 4),
        _status("planning", 5),
        _plan(2, 6),
    ]
    conditions = dictated_content_conditions_from_events(events)
    assert [(c.revision, c.literal) for c in conditions] == [
        (1, "Launch Day"),
        (1, "Get Started"),
        (1, "Plans / Pricing"),
        (2, "Start Now"),
    ]


def test_scoped_edit_supersedes_old_literal_without_harvesting_label_text():
    directive = _scoped_edit_directive(human_label='h1 — "NightOwl Coffee"')
    events = [
        _user('Build a hero titled "NightOwl Coffee" and include "Contact us today".', 1),
        _plan(1, 2),
        _status("plan_approved", 3),
        _user(directive, 4),
        _status("planning", 5),
        _plan(2, 6),
    ]

    conditions = dictated_content_conditions_from_events(events)

    assert [(c.revision, c.literal) for c in conditions] == [
        (1, "Contact us today"),
    ]


def test_label_less_scoped_edit_supersedes_no_prior_literal():
    directive = _scoped_edit_directive(
        human_label=None,
        instruction="Make the selected heading shorter.",
    )
    events = [
        _user('Build a hero titled "NightOwl Coffee".', 1),
        _plan(1, 2),
        _status("plan_approved", 3),
        _user(directive, 4),
        _status("planning", 5),
        _plan(2, 6),
    ]

    conditions = dictated_content_conditions_from_events(events)

    assert [(c.revision, c.literal) for c in conditions] == [
        (1, "NightOwl Coffee"),
    ]


@pytest.mark.asyncio
async def test_finish_refused_until_dictated_literal_lands(tmp_path: Path):
    loop, store = build_loop(
        ScriptedAgent([_submit_plan()]),
        conversation_id="dictated-finish",
        executor=_FSBuildExecutor(tmp_path),
        mode=OperatingMode.PLANNING,
        planning_tools=frozenset({"submit_plan"}),
    )
    await loop.send_message('Build the artifact with button text "Get Started".')
    await loop.run()
    await loop.approve_plan()

    loop.agent = ScriptedAgent(
        [_write("plain content"), _finish(), _write("plain content\nGet Started"), _finish()]
    )
    state = await loop.run()

    events = await store.get_events("dictated-finish")
    env = _env_messages(events)
    assert state.execution_status == ConversationStatus.FINISHED
    assert any("Get Started" in m and "artifact.txt" in m for m in env)
    assert sum("quoted user literal is missing" in m for m in env) == 1


@pytest.mark.asyncio
async def test_finish_gate_allows_selection_edit_to_replace_dictated_literal(
    tmp_path: Path,
):
    loop, store = build_loop(
        ScriptedAgent([_submit_plan("rev1")]),
        conversation_id="dictated-selection-edit",
        executor=_FSBuildExecutor(tmp_path),
        mode=OperatingMode.PLANNING,
        planning_tools=frozenset({"submit_plan"}),
    )
    await loop.send_message('Build the artifact with heading "NightOwl Coffee".')
    await loop.run()
    await loop.approve_plan()

    loop.agent = ScriptedAgent([_write("NightOwl Coffee"), _finish()])
    await loop.run()

    directive = _scoped_edit_directive(human_label='h1 — "NightOwl Coffee"')
    loop.agent = ScriptedAgent([_submit_plan("rev2")])
    await loop.steer(directive)
    await loop.run()
    await loop.approve_plan()

    loop.agent = ScriptedAgent([_write("Midnight Coffee"), _finish()])
    state = await loop.run()

    events = await store.get_events("dictated-selection-edit")
    env = _env_messages(events)
    assert state.execution_status == ConversationStatus.FINISHED
    assert "NightOwl Coffee" not in (tmp_path / "artifact.txt").read_text(encoding="utf-8")
    assert not any("quoted user literal is missing" in m for m in env)


@pytest.mark.asyncio
async def test_prior_revision_dictated_literal_carries_forward(tmp_path: Path):
    loop, store = build_loop(
        ScriptedAgent([_submit_plan("rev1")]),
        conversation_id="dictated-carry",
        executor=_FSBuildExecutor(tmp_path),
        mode=OperatingMode.PLANNING,
        planning_tools=frozenset({"submit_plan"}),
    )
    await loop.send_message('Build the artifact with "Alpha".')
    await loop.run()
    await loop.approve_plan()
    loop.agent = ScriptedAgent([_write("Alpha"), _finish()])
    await loop.run()

    loop.agent = ScriptedAgent([_submit_plan("rev2")])
    await loop.send_message('Add "Beta" to the artifact.')
    await loop.run()
    await loop.approve_plan()
    loop.agent = ScriptedAgent([_write("Alpha Beta"), _finish()])
    await loop.run()

    loop.agent = ScriptedAgent([_submit_plan("rev3")])
    await loop.send_message('Add "Gamma" to the artifact.')
    await loop.run()
    await loop.approve_plan()
    loop.agent = ScriptedAgent(
        [_write("Alpha Gamma"), _finish(), _write("Alpha Beta Gamma"), _finish()]
    )
    state = await loop.run()

    events = await store.get_events("dictated-carry")
    env = _env_messages(events)
    assert state.execution_status == ConversationStatus.FINISHED
    assert any("Beta" in m and "artifact.txt" in m for m in env)
    assert (tmp_path / "artifact.txt").read_text(encoding="utf-8") == "Alpha Beta Gamma"


@pytest.mark.asyncio
async def test_dictated_content_gate_releases_loudly_at_cap(tmp_path: Path):
    loop, store = build_loop(
        ScriptedAgent([_submit_plan()]),
        conversation_id="dictated-cap",
        executor=_FSBuildExecutor(tmp_path),
        mode=OperatingMode.PLANNING,
        planning_tools=frozenset({"submit_plan"}),
    )
    await loop.send_message('Build the artifact with "Get Started".')
    await loop.run()
    await loop.approve_plan()

    loop.agent = ScriptedAgent([_write("missing literal"), _finish()])
    state = await loop.run()

    events = await store.get_events("dictated-cap")
    env = _env_messages(events)
    assert state.execution_status == ConversationStatus.FINISHED
    assert sum("quoted user literal is missing" in m for m in env) == _DICTATED_CONTENT_REFUSAL_CAP
    assert any("Finished despite missing dictated content" in m and "Get Started" in m for m in env)
    assert any(
        isinstance(e, StatusEvent) and e.detail == "dictated_content_release"
        for e in events
    )
