"""Replay external policy inputs while retaining real research decisions."""

from __future__ import annotations

import asyncio
import datetime as dt
from types import SimpleNamespace

import pytest
from harness.cassette import Cassette, CassetteMiss
from harness.research_environment import ResearchEnvironment, environment_scope


def test_extraction_only_reads_keep_cooldown_feedback_under_changed_live_state(monkeypatch):
    from disco.retrieval.deep_research import _search_outcomes as outcomes
    from disco.retrieval.deep_research import _search_turn, agent

    live = {"exa": 75.0}
    current = [dt.datetime(2026, 9, 7, 8, 20, tzinfo=dt.UTC)]
    monkeypatch.setattr(_search_turn, "engine_cooldown_seconds", lambda: dict(live))
    monkeypatch.setattr(agent, "engine_cooldown_seconds", lambda: dict(live))

    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return current[0].astimezone(tz)

    monkeypatch.setattr(
        outcomes,
        "datetime",
        SimpleNamespace(datetime=Clock, date=dt.date, UTC=dt.UTC, timedelta=dt.timedelta),
    )

    def observe():
        # These policy reads happen even when no search provider is called.
        cooling = _search_turn.engine_cooldown_seconds()
        return cooling, agent.engine_cooldown_seconds(), outcomes.cooldown_until_text(cooling)

    cassette = Cassette()
    with environment_scope(ResearchEnvironment(cassette, recording=True)):
        recorded = observe()
    assert recorded == ({"exa": 75.0}, {"exa": 75.0}, "search engine exa, cooling until 08:21 UTC")
    live.clear()
    live["brave"] = 900.0
    current[0] += dt.timedelta(days=30)
    with environment_scope(ResearchEnvironment(cassette, recording=False)):
        assert observe() == recorded
    assert agent.engine_cooldown_seconds() == {"brave": 900.0}
    assert outcomes.cooldown_until_text(live) != recorded[2]


async def test_hold_expiry_and_visible_events_replay_without_live_state_or_sleep(monkeypatch):
    from disco.retrieval.deep_research import _hold as hold

    clock = [100.0]
    monkeypatch.setattr(
        hold, "engine_cooldown_seconds", lambda: {"exa": 101.0 - clock[0]} if clock[0] < 101 else {}
    )
    monkeypatch.setattr(hold, "search_slot_available", lambda: True)
    monkeypatch.setattr(hold, "_now", lambda: clock[0])

    async def advance(seconds):
        clock[0] += seconds

    monkeypatch.setattr(hold, "_sleep", advance)

    async def run():
        events = []

        async def emit(name, payload):
            events.append((name, payload))

        outcome = await hold.hold_for_dead_pool(
            observed=frozenset({"exa"}),
            sources_retained=3,
            position=(4, 8),
            queued_queries=("unread source",),
            starvation=hold.StarvationClock(),
            emit=emit,
            should_cancel=lambda: False,
        )
        return outcome, events

    cassette = Cassette()
    with environment_scope(ResearchEnvironment(cassette, recording=True)):
        recorded, events = await run()
    assert recorded.held and not recorded.stopped
    assert recorded.waited_s == 1.0 and recorded.engines_live == ("exa",)
    assert [name for name, _ in events] == ["hold", "hold_resumed"]
    assert events[0][1]["sources_retained"] == 3

    def no_live():
        pytest.fail("replay read live policy state")

    async def no_sleep(_seconds):
        pytest.fail("replay waited on live historical duration")

    for name in ("engine_cooldown_seconds", "search_slot_available", "_now"):
        monkeypatch.setattr(hold, name, no_live)
    monkeypatch.setattr(hold, "_sleep", no_sleep)
    with environment_scope(ResearchEnvironment(cassette, recording=False)):
        assert await run() == (recorded, events)


