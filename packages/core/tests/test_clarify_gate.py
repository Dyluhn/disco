"""RP-13 — Clarify gate: the clarify virtual tool yields a gate, answers resume
planning, and a clarify event is NOT mistaken for a finished plan.

Load-bearing test asserting the engine work-gate behaves correctly.
"""

from __future__ import annotations

from disco.core import (
    ClarifyEvent,
    ClarifyQuestionItem,
    ConversationStatus,
    StatusEvent,
    ToolCall,
)
from disco.core.llm import OperatingMode
from disco.core.loop import AgentStep
from loop_fakes import (
    ScriptedAgent,
    build_loop,
)

CID = "clarify_test"


async def test_clarify_yields_awaiting_user_question():
    """When the agent calls `clarify` with structured questions, the loop halts at
    AWAITING_USER_QUESTION and emits a ClarifyEvent with the typed items."""
    agent = ScriptedAgent(
        [
            AgentStep(
                thought="I need clarification on several points.",
                tool_call=ToolCall(
                    tool_name="clarify",
                    arguments={
                        "question": "I need a few specifics before I can plan.",
                        "questions": [
                            {
                                "id": "color",
                                "question": "What brand color (hex)?",
                                "type": "short_text",
                            },
                            {
                                "id": "stack",
                                "question": "React or Vue?",
                                "type": "choice",
                                "options": ["React", "Vue"],
                            },
                        ],
                    },
                ),
                finished=False,
            ),
        ]
    )
    loop, store = build_loop(
        agent, mode=OperatingMode.PLANNING, planning_tools={"file_read", "file_list"},
        conversation_id=CID,
    )
    await loop.send_message("build a landing page")
    state = await loop.run()

    # The loop should have halted at AWAITING_USER_QUESTION
    assert state.execution_status == ConversationStatus.AWAITING_USER_QUESTION

    events = await store.get_events(CID)
    clarify_events = [e for e in events if isinstance(e, ClarifyEvent)]
    assert len(clarify_events) == 1
    ce = clarify_events[0]
    assert ce.kind == "clarify"
    assert len(ce.items) == 2
    assert ce.items[0].id == "color"
    assert ce.items[0].question == "What brand color (hex)?"
    assert ce.items[0].type == "short_text"
    assert ce.items[1].id == "stack"
    assert ce.items[1].type == "choice"
    assert ce.items[1].options == ["React", "Vue"]

    # The status event should carry the clarify event's id
    statuses = [e for e in events if isinstance(e, StatusEvent)]
    gate_status = [s for s in statuses if s.status == ConversationStatus.AWAITING_USER_QUESTION]
    assert len(gate_status) == 1
    assert gate_status[0].detail == ce.id


async def test_clarify_is_not_mistaken_for_finished_plan():
    """A clarify gate is NOT a plan approval — no plan_event was emitted.
    The conversation should NOT somehow slip into FINISHED."""
    agent = ScriptedAgent(
        [
            AgentStep(
                thought="clarifying before plan",
                tool_call=ToolCall(
                    tool_name="clarify",
                    arguments={
                        "question": "Which color?",
                        "questions": [
                            {"id": "color", "question": "Color?", "type": "short_text"},
                        ],
                    },
                ),
                finished=False,
            ),
        ]
    )
    loop, store = build_loop(
        agent, mode=OperatingMode.PLANNING, planning_tools={"file_read", "file_list"},
        conversation_id=CID,
    )
    await loop.send_message("build a site")
    state = await loop.run()

    # Should be at clarify gate, NOT finished
    assert state.execution_status == ConversationStatus.AWAITING_USER_QUESTION
    assert state.execution_status != ConversationStatus.FINISHED
    assert state.execution_status != ConversationStatus.AWAITING_PLAN_APPROVAL

    events = await store.get_events(CID)
    plan_events = [e for e in events if e.kind == "plan"]
    assert len(plan_events) == 0  # No plan was ever submitted


