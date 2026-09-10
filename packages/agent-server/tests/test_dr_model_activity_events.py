"""`model_activity` reaches the conversation log like every other progress action.

The heartbeat is only useful if it survives the trip from the provider stream to
the event log without a new encoding rule. These tests pin two things: the
action rides the SAME ActionEvent path the loop's other progress events do, and
it never steals the search/observation correlation that pairs a search with its
own results.
"""

from __future__ import annotations

from typing import Any

from disco.agent_server import ConversationRuntime
from disco.agent_server._deep_research_service_parts.execute import build_emit_callback
from disco.core import (
    ActionEvent,
    ObservationEvent,
    SqliteEventStore,
)
from disco.retrieval.deep_research._progress_events import RESEARCH_PROGRESS_ACTIONS


def _rt(store: SqliteEventStore) -> ConversationRuntime:
    rt = ConversationRuntime(store)
    rt.settings._set_surface("c1", "deep_research")
    return rt


def _payload() -> dict[str, Any]:
    return {"stage": "research_turn", "tokens_streamed": 412, "seconds": 12.5, "call_ordinal": 3}


async def test_a_model_activity_lands_as_an_ordinary_action_event() -> None:
    store = SqliteEventStore(":memory:")
    emit = build_emit_callback(_rt(store).deep_research, "c1")

    await emit("model_activity", _payload())

    events = await store.get_events("c1")
    actions = [e for e in events if isinstance(e, ActionEvent)]
    assert len(actions) == 1
    assert actions[0].tool_call is not None
    assert actions[0].tool_call.tool_name == "model_activity"
    assert actions[0].tool_call.arguments == _payload()


async def test_every_declared_progress_action_rides_the_same_path() -> None:
    """No per-action encoding: a new progress action is an ActionEvent, full stop."""
    store = SqliteEventStore(":memory:")
    emit = build_emit_callback(_rt(store).deep_research, "c1")

    for action in RESEARCH_PROGRESS_ACTIONS:
        await emit(action, {"probe": action})

    names = [
        e.tool_call.tool_name
        for e in await store.get_events("c1")
        if isinstance(e, ActionEvent) and e.tool_call is not None
    ]
    assert names == list(RESEARCH_PROGRESS_ACTIONS)


async def test_a_heartbeat_between_a_search_and_its_results_keeps_them_paired() -> None:
    """A heartbeat lands mid-turn, so it must not become the action an
    observation points at — that would attach a search's results to a token
    count and lose the row the user is actually reading."""
    store = SqliteEventStore(":memory:")
    emit = build_emit_callback(_rt(store).deep_research, "c1")

    await emit("search", {"query": "q", "subquestion": "angle-1", "round": 1})
    await emit("model_activity", _payload())
    await emit("observation", {"subquestion": "angle-1", "round": 1, "added": 2})

    events = await store.get_events("c1")
    search = next(
        e
        for e in events
        if isinstance(e, ActionEvent) and e.tool_call is not None
        and e.tool_call.tool_name == "search"
    )
    observation = next(e for e in events if isinstance(e, ObservationEvent))
    assert observation.action_id == search.id
