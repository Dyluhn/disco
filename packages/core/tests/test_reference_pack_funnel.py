"""PKG-47: packs the user selected are required reading before the first plan.

Drives the REAL AgentLoop (scripted model, in-memory tools). A submit_plan that
arrives before every bound references/<pack>/PACK.md was read is turned back
into reading with a system reminder; once read (or after two reminders) the
plan is accepted as usual. No binding → no funnel."""

from __future__ import annotations

from _buildsoak_fakes import BuildExecutor, build_plan_loop
from disco.core import EventSource, LLMMessage, MessageEvent, PlanEvent
from disco.core.events import ConversationStatus
from disco.core.loop.plan_validation import (
    REFERENCE_PACK_UNREAD_DIAGNOSTIC,
    unread_reference_pack_indexes,
)
from loop_fakes import ScriptedAgent, action_step

_MARKERS = [
    {
        "pack_id": "rp_1",
        "name": "Brand kit",
        "digest": "d",
        "path": "references/brand-kit/PACK.md",
        "file_count": 2,
    },
    {
        "pack_id": "rp_2",
        "name": "Spec",
        "digest": "e",
        "path": "references/spec/PACK.md",
        "file_count": 1,
    },
]


def _binding_event() -> MessageEvent:
    return MessageEvent(
        source=EventSource.ENVIRONMENT,
        message=LLMMessage(role="user", content="Reference packs selected …"),
        meta={"reference_packs": _MARKERS},
    )


def _plan(summary: str = "plan"):
    return action_step("submit_plan", {"summary": summary, "steps": [{"title": "do the work"}]})


def _reminders(events) -> list[MessageEvent]:
    return [
        e
        for e in events
        if isinstance(e, MessageEvent)
        and e.meta.get("diagnostic") == REFERENCE_PACK_UNREAD_DIAGNOSTIC
    ]


async def test_early_plan_is_turned_back_into_reading_then_accepted() -> None:
    agent = ScriptedAgent(
        [
            _plan("too early"),
            action_step("file_read", {"path": "./references/brand-kit/PACK.md"}),
            action_step("file_read", {"path": "references/spec/PACK.md"}),
            _plan("after reading"),
        ]
    )
    loop, store = build_plan_loop(agent, conversation_id="rp-funnel-read", executor=BuildExecutor())
    await store.append(loop.conversation_id, _binding_event())
    await loop.send_message("Build the landing page from my brand kit.")
    state = await loop.run()
    assert state.execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL
    events = await store.get_events(loop.conversation_id)
    reminders = _reminders(events)
    assert len(reminders) == 1
    assert "references/brand-kit/PACK.md" in reminders[0].message.content
    plans = [e for e in events if isinstance(e, PlanEvent)]
    assert [p.summary for p in plans] == ["after reading"]
    assert unread_reference_pack_indexes(events) == []


async def test_reminders_are_capped_then_the_plan_is_accepted_not_terminated() -> None:
    agent = ScriptedAgent([_plan("1"), _plan("2"), _plan("3")])
    loop, store = build_plan_loop(agent, conversation_id="rp-funnel-cap", executor=BuildExecutor())
    await store.append(loop.conversation_id, _binding_event())
    await loop.send_message("Build it.")
    state = await loop.run()
    assert state.execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL
    events = await store.get_events(loop.conversation_id)
    assert len(_reminders(events)) == 2
    assert [p.summary for p in events if isinstance(p, PlanEvent)] == ["3"]


async def test_no_binding_means_no_funnel() -> None:
    agent = ScriptedAgent([_plan("direct")])
    loop, store = build_plan_loop(agent, conversation_id="rp-funnel-none", executor=BuildExecutor())
    await loop.send_message("Build it.")
    state = await loop.run()
    assert state.execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL
    events = await store.get_events(loop.conversation_id)
    assert _reminders(events) == []


async def test_an_empty_binding_clears_the_requirement() -> None:
    agent = ScriptedAgent([_plan("direct")])
    loop, store = build_plan_loop(
        agent, conversation_id="rp-funnel-clear", executor=BuildExecutor()
    )
    await store.append(loop.conversation_id, _binding_event())
    await store.append(
        loop.conversation_id,
        MessageEvent(
            source=EventSource.ENVIRONMENT,
            message=LLMMessage(
                role="user", content="No reference packs are selected for this build."
            ),
            meta={"reference_packs": []},
        ),
    )
    await loop.send_message("Build it.")
    await loop.run()
    assert _reminders(await store.get_events(loop.conversation_id)) == []