async def test_answer_resumes_planning():
    """After a clarify gate, the user's answer resumes the loop and planning
    can proceed to a plan submission."""
    agent = ScriptedAgent(
        [
            AgentStep(
                thought="I need specifics.",
                tool_call=ToolCall(
                    tool_name="clarify",
                    arguments={
                        "question": "Details needed",
                        "questions": [
                            {"id": "color", "question": "Brand color?", "type": "short_text"},
                        ],
                    },
                ),
                finished=False,
            ),
            # After the user answers, the agent should propose a plan
            AgentStep(
                thought="Now I can plan.",
                tool_call=ToolCall(
                    tool_name="submit_plan",
                    arguments={
                        "summary": "Build a landing page",
                        "steps": [{"title": "Scaffold page"}],
                    },
                ),
                finished=False,
            ),
        ]
    )
    loop, store = build_loop(
        agent,
        mode=OperatingMode.PLANNING,
        planning_tools={"file_read", "file_list", "submit_plan"},
        plan_tool="submit_plan",
        conversation_id=CID,
    )
    await loop.send_message("build a landing page")

    # First run — should halt at clarify gate
    state = await loop.run()
    assert state.execution_status == ConversationStatus.AWAITING_USER_QUESTION

    # User answers the clarification
    await loop.send_message("The brand color is #FF5733.")
    state = await loop.run()
    # Should now be at plan approval
    assert state.execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL

    events = await store.get_events(CID)
    # Verify the user's answer is in the log
    user_msgs = [
        e
        for e in events
        if e.kind == "message"
        and e.source == "user"
        and "#FF5733" in (e.message.content if hasattr(e, "message") else "")
    ]
    assert len(user_msgs) > 0


async def test_clarify_falls_back_to_free_form_on_empty_questions():
    """When the clarify tool call has no valid questions, the loop falls back
    to a free-form question (AWAITING_USER_QUESTION via MessageEvent)."""
    agent = ScriptedAgent(
        [
            AgentStep(
                thought="Hmm, I need to clarify something.",
                tool_call=ToolCall(
                    tool_name="clarify",
                    arguments={
                        "question": "Can you tell me more about the target audience?",
                        "questions": [],  # empty — should fall back
                    },
                ),
                finished=False,
            ),
        ]
    )
    loop, store = build_loop(
        agent, mode=OperatingMode.PLANNING, planning_tools={"file_read", "file_list"},
        conversation_id=CID,
    )
    await loop.send_message("build a landing page")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.AWAITING_USER_QUESTION
    events = await store.get_events(CID)
    # No ClarifyEvent should have been emitted (empty questions)
    clarify_events = [e for e in events if isinstance(e, ClarifyEvent)]
    assert len(clarify_events) == 0
    # But there should be a MessageEvent with the question
    msg_events = [e for e in events if e.kind == "message"]
    assert any("target audience" in str(e.message.content if hasattr(e, "message") else "")
               for e in msg_events)


def test_reconstruct_clarify_then_ask_does_not_shadow():
    """RECONNECT FIDELITY (Fable gate, rp-13): after an answered clarify gate, a
    LATER free-form ask_user gate must reconstruct with pending_clarify_id=None —
    so the snapshot path renders the AskPanel, not a stale ClarifyPanel. Regression
    guard for the unconditional `pending_clarify_id = last_clarify_id` bug."""
    from disco.core import (
        ConversationState,
        EventSource,
        LLMMessage,
        MessageEvent,
    )

    # 1) A clarify gate (engine stamps the status detail with the clarify id).
    c1 = ClarifyEvent(
        question="A few specifics first.",
        items=[
            ClarifyQuestionItem(id="color", question="Brand color?", type="short_text", options=[])
        ],
    )
    s1 = StatusEvent(status=ConversationStatus.AWAITING_USER_QUESTION, detail=c1.id)
    # 2) The user answers (resumes), then planning produces a later free-form ask.
    ans = MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content="blue"))
    m2 = MessageEvent(
        source=EventSource.AGENT,
        message=LLMMessage(role="assistant", content="One more thing?"),
    )
    # 3) A free-form ask_user gate: detail is the agent MESSAGE id, NOT a clarify id.
    s2 = StatusEvent(status=ConversationStatus.AWAITING_USER_QUESTION, detail=m2.id)

    # At the clarify gate: pending_clarify_id is set.
    at_clarify = ConversationState.reconstruct("cid", [c1, s1])
    assert at_clarify.pending_clarify_id == c1.id
    assert at_clarify.pending_question_id == c1.id

    # At the LATER ask_user gate: clarify must NOT shadow the ask question.
    at_ask = ConversationState.reconstruct("cid", [c1, s1, ans, m2, s2])
    assert at_ask.pending_question_id == m2.id
    assert at_ask.pending_clarify_id is None, (
        "stale clarify id leaked into a later ask_user gate — the snapshot path "
        "would render the wrong (clarify) panel on reconnect"
    )
