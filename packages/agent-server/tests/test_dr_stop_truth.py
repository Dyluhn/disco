"""Stop and Kill must be true on a LIVE deep-research run (L29).

Measured 2026-09-02: a `standard_deep` run was in its writer phase, the user
pressed Stop, and 29 ms later the log carried `status IDLE detail="cancelled"`.
No checkpoint, no PAUSED, no Resume — while the engine went on emitting
`model_activity` / `review` / `rework` for another eight minutes. The status was
a statement about nothing: for this surface the ENGINE owns the run, and the
`AgentLoop` the terminal came from had never started it.

These tests pin the four facts that fixes it:

  1. Stop on a live deep-research run sets the flag and appends ONE
     non-terminal `stop_requested` marker — and NO status. The run writes its
     own ending.
  2. Build/agent conversations keep the loop branch exactly as it was.
  3. A deep-research conversation with no run in flight also keeps it: the
     branch is chosen by the live-run registry, never by the surface name.
  4. `run_execute`'s terminal branch turns the engine's Stop checkpoint into
     `ResearchCheckpointEvent` + PAUSED/"stopped", with the run's selections
     kept so Resume runs at the same tier.

  5. Resume from that checkpoint re-enters the run with its pool. This was
     broken and invisible: `resume_conversation` writes no status for this
     surface (the engine owns it) but still sent that statusless batch to the
     lifecycle TRANSITION path, which rejects it — so Resume did nothing, and
     nobody could see it while Stop never produced a PAUSED to resume from.

…and one Kill fact: a killed research run's terminal says a person ended it,
and says nothing else. Kill used to append one "Kill interrupted this admitted
action before its result was recorded. Its outcome is UNKNOWN; re-verify the
workspace or external system before retrying." per unpaired ActionEvent — i.e.
per turn counter, per phase marker, per heartbeat. Resume's reconstruction had
the same bug with its own sentence ("interrupted by a server restart").
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest
from disco.agent_server import ConversationRuntime
from disco.agent_server._deep_research_service_parts import execute as execute_parts
from disco.core import (
    ActionEvent,
    AgentErrorEvent,
    ConversationStatus,
    ErrorEvent,
    EventSource,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    ReportEvent,
    ResearchCheckpointEvent,
    SqliteEventStore,
    StatusEvent,
    ToolCall,
)
from disco.retrieval.deep_research import DepthTier, ReportFromRun

CID = "c1"


def _user(text: str) -> MessageEvent:
    return MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content=text))


def _progress(name: str, **args: Any) -> ActionEvent:
    """One of the run's own progress markers, in the shape the emit callback
    appends them (`_deep_research_service_parts.execute.build_emit_callback`)."""
    return ActionEvent(
        thought=f"Deep Research: {name}",
        tool_call=ToolCall(tool_name=name, arguments=dict(args)),
    )


def _rt(store: SqliteEventStore, *, surface: str = "deep_research") -> ConversationRuntime:
    rt = ConversationRuntime(store)
    rt.settings._set_surface(CID, surface)
    store.create_conversation(CID, owner_id="local")
    return rt


async def _live_run(rt: ConversationRuntime, store: SqliteEventStore) -> asyncio.Event:
    """Put a fake engine run in flight: the live-run queues are installed and a
    couple of progress markers are on the log, exactly as a real run leaves
    them. Returns an event set once the run is established."""
    started = asyncio.Event()

    async def fake_execute(conversation_id: str, *, resume_from: Any = None) -> None:
        del resume_from
        rt.deep_research._live_state.begin(conversation_id)
        try:
            await store.append(conversation_id, _progress("turn", n=3, of=16, phase="thinking"))
            await store.append(conversation_id, _progress("phase", phase="writing"))
            started.set()
            await asyncio.sleep(600)
        finally:
            rt.deep_research._live_state.forget(conversation_id)

    rt.deep_research._execute_deep_research = fake_execute  # type: ignore[method-assign]
    await store.append(CID, _user("state of X"))
    rt.start(CID)
    await asyncio.wait_for(started.wait(), timeout=10)
    return started


def _stop_markers(events: list[Any]) -> list[ActionEvent]:
    return [
        e
        for e in events
        if isinstance(e, ActionEvent)
        and e.tool_call is not None
        and e.tool_call.tool_name == "stop_requested"
    ]


async def test_stop_on_a_live_run_marks_and_never_terminates() -> None:
    """The whole defect, in one assertion set: the flag is set, ONE
    `stop_requested` marker lands, and no status is written at all."""
    store = SqliteEventStore(":memory:")
    rt = _rt(store)
    await _live_run(rt, store)
    before = len([e for e in await store.get_events(CID) if isinstance(e, StatusEvent)])

    await rt.cancel(CID)

    events = await store.get_events(CID)
    markers = _stop_markers(events)
    assert len(markers) == 1
    assert markers[0].tool_call is not None
    # The payload carries exactly what the server knows: that Stop happened,
    # and when. Everything else the wall says comes from the run's own events.
    assert set(markers[0].tool_call.arguments) == {"requested_at"}
    statuses = [e for e in events if isinstance(e, StatusEvent)]
    assert len(statuses) == before, "Stop must not write a terminal status"
    assert not any(
        s.status == ConversationStatus.IDLE and s.detail == "cancelled" for s in statuses
    )
    # And the flag the engine polls really is set — the marker is a record of
    # the request, not a substitute for it.
    assert rt._cancellations._flags[CID].is_set()

    await rt.kill(CID)
    await rt.aclose()


async def test_stop_on_a_build_conversation_is_unchanged() -> None:
    """The loop branch is untouched: a build run still gets its terminal
    IDLE/"cancelled" from `AgentLoop.cancel`, and no research marker appears."""
    store = SqliteEventStore(":memory:")
    rt = _rt(store, surface="build")
    await store.append(CID, _user("build a site"))
    rt._loop_registry.bind(CID, rt.run_controller.loop_for(CID))

    await rt.cancel(CID)

    events = await store.get_events(CID)
    assert _stop_markers(events) == []
    assert any(
        isinstance(e, StatusEvent)
        and e.status == ConversationStatus.IDLE
        and e.detail == "cancelled"
        for e in events
    )
    await rt.aclose()


async def test_stop_on_a_deep_research_conversation_with_no_live_run() -> None:
    """The branch is chosen by the live-run registry, not by the surface: a
    deep-research conversation whose engine is NOT running keeps the loop
    behaviour, because there is no engine to write an ending."""
    store = SqliteEventStore(":memory:")
    rt = _rt(store)
    await store.append(CID, _user("state of X"))
    assert not rt.deep_research.has_live_run(CID)
    rt._loop_registry.bind(CID, rt.run_controller.loop_for(CID))

    await rt.cancel(CID)

    events = await store.get_events(CID)
    assert _stop_markers(events) == []
    assert any(isinstance(e, StatusEvent) and e.detail == "cancelled" for e in events)
    await rt.aclose()


async def test_kill_on_a_live_run_says_a_person_ended_it() -> None:
    """A killed research run's terminal: IDLE/"killed", the trace intact, and
    no error of any kind — neither a run failure nor a per-marker "re-verify
    the workspace" warning about a turn counter."""
    store = SqliteEventStore(":memory:")
    rt = _rt(store)
    await _live_run(rt, store)

    await rt.kill(CID)
    await asyncio.sleep(0.2)

    events = await store.get_events(CID)
    assert [e for e in events if isinstance(e, AgentErrorEvent)] == []
    assert [e for e in events if isinstance(e, ErrorEvent)] == []
    assert not any(isinstance(e, ResearchCheckpointEvent) for e in events)
    # The trace the run produced stays on the conversation.
    assert [
        e.tool_call.tool_name
        for e in events
        if isinstance(e, ActionEvent) and e.tool_call is not None
    ] == ["turn", "phase"]
    terminal = [e for e in events if isinstance(e, StatusEvent)][-1]
    assert terminal.status == ConversationStatus.IDLE
    assert terminal.detail == "killed"
    await rt.aclose()


