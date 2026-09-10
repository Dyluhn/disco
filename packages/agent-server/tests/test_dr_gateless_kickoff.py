"""Deep Research v2 gateless kickoff — the dispatcher phase table (PKG-35).

Decision #7: there is NO plan gate. The first user message launches execution
immediately — no PlanEvent proposal, no AWAITING_PLAN_APPROVAL, no approval
step (autonomous or human). These tests pin the new dispatch table:

  kickoff   — first user message → execute, exactly once, no plan events;
  in-flight — RUNNING with no report → no-op (mid-run input steers via WS);
  error     — ERROR retries fresh ONLY on new user input (no re-kick loop);
  resume    — PAUSED → execute with the ResearchCheckpointEvent carried;
  query     — pre-run user additions fold into the question.

Hermetic — the engine itself is faked via ``_execute_deep_research``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from disco.agent_server import ConversationRuntime
from disco.core import (
    ConversationStatus,
    EventSource,
    LLMMessage,
    MessageEvent,
    PlanEvent,
    ReportEvent,
    ReportSection,
    ResearchCheckpointEvent,
    SqliteEventStore,
    StatusEvent,
)


def _user(text: str) -> MessageEvent:
    return MessageEvent(
        source=EventSource.USER,
        message=LLMMessage(role="user", content=text),
    )


def _rt(store: SqliteEventStore) -> ConversationRuntime:
    rt = ConversationRuntime(store)
    rt.settings._set_surface("c1", "deep_research")
    return rt


async def test_first_user_message_launches_execution(monkeypatch):
    """The kickoff: one user message → the engine runs. No PlanEvent, no
    AWAITING_PLAN_APPROVAL, no approval step of any kind."""
    store = SqliteEventStore(":memory:")
    rt = _rt(store)
    await store.append("c1", _user("state of local LLMs in 2026"))
    exec_mock = AsyncMock()
    monkeypatch.setattr(rt.deep_research, "_execute_deep_research", exec_mock)

    await rt.deep_research._maybe_run_deep_research("c1")

    exec_mock.assert_awaited_once_with("c1")
    events = await store.get_events("c1")
    assert not any(isinstance(e, PlanEvent) for e in events), "gateless: no plan is proposed"
    assert not any(
        isinstance(e, StatusEvent) and e.status == ConversationStatus.AWAITING_PLAN_APPROVAL
        for e in events
    )


async def test_kickoff_ignores_autonomous_setting(monkeypatch):
    """Gateless means gateless: an interactive (non-autonomous) conversation
    kicks off exactly like an autonomous one — there is no approval to skip."""
    store = SqliteEventStore(":memory:")
    rt = _rt(store)  # autonomous NOT set
    await store.append("c1", _user("What is X?"))
    exec_mock = AsyncMock()
    monkeypatch.setattr(rt.deep_research, "_execute_deep_research", exec_mock)

    await rt.deep_research._maybe_run_deep_research("c1")

    assert exec_mock.await_count == 1


async def test_no_user_message_is_noop(monkeypatch):
    """Nothing to research yet → the dispatcher emits nothing and runs nothing."""
    store = SqliteEventStore(":memory:")
    rt = _rt(store)
    exec_mock = AsyncMock()
    monkeypatch.setattr(rt.deep_research, "_execute_deep_research", exec_mock)

    await rt.deep_research._maybe_run_deep_research("c1")

    assert exec_mock.await_count == 0
    assert await store.get_events("c1") == []


async def test_running_without_report_is_noop(monkeypatch):
    """A RUNNING status with no report means the engine is executing — a
    re-kick (e.g. a plain send appended mid-run) must NOT start a second run.
    Mid-run guidance reaches the engine via the WS steer queue instead."""
    store = SqliteEventStore(":memory:")
    rt = _rt(store)
    await store.append("c1", _user("state of local LLMs in 2026"))
    await store.append("c1", StatusEvent(status=ConversationStatus.RUNNING, detail="research"))
    await store.append("c1", _user("also cover MoE models"))
    exec_mock = AsyncMock()
    monkeypatch.setattr(rt.deep_research, "_execute_deep_research", exec_mock)

    await rt.deep_research._maybe_run_deep_research("c1")

    assert exec_mock.await_count == 0


async def test_error_retries_only_on_fresh_user_message(monkeypatch):
    """An errored run must not restart from a bare re-kick (that would loop an
    erroring run) — but a NEW user message retries fresh."""
    store = SqliteEventStore(":memory:")
    rt = _rt(store)
    await store.append("c1", _user("state of local LLMs in 2026"))
    await store.append("c1", StatusEvent(status=ConversationStatus.ERROR, detail="boom"))
    exec_mock = AsyncMock()
    monkeypatch.setattr(rt.deep_research, "_execute_deep_research", exec_mock)

    await rt.deep_research._maybe_run_deep_research("c1")
    assert exec_mock.await_count == 0, "bare re-kick after ERROR must not restart"

    await store.append("c1", _user("try again, focus on inference engines"))
    await rt.deep_research._maybe_run_deep_research("c1")
    assert exec_mock.await_count == 1


async def test_paused_resumes_with_checkpoint(monkeypatch):
    """PAUSED → resume carries the full checkpoint trail and evidence."""
    store = SqliteEventStore(":memory:")
    rt = _rt(store)
    await store.append("c1", _user("state of local LLMs in 2026"))
    checkpoint = ResearchCheckpointEvent(
        query="state of local LLMs in 2026",
        passages=[
            {
                "id": "p0",
                "source_url": "https://example.test/local-llm",
                "source_title": "Local LLM report",
                "text": "A source passage retained by the checkpoint.",
            }
        ],
        all_hits=[
            {
                "url": "https://example.test/local-llm",
                "title": "Local LLM report",
                "snippet": "A retained discovery hit.",
                "source_engine": "test",
                "rank": 1,
            }
        ],
        trail=[{"kind": "search", "query": "local llm inference 2026"}],
        completed_queries=["local llm inference 2026"],
        depth_tier="standard_deep",
        recency_window="month",
    )
    await store.append("c1", checkpoint)
    await store.append("c1", StatusEvent(status=ConversationStatus.PAUSED, detail="stopped"))
    exec_mock = AsyncMock()
    monkeypatch.setattr(rt.deep_research, "_execute_deep_research", exec_mock)

    await rt.deep_research._maybe_run_deep_research("c1")

    exec_mock.assert_awaited_once()
    resume_from = exec_mock.await_args.kwargs["resume_from"]
    assert resume_from is not None
    assert resume_from.completed_queries == ["local llm inference 2026"]
    assert resume_from.trail == [{"kind": "search", "query": "local llm inference 2026"}]
    assert resume_from.passages[0]["id"] == "p0"


def test_checkpoint_resume_rebuilds_typed_evidence_and_trail():
    """The execution seam preserves typed passages, hits, queries, and trail."""
    from disco.agent_server._deep_research_service_parts.execute import rebuild_resume_state

    checkpoint = ResearchCheckpointEvent(
        query="state of local LLMs in 2026",
        passages=[
            {
                "id": "p0",
                "source_url": "https://example.test/local-llm",
                "source_title": "Local LLM report",
                "text": "A source passage retained by the checkpoint.",
            }
        ],
        all_hits=[
            {
                "url": "https://example.test/local-llm",
                "title": "Local LLM report",
                "snippet": "A retained discovery hit.",
                "source_engine": "test",
                "rank": 1,
            }
        ],
        trail=[{"kind": "search", "query": "local llm inference 2026"}],
        completed_queries=["local llm inference 2026"],
    )

    sections, passages, hits, queries, pending, trail = rebuild_resume_state(checkpoint)

    assert sections is None
    assert passages is not None and passages[0].id == "p0"
    assert hits is not None and hits[0].url == "https://example.test/local-llm"
    assert queries == ["local llm inference 2026"]
    assert pending is None
    assert trail == [{"kind": "search", "query": "local llm inference 2026"}]


async def test_finished_report_without_new_message_is_noop(monkeypatch):
    """A finished report with no fresh user message is a finished
    conversation — no new run, no follow-up."""
    store = SqliteEventStore(":memory:")
    rt = _rt(store)
    await store.append("c1", _user("state of local LLMs in 2026"))
    await store.append(
        "c1",
        ReportEvent(
            query="state of local LLMs in 2026",
            summary="Done.",
            sections=[ReportSection(id="s0", title="T", markdown="Body.")],
            passages=[],
            all_hits=[],
        ),
    )
    await store.append("c1", StatusEvent(status=ConversationStatus.FINISHED))
    exec_mock = AsyncMock()
    follow_mock = AsyncMock()
    monkeypatch.setattr(rt.deep_research, "_execute_deep_research", exec_mock)
    monkeypatch.setattr(rt.deep_research, "_follow_up_deep_research", follow_mock)

    await rt.deep_research._maybe_run_deep_research("c1")

    assert exec_mock.await_count == 0
    assert follow_mock.await_count == 0


async def test_finished_report_plus_fresh_message_routes_to_followup(monkeypatch):
    """After a report, a new user message is a follow-up — never a new run."""
    store = SqliteEventStore(":memory:")
    rt = _rt(store)
    await store.append("c1", _user("state of local LLMs in 2026"))
    report = ReportEvent(
        query="state of local LLMs in 2026",
        summary="Done.",
        sections=[ReportSection(id="s0", title="T", markdown="Body.")],
        passages=[],
        all_hits=[],
    )
    await store.append("c1", report)
    await store.append("c1", StatusEvent(status=ConversationStatus.FINISHED))
    await store.append("c1", _user("what about quantization?"))
    exec_mock = AsyncMock()
    follow_mock = AsyncMock()
    monkeypatch.setattr(rt.deep_research, "_execute_deep_research", exec_mock)
    monkeypatch.setattr(rt.deep_research, "_follow_up_deep_research", follow_mock)

    await rt.deep_research._maybe_run_deep_research("c1")

    assert exec_mock.await_count == 0
    follow_mock.assert_awaited_once()


def test_query_folds_pre_run_additions():
    """The run's query is the FIRST user message with later pre-run additions
    folded in as explicit instructions — never the addition text alone."""
    from disco.agent_server._deep_research_service_parts.execute import (
        build_query_with_constraints,
    )

    events = [
        _user("Latest open-source model releases"),
        _user("Only include the past seven days"),
    ]
    query = build_query_with_constraints(events)
    assert query is not None
    assert query.startswith("Latest open-source model releases")
    assert "Only include the past seven days" in query

    assert build_query_with_constraints([]) is None


def test_resume_does_not_duplicate_accepted_steering_but_keeps_new_guidance():
    from disco.agent_server._deep_research_service_parts.execute import build_query_with_constraints

    events = [_user("Compare deployment evidence"), _user("Keep primary source limitations")]
    accepted = frozenset({"Keep primary source limitations"})
    assert (
        build_query_with_constraints(events, accepted_steers=accepted)
        == "Compare deployment evidence"
    )
    events.append(_user("Focus on operating capacity"))
    query = build_query_with_constraints(events, accepted_steers=accepted)
    assert "Focus on operating capacity" in query
    assert "Keep primary source limitations" not in query


async def test_live_websocket_guidance_is_durable_before_enqueue(monkeypatch):
    from disco.agent_server.routes.ws import _handle_steer_frame
    from disco.core import WSClientFrame

    store = SqliteEventStore(":memory:")
    rt = _rt(store)
    rt.deep_research._live_state.begin("c1")
    seen = []

    def enqueue(cid, text):
        rows = store._query(cid, None)
        assert any(isinstance(row, MessageEvent) and row.message.content == text for row in rows)
        seen.append(text)
        return True

    monkeypatch.setattr(rt.deep_research, "enqueue_steer", enqueue)
    await _handle_steer_frame(
        store, "c1", WSClientFrame(type="steer", steer_text="Keep limits"), rt
    )
    assert seen == ["Keep limits"]
    store.close()


async def test_writing_refuses_late_guidance_without_starting_another_run(monkeypatch):
    from disco.agent_server._deep_research_service_parts.execute import build_emit_callback
    from disco.agent_server.routes.ws import _handle_steer_frame
    from disco.core import WSClientFrame

    store = SqliteEventStore(":memory:")
    rt = _rt(store)
    rt.deep_research._live_state.begin("c1")
    assert rt.deep_research.enqueue_steer("c1", "Already accepted")
    emit = build_emit_callback(rt.deep_research, "c1")
    await emit("phase", {"phase": "writing"})
    kick = AsyncMock()
    monkeypatch.setattr(rt.deep_research, "_maybe_run_deep_research", kick)
    accepted = await _handle_steer_frame(
        store, "c1", WSClientFrame(type="steer", steer_text="Too late"), rt
    )
    assert accepted is False
    assert rt.deep_research._live_state.pop_steers("c1") == ["Already accepted"]
    kick.assert_not_awaited()
    assert rt.deep_research.has_live_run("c1")
    store.close()
