"""F-3: post-FINISHED 'ship-it / accept-as-is' intent must NOT force a re-plan.

After a build FINISHES, a user message that clearly means "stop, we're done"
should echo the user text to the log but leave the conversation FINISHED —
no enter_planning(), no RUNNING/planning status, no PlanEvent, no task kick.
Real change/deploy intents must still fall through to the existing replan path.
The guard is a no-op when the conversation is NOT FINISHED.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from disco.agent_server.control_ops import ControlOps, _is_ship_it_intent
from disco.core import (
    ConversationStatus,
    EventSource,
    LLMMessage,
    MessageEvent,
    SqliteEventStore,
    StatusEvent,
)
from disco.core.events import PlanEvent

CID = "conv-f3-test"


# ---- helpers -----------------------------------------------------------------


def _make_rt(store: SqliteEventStore) -> MagicMock:
    """Minimal mock runtime: real store, trackable _loop_for + kick."""
    rt = MagicMock()
    rt._store = store
    rt._loop_for.return_value.enter_planning = AsyncMock()
    rt.kick = MagicMock()
    return rt


async def _finished_conversation(store: SqliteEventStore) -> None:
    """Seed a conversation with a FINISHED terminal status."""
    store.create_conversation(CID, owner_id="local")
    await store.append(
        CID,
        MessageEvent(
            source=EventSource.USER,
            message=LLMMessage(role="user", content="build a site"),
        ),
    )
    await store.append(CID, StatusEvent(status=ConversationStatus.FINISHED))


# ---- _is_ship_it_intent unit tests ------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "publish it and be done",
        "Publish It And Be Done",
        "  publish it and be done  ",
        "leave it as is",
        "leave it as-is",
        "you're done",
        "you are done",
        "that's done",
        "we're done",
        "mark it done",
        "mark it as done",
        "mark it complete",
        "mark as complete",
        "it's done",
        "call it done",
    ],
)
def test_is_ship_it_intent_matches_stop_phrases(text: str) -> None:
    """Every phrase on the authorized stop-list must match (case-insensitive,
    stripped)."""
    assert _is_ship_it_intent(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "publish it",  # ambiguous — bare publish is NOT a stop phrase
        "publish",
        "deploy it",
        "publish to Netlify",
        "add a dark mode toggle",
        "fix the header",
        "change the color to blue",
        "we're done, but also add a footer",
        "",
        "   ",
    ],
)
def test_is_ship_it_intent_rejects_ambiguous_and_change_intents(text: str) -> None:
    """Bare 'publish', real change intents, and empty strings must NOT match."""
    assert _is_ship_it_intent(text) is False


# ---- request_plan: ship-it path (FINISHED + stop phrase) --------------------


async def test_ship_it_post_finish_appends_message_no_planning_status() -> None:
    """'publish it and be done' on a FINISHED conversation: user message is
    appended (UI echo resolves), but NO planning status and NO re-plan."""
    store = SqliteEventStore(":memory:")
    await _finished_conversation(store)
    rt = _make_rt(store)
    ops = ControlOps(rt)

    await ops.request_plan(CID, "publish it and be done")

    events = await store.get_events(CID)
    # The user's echo must appear in the log.
    user_msgs = [
        e
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.USER
        and e.message.content == "publish it and be done"
    ]
    assert len(user_msgs) == 1, "ship-it text must be appended as a user message"

    # No RUNNING/planning status must have been emitted.
    planning_statuses = [
        e for e in events if isinstance(e, StatusEvent) and e.status == ConversationStatus.RUNNING
    ]
    assert not planning_statuses, "no RUNNING/planning status on ship-it path"

    # The conversation must still be FINISHED.
    state = await store.get_state(CID)
    assert state.execution_status == ConversationStatus.FINISHED

    # No PlanEvent (no revision-2 plan).
    plan_events = [e for e in events if isinstance(e, PlanEvent)]
    assert not plan_events, "no PlanEvent on ship-it path"

    # enter_planning and kick must NOT have been called.
    rt._loop_for.return_value.enter_planning.assert_not_awaited()
    rt.kick.assert_not_called()


async def test_ship_it_case_insensitive() -> None:
    """Ship-it check is case-insensitive — 'YOU'RE DONE' must be caught."""
    store = SqliteEventStore(":memory:")
    await _finished_conversation(store)
    rt = _make_rt(store)
    ops = ControlOps(rt)

    await ops.request_plan(CID, "YOU'RE DONE")

    state = await store.get_state(CID)
    assert state.execution_status == ConversationStatus.FINISHED
    rt.kick.assert_not_called()