async def test_resume_from_a_stop_checkpoint_re_enters_the_run() -> None:
    """The other half of the promise the stopping wall makes.

    `resume_conversation` deliberately writes NO status for this surface — the
    engine owns it — but it still sent that statusless batch to the lifecycle
    TRANSITION path, which rejects a batch with no status. The WS control frame
    swallowed the error, so Resume on a stopped run did nothing and the
    conversation sat PAUSED behind a Resume button that could never work. It was
    invisible until Stop started producing PAUSED at all.
    """
    store = SqliteEventStore(":memory:")
    rt = _rt(store)
    await store.append(CID, _user("state of X"))
    checkpoint = ResearchCheckpointEvent(
        query="state of X",
        passages=[],
        all_hits=[],
        trail=[{"kind": "search", "query": "what is X"}],
        completed_queries=["what is X"],
        depth_tier=DepthTier.QUICK.value,
    )
    await store.append(CID, checkpoint)
    await store.append(CID, StatusEvent(status=ConversationStatus.PAUSED, detail="stopped"))
    resumed = AsyncMock()
    rt.deep_research._execute_deep_research = resumed  # type: ignore[method-assign]

    await store.append(CID, _progress("turn", n=8, of=8, phase="thinking"))
    await store.append(CID, _progress("model_activity", stage="draft", seconds=12.0))

    result = await rt._resume.resume_conversation(CID)
    await asyncio.sleep(0.3)

    assert result["ok"] is True
    # …and the run's own progress markers are not "dangling tool calls": pairing
    # them wrote one "interrupted by a server restart — its outcome is UNKNOWN"
    # observation per turn counter and per token heartbeat.
    assert [e for e in await store.get_events(CID) if isinstance(e, ObservationEvent)] == []
    # The dispatcher re-entered the stopped run WITH its checkpoint, so the
    # gathered pool seeds the pool instead of being researched again.
    resumed.assert_awaited_once()
    assert resumed.await_args is not None
    assert resumed.await_args.kwargs["resume_from"].id == checkpoint.id
    await rt.aclose()


