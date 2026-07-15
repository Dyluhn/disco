"""Regression tests for plan-approved mode desync at turn composition."""

from __future__ import annotations

import pytest
from _buildsoak_fakes import PROD_PLANNING_TOOLS, BuildExecutor, build_plan_loop
from disco.core import ConversationStatus, PlanEvent, PlanStep, StatusEvent
from disco.core.llm import (
    DefaultLLMRouter,
    DriverPrompts,
    OperatingMode,
    ProposedToolCall,
)
from disco.core.loop import BuildAgent
from llm_fakes import simple_config
from loop_fakes import SequenceProvider

CID = "conv-mode-desync"


def _tool(name: str, arguments: dict) -> ProposedToolCall:
    return ProposedToolCall(tool_name=name, arguments=arguments)


def _submit_plan_tool() -> ProposedToolCall:
    return _tool(
        "submit_plan",
        {
            "summary": "Build the repair compendium.",
            "steps": [
                {
                    "title": "Scaffold the file layout: index.html, styles, and JS modules",
                    "done_condition": {"kind": "file_exists", "path": "index.html"},
                },
                {"title": "Wire storage and editor interactions"},
            ],
            "context": "Workspace is empty; build a client-side app.",
        },
    )


def _questions_v2_tool() -> ProposedToolCall:
    return _tool(
        "questions_v2",
        {
            "question": "A couple of choices will shape the compendium.",
            "questions": [
                {
                    "id": "writing",
                    "question": "How should each section be written?",
                    "options": [
                        "Explore a few options",
                        "Decide for me",
                        "Other",
                    ],
                }
            ],
        },
    )


def _execution_write_tool() -> ProposedToolCall:
    return _tool("file_write", {"path": "index.html", "content": "<main></main>"})


def _build_agent(script: list[dict]) -> tuple[BuildAgent, SequenceProvider]:
    provider = SequenceProvider(script)
    router = DefaultLLMRouter(
        simple_config(),
        {"ollama": provider, "openrouter": provider},
        prompt_provider=DriverPrompts(),
    )
    return BuildAgent(router, conversation_id=CID), provider


async def _assert_next_driver_request_is_execution(loop, store, provider) -> None:
    # Simulate the runtime/fresh-loop failure mode: event log says the plan was
    # approved, but the in-memory cache has booted or drifted back to PLANNING.
    loop.mode = OperatingMode.PLANNING
    events = await store.get_events(CID)
    assert any(
        isinstance(event, StatusEvent) and event.detail == "plan_approved" for event in events
    )
    view = await loop._materialize_view(events)
    assert any("<current-objective>" in (message.content or "") for message in view.messages)

    step, _disp = await loop._driver.drive_step(view, events)
    assert step is not None

    req = provider.seen[-1]
    assert req.profile.mode != OperatingMode.PLANNING
    assert req.profile.mode == OperatingMode.LONG_HORIZON
    assert req.messages[0].role == "system"
    assert "currently in PLANNING mode" not in req.messages[0].content
    assert "The user has APPROVED your plan" in req.messages[0].content
    tool_names = {tool.name for tool in req.tools or []}
    assert "file_write" in tool_names
    assert "submit_plan" not in tool_names


@pytest.mark.asyncio
async def test_questions_v2_revision_approval_composes_execution_from_event_log():
    agent, provider = _build_agent(
        [
            {"text": "intake", "tool_calls": [_questions_v2_tool()]},
            {"text": "grounding", "tool_calls": [_tool("think", {"thought": "grounded"})]},
            {"text": "plan", "tool_calls": [_submit_plan_tool()]},
            {"text": "write", "tool_calls": [_execution_write_tool()]},
        ]
    )
    loop, store = build_plan_loop(agent, conversation_id=CID, executor=BuildExecutor())

    await loop.send_message("build a GPU repair compendium")
    assert (await loop.run()).execution_status == ConversationStatus.AWAITING_USER_QUESTION

    await loop.steer("Both rich text and markdown, with dark/light theme controls.")
    assert (await loop.run()).execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL
    await loop.approve_plan()

    await _assert_next_driver_request_is_execution(loop, store, provider)


@pytest.mark.asyncio
async def test_plain_plan_approval_composes_execution_even_with_stale_planning_cache():
    agent, provider = _build_agent(
        [
            {"text": "plan", "tool_calls": [_submit_plan_tool()]},
            {"text": "write", "tool_calls": [_execution_write_tool()]},
        ]
    )
    loop, store = build_plan_loop(agent, conversation_id=CID, executor=BuildExecutor())

    await loop.send_message("build a GPU repair compendium")
    assert (await loop.run()).execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL
    await loop.approve_plan()

    await _assert_next_driver_request_is_execution(loop, store, provider)


@pytest.mark.asyncio
async def test_autonomous_plan_approval_composes_execution_from_event_log():
    agent, provider = _build_agent(
        [
            {"text": "write", "tool_calls": [_execution_write_tool()]},
        ]
    )
    loop, store = build_plan_loop(agent, conversation_id=CID, executor=BuildExecutor())
    loop._autonomous = True
    loop._planning_tools = PROD_PLANNING_TOOLS

    await loop.send_message("build a GPU repair compendium")
    plan = PlanEvent(
        summary="Build the repair compendium.",
        steps=[PlanStep(title="Scaffold the file layout: index.html, styles, and JS modules")],
        revision=1,
    )
    await loop._emit(plan)
    await loop._route_plan_approval_gate(plan)

    await _assert_next_driver_request_is_execution(loop, store, provider)