async def test_ship_it_all_stop_phrases_skip_replan() -> None:
    """Every stop phrase (both substring + exact sets) must skip enter_planning + kick,
    INCLUDING the real traced phrasing with a leading clause (codex MAJOR), and a
    trailing-punctuation / extra-whitespace variant."""
    from disco.agent_server.control_ops import _SHIP_IT_CONTAINS, _SHIP_IT_EXACT

    phrases = [
        *_SHIP_IT_CONTAINS,
        *_SHIP_IT_EXACT,
        "stop troubleshooting, publish it and be done",  # the actual trace phrase
        "mark it as complete",  # codex-flagged miss
        "you're done!",  # trailing punctuation
        "  leave it as is  ",  # whitespace
    ]
    for phrase in phrases:
        store = SqliteEventStore(":memory:")
        await _finished_conversation(store)
        rt = _make_rt(store)
        ops = ControlOps(rt)

        await ops.request_plan(CID, phrase)

        rt.kick.assert_not_called(), f"kick must not be called for phrase '{phrase}'"
        rt._loop_for.return_value.enter_planning.assert_not_awaited()


async def test_mid_sentence_stop_phrase_still_replans() -> None:
    """A short stop phrase embedded in a real change request must NOT skip replan —
    'you're done with X, now add Y' is a change request, not a ship-it."""
    store = SqliteEventStore(":memory:")
    await _finished_conversation(store)
    rt = _make_rt(store)
    ops = ControlOps(rt)
    await ops.request_plan(CID, "you're done with the header, now add a footer")
    rt.kick.assert_called()  # fell through to the replan path


# ---- request_plan: replan path (FINISHED + real change intent) ---------------


async def test_real_change_intent_post_finish_still_replans() -> None:
    """'add a dark mode toggle' on a FINISHED conversation must still trigger
    the replan path — enter_planning + kick both called."""
    store = SqliteEventStore(":memory:")
    await _finished_conversation(store)
    rt = _make_rt(store)
    ops = ControlOps(rt)

    await ops.request_plan(CID, "add a dark mode toggle")

    rt._loop_for.assert_called_once_with(CID)
    rt._loop_for.return_value.enter_planning.assert_awaited_once_with("add a dark mode toggle")
    rt.kick.assert_called_once_with(CID, claimed_user_seq=1)


async def test_publish_to_netlify_post_finish_still_replans() -> None:
    """'publish to Netlify' is an ambiguous real-deploy intent — must replan."""
    store = SqliteEventStore(":memory:")
    await _finished_conversation(store)
    rt = _make_rt(store)
    ops = ControlOps(rt)

    await ops.request_plan(CID, "publish to Netlify")

    rt.kick.assert_called_once_with(CID, claimed_user_seq=1)
    rt._loop_for.return_value.enter_planning.assert_awaited_once()


async def test_bare_publish_post_finish_still_replans() -> None:
    """Bare 'publish it' is ambiguous — must replan, not be treated as stop."""
    store = SqliteEventStore(":memory:")
    await _finished_conversation(store)
    rt = _make_rt(store)
    ops = ControlOps(rt)

    await ops.request_plan(CID, "publish it")

    rt.kick.assert_called_once_with(CID, claimed_user_seq=1)


# ---- request_plan: guard only applies when FINISHED -------------------------


async def test_ship_it_phrase_mid_run_still_replans() -> None:
    """The ship-it guard is ONLY for FINISHED conversations. The same phrase on
    a non-FINISHED conversation (e.g. IDLE — a fresh conversation) must fall
    through to the existing replan path."""
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    # IDLE by default (no StatusEvent appended) — NOT FINISHED.
    rt = _make_rt(store)
    ops = ControlOps(rt)

    await ops.request_plan(CID, "publish it and be done")

    # Must have entered the replan path.
    rt.kick.assert_called_once_with(CID)
    rt._loop_for.return_value.enter_planning.assert_awaited_once()


async def test_ship_it_phrase_awaiting_approval_still_replans() -> None:
    """AWAITING_PLAN_APPROVAL is not FINISHED — the stop-phrase check must not
    fire; the conversation continues through the normal replan path."""
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    await store.append(
        CID,
        StatusEvent(status=ConversationStatus.AWAITING_PLAN_APPROVAL),
    )
    rt = _make_rt(store)
    ops = ControlOps(rt)

    await ops.request_plan(CID, "we're done")

    rt.kick.assert_called_once_with(CID)