@pytest.fixture
def _stopped_engine(monkeypatch: pytest.MonkeyPatch) -> ReportFromRun:
    """Stand the run driver up around a single fact: the engine returned a Stop
    checkpoint. Everything between the question and that return — providers,
    preflight, the router — is the run's other concerns and is stubbed out."""
    checkpoint = ReportFromRun(
        query="state of X",
        summary="",
        sections=[],
        cited_passages=[],
        reviewed_passages=[],
        all_hits=[],
        unsupported_count=0,
        bounded_by="stopped",
        depth_tier=DepthTier.EXHAUSTIVE.value,
        completed_probes=["what is X"],
        research_trail=[{"kind": "search", "query": "what is X"}],
    )
    monkeypatch.setattr(
        execute_parts, "build_retrieval_deps", AsyncMock(return_value=({}, frozenset(), ()))
    )
    monkeypatch.setattr(execute_parts, "run_preflight", AsyncMock(return_value=None))
    monkeypatch.setattr(
        execute_parts, "build_router_and_engine", lambda *a, **k: (object(), object())
    )
    monkeypatch.setattr(execute_parts, "build_run", lambda *a, **k: object())
    monkeypatch.setattr(execute_parts, "run_engine", AsyncMock(return_value=checkpoint))
    return checkpoint


async def test_stop_checkpoint_becomes_paused_and_keeps_the_selections(
    _stopped_engine: ReportFromRun,
) -> None:
    """The run's own ending: a checkpoint event plus PAUSED/"stopped", with the
    depth selection kept so Resume runs at the tier the user chose."""
    store = SqliteEventStore(":memory:")
    rt = _rt(store)
    rt.deep_research.set_depth(CID, DepthTier.EXHAUSTIVE.value)
    await store.append(CID, _user("state of X"))

    await execute_parts.run_execute(rt.deep_research, CID)

    events = await store.get_events(CID)
    checkpoints = [e for e in events if isinstance(e, ResearchCheckpointEvent)]
    assert len(checkpoints) == 1
    assert checkpoints[0].completed_queries == ["what is X"]
    assert checkpoints[0].depth_tier == DepthTier.EXHAUSTIVE.value
    terminal = [e for e in events if isinstance(e, StatusEvent)][-1]
    assert terminal.status == ConversationStatus.PAUSED
    assert terminal.detail == "stopped"
    # A stopped run keeps its selections: resume must not silently drop to the
    # default tier.
    assert rt.deep_research._depth_for(CID) is DepthTier.EXHAUSTIVE
    await rt.aclose()


@pytest.mark.parametrize("during_cancel", [False, True])
async def test_kill_cannot_overwrite_a_report_that_committed_first(during_cancel):
    store = SqliteEventStore(":memory:")
    rt = _rt(store)
    await store.append(CID, _user("research the evidence"))
    await store.append(CID, StatusEvent(status=ConversationStatus.RUNNING))

    async def finish():
        await store.append_many(
            CID,
            [
                ReportEvent(
                    query="research the evidence",
                    summary="A completed report.",
                    sections=[{"id": "s0", "title": "Finding", "markdown": "A completed report."}],
                ),
                StatusEvent(status=ConversationStatus.FINISHED),
            ],
        )

    if during_cancel:
        started = asyncio.Event()

        async def commit_while_cancelling():
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await finish()
                raise
            return await store.get_state(CID)

        task = asyncio.create_task(commit_while_cancelling())
        rt.run_registry.register_task(CID, task)
        await started.wait()
    else:
        await finish()
    await rt.kill(CID)
    events = await store.get_events(CID)
    assert len([event for event in events if isinstance(event, ReportEvent)]) == 1
    assert [event for event in events if isinstance(event, StatusEvent)][
        -1
    ].status is ConversationStatus.FINISHED
    assert not any(isinstance(event, StatusEvent) and event.detail == "killed" for event in events)
    assert rt.run_registry.active_task(CID) is None
    await rt.aclose()


async def test_a_new_user_run_after_a_report_is_still_killable_before_preflight():
    store = SqliteEventStore(":memory:")
    rt = _rt(store)
    await store.append_many(
        CID,
        [
            _user("first research"),
            ReportEvent(
                query="first research",
                summary="A completed report.",
                sections=[{"id": "s0", "title": "Finding", "markdown": "A completed report."}],
            ),
            StatusEvent(status=ConversationStatus.FINISHED),
            _user("new research"),
        ],
    )
    await rt.kill(CID)
    events = await store.get_events(CID)
    status = [event for event in events if isinstance(event, StatusEvent)][-1]
    assert status.status is ConversationStatus.IDLE and status.detail == "killed"
    assert len([event for event in events if isinstance(event, ReportEvent)]) == 1
    await rt.aclose()
