"""§K — questions_v2 structured pre-plan intake."""

from __future__ import annotations

from disco.core import (
    AgentErrorEvent,
    ConversationStatus,
    PlanEvent,
    QuestionsV2Event,
    StatusEvent,
    ToolCall,
)
from disco.core.llm import OperatingMode
from disco.core.loop import AgentStep
from loop_fakes import ScriptedAgent, action_step, build_loop

CID = "questions_v2_test"


def _questions_step(*, questions: list[dict] | None = None) -> AgentStep:
    return AgentStep(
        thought="I need structured intake before planning.",
        tool_call=ToolCall(
            tool_name="questions_v2",
            arguments={
                "question": "A few details before I propose a plan.",
                "questions": questions
                or [
                    {
                        "id": "style",
                        "question": "Which starting style?",
                        "options": ["Minimal", {"label": "Editorial"}],
                    }
                ],
            },
        ),
        finished=False,
    )


def _submit_plan_step() -> AgentStep:
    return action_step(
        "submit_plan",
        {"summary": "Build the page", "steps": [{"title": "Scaffold the page"}]},
    )


async def test_questions_v2_yields_structured_intake_event_capped_to_four():
    agent = ScriptedAgent(
        [
            _questions_step(
                questions=[
                    {"id": "q1", "question": "Question 1?", "options": ["A"]},
                    {"id": "q2", "question": "Question 2?", "options": ["B"]},
                    {"id": "q3", "question": "Question 3?", "options": ["C"]},
                    {"id": "q4", "question": "Question 4?", "options": ["D"]},
                    {"id": "q5", "question": "Question 5?", "options": ["E"]},
                ]
            )
        ]
    )
    loop, store = build_loop(
        agent,
        mode=OperatingMode.PLANNING,
        planning_tools=frozenset({"submit_plan", "file_read"}),
        conversation_id=CID,
    )

    await loop.send_message("build an ambiguous landing page")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.AWAITING_USER_QUESTION
    events = await store.get_events(CID)
    intake = [e for e in events if isinstance(e, QuestionsV2Event)]
    assert len(intake) == 1
    assert intake[0].kind == "questions_v2"
    assert len(intake[0].items) == 4
    assert "Explore a few options" in intake[0].items[0].options
    assert "Decide for me" in intake[0].items[0].options
    assert "Other" in intake[0].items[0].options
    statuses = [
        e
        for e in events
        if isinstance(e, StatusEvent) and e.status == ConversationStatus.AWAITING_USER_QUESTION
    ]
    assert statuses[-1].detail == intake[0].id
    assert state.pending_questions_v2_id == intake[0].id


async def test_questions_v2_answer_resumes_to_submit_plan():
    agent = ScriptedAgent([_questions_step(), _submit_plan_step()])
    loop, store = build_loop(
        agent,
        mode=OperatingMode.PLANNING,
        planning_tools=frozenset({"submit_plan", "file_read"}),
        conversation_id=CID,
    )

    await loop.send_message("build a landing page")
    state = await loop.run()
    assert state.execution_status == ConversationStatus.AWAITING_USER_QUESTION

    await loop.send_message("Option: Minimal\nDetails: use a focused hero")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL
    plans = [e for e in await store.get_events(CID) if isinstance(e, PlanEvent)]
    assert len(plans) == 1


async def test_questions_v2_allows_only_one_round_before_submit_plan():
    agent = ScriptedAgent([_questions_step(), _questions_step(), _submit_plan_step()])
    loop, store = build_loop(
        agent,
        mode=OperatingMode.PLANNING,
        planning_tools=frozenset({"submit_plan", "file_read"}),
        conversation_id=CID,
    )

    await loop.send_message("build a landing page")
    assert (await loop.run()).execution_status == ConversationStatus.AWAITING_USER_QUESTION

    await loop.send_message("Decide for me")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL
    events = await store.get_events(CID)
    assert len([e for e in events if isinstance(e, QuestionsV2Event)]) == 1
    refusals = [e for e in events if isinstance(e, AgentErrorEvent)]
    assert any("already used the one allowed" in e.error for e in refusals)


def test_autonomous_plan_context_gets_questions_v2_assumptions():
    agent = ScriptedAgent([_submit_plan_step()])
    loop, _ = build_loop(
        agent,
        mode=OperatingMode.PLANNING,
        planning_tools=frozenset({"submit_plan", "file_read"}),
        conversation_id=CID,
        autonomous=True,
    )

    assert "questions_v2" not in {getattr(t, "name", None) for t in loop._tools_for_step()}
    plan = loop._plan_from_args(
        {"summary": "Build the page", "steps": [{"title": "Scaffold"}]},
        [],
    )

    assert "## Assumptions" in plan.context
    assert "questions_v2 structured intake was skipped" in plan.context