@pytest.mark.parametrize("change", ["reordered", "missing", "extra"])
def test_changed_policy_read_sequence_fails_instead_of_reusing_state(monkeypatch, change):
    from disco.retrieval.deep_research import _search_turn, agent

    monkeypatch.setattr(agent, "engine_cooldown_seconds", lambda: {"exa": 5.0})
    monkeypatch.setattr(_search_turn, "engine_cooldown_seconds", lambda: {"exa": 4.0})
    cassette = Cassette()
    with environment_scope(ResearchEnvironment(cassette, recording=True)):
        agent.engine_cooldown_seconds()
        _search_turn.engine_cooldown_seconds()
    with pytest.raises(CassetteMiss):
        with environment_scope(ResearchEnvironment(cassette, recording=False)):
            if change == "reordered":
                _search_turn.engine_cooldown_seconds()
            else:
                agent.engine_cooldown_seconds()
                if change != "missing":
                    _search_turn.engine_cooldown_seconds()
                    agent.engine_cooldown_seconds()


def test_exception_restores_clock_and_policy_bindings(monkeypatch):
    from disco.retrieval.deep_research import _search_outcomes, agent

    def live():
        return {"exa": 1.0}

    monkeypatch.setattr(agent, "engine_cooldown_seconds", live)
    original_clock = _search_outcomes.datetime
    with pytest.raises(RuntimeError, match="interrupted"):
        with environment_scope(ResearchEnvironment(Cassette(), recording=True)):
            assert agent.engine_cooldown_seconds() == {"exa": 1.0}
            raise RuntimeError("interrupted")
    assert agent.engine_cooldown_seconds is live
    assert _search_outcomes.datetime is original_clock


async def test_unrelated_task_reads_live_state_while_replay_scope_is_active(monkeypatch):
    from disco.retrieval.deep_research import agent

    monkeypatch.setattr(agent, "engine_cooldown_seconds", lambda: {"recorded": 5.0})
    cassette = Cassette()
    with environment_scope(ResearchEnvironment(cassette, recording=True)):
        agent.engine_cooldown_seconds()
    monkeypatch.setattr(agent, "engine_cooldown_seconds", lambda: {"live": 9.0})
    ready = asyncio.Event()

    async def unrelated():
        await ready.wait()
        return agent.engine_cooldown_seconds()

    task = asyncio.create_task(unrelated())  # created outside the replay context
    with environment_scope(ResearchEnvironment(cassette, recording=False)):
        ready.set()
        assert await task == {"live": 9.0}
        assert agent.engine_cooldown_seconds() == {"recorded": 5.0}
    assert agent.engine_cooldown_seconds() == {"live": 9.0}


def test_nested_recordings_keep_their_own_order_and_restore_outer_context(monkeypatch):
    from disco.retrieval.deep_research import agent

    values = iter(({"first": 3}, {"second": 2}, {"first": 1}))
    monkeypatch.setattr(agent, "engine_cooldown_seconds", lambda: next(values))
    first, second = Cassette(), Cassette()
    with environment_scope(ResearchEnvironment(first, recording=True)):
        agent.engine_cooldown_seconds()
        with environment_scope(ResearchEnvironment(second, recording=True)):
            agent.engine_cooldown_seconds()
        agent.engine_cooldown_seconds()
    with environment_scope(ResearchEnvironment(first, recording=False)):
        assert agent.engine_cooldown_seconds() == {"first": 3}
        with environment_scope(ResearchEnvironment(second, recording=False)):
            assert agent.engine_cooldown_seconds() == {"second": 2}
        assert agent.engine_cooldown_seconds() == {"first": 1}


def test_legacy_scope_uses_live_reads_without_contaminating_outer_recording(monkeypatch):
    from disco.retrieval.deep_research import agent

    monkeypatch.setattr(agent, "engine_cooldown_seconds", lambda: {"live": 1})
    cassette = Cassette()
    with environment_scope(ResearchEnvironment(cassette, recording=True)):
        agent.engine_cooldown_seconds()
        with environment_scope(ResearchEnvironment(Cassette(), recording=False)):
            assert agent.engine_cooldown_seconds() == {"live": 1}
        agent.engine_cooldown_seconds()
    assert cassette.seams()["research.environment.read"] == 2


@pytest.mark.parametrize("kind", ["unknown_schema", "missing_schema"])
def test_new_environment_recordings_cannot_silently_fall_back_to_legacy(kind):
    cassette = Cassette()
    if kind == "unknown_schema":
        cassette.record("research.environment.schema", {}, {"version": "unknown"})
    else:
        cassette.record("research.environment.read", {}, None)
    with pytest.raises(ValueError):
        ResearchEnvironment(cassette, recording=False)
