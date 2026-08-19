"""F-3 follow-up: the EXPLICIT `accept_finished` frame is authoritative stop-intent.

The free-text ship-it heuristic (test_control_ops_ship_it.py) guesses stop-intent
from prose. This frame removes the guessing for the UI's Mark-done control: the
frontend STATES "accept the finished build as-is", and the server must

  * leave a FINISHED conversation FINISHED — no planning ingress, no run-intent
    marker, no PlanEvent, no kick;
  * treat the frame as AUTHORITATIVE: any prose riding it (even a clear change
    request like "add a dark mode toggle", which through `request_plan` MUST
    replan) is echoed as a user note and never intent-matched;
  * be a no-op on a non-FINISHED conversation (nothing to accept; pause/cancel
    own live runs);
  * be reachable from the WS seam (`accept_finished` frames dispatch to
    `conversation_control.accept_finished`, and imported read-only conversations
    refuse the frame — it can write the acknowledgment echo).

Exercised against the real ControlOps + WorkspaceCoordinator + event store (the
``test_control_ops_ship_it.py`` faithful-double pattern), asserting the
observable event log rather than mock bookkeeping.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from disco.agent_server.control_ops import ControlOps
from disco.agent_server.lifecycle_command_service import LifecycleCommandService
from disco.agent_server.routes.ws import _handle_frame
from disco.agent_server.run_registry import CancellationRegistry, LoopRegistry
from disco.agent_server.workspace_fence import WorkspaceFenceService
from disco.agent_server.workspace_service import WorkspaceCoordinator
from disco.core import (
    ConversationStatus,
    EventSource,
    LLMMessage,
    MessageEvent,
    SqliteEventStore,
    StatusEvent,
    WorkspaceMutationEvent,
    WSClientFrame,
)
from disco.core.events import PlanEvent
from disco.tools.projects import ProjectStore

CID = "conv-accept-finished-test"


# ---- faithful runtime double (mirrors test_control_ops_ship_it.py) -----------


@pytest.fixture(autouse=True)
def _deploy_lock_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The real WorkspaceCoordinator ingress rides a cross-process fence; give it
    a per-test lock directory (mirrors test_control_ops_ship_it.py)."""
    monkeypatch.setenv("DISCO_DEPLOY_LOCK_DIR", str(tmp_path / "locks"))


class _Runtime:
    """Faithful runtime double: a REAL WorkspaceCoordinator over a real event
    store, plus trackable kick/_loop_for spies — identical in shape to
    test_control_ops_ship_it.py::_Runtime so assertions read the real event
    log, not mock bookkeeping."""

    _BUILD_LIKE_SURFACES = frozenset({"build", "agent"})

    def __init__(self, store: SqliteEventStore, project_store: ProjectStore) -> None:
        self._store = store
        self._project_store = project_store
        self.settings = MagicMock()
        self.settings._surface_of.return_value = "build"
        projects = MagicMock()
        projects.current_project_store.return_value = project_store
        fence = WorkspaceFenceService(store, projects, self.settings)
        self.workspace = WorkspaceCoordinator(
            store,
            self.settings,
            projects,
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            fence,
        )
        self._lifecycle_commands = LifecycleCommandService(
            store=store,
            fence=self.workspace,
            terminal_effects=MagicMock(),
        )
        self.workspace._lifecycle_commands = self._lifecycle_commands
        self.run_controller = MagicMock()
        self._loop_for = MagicMock()
        self._loop_for.return_value.enter_planning = AsyncMock()

    def _surface_of(self, conversation_id: str) -> str:
        return "build"

    def _project_store_now(self) -> ProjectStore:
        return self._project_store


# ---- helpers -----------------------------------------------------------------


def _make_rt(store: SqliteEventStore, tmp_path: Path) -> _Runtime:
    return _Runtime(store, ProjectStore(str(tmp_path / "projects")))


def _make_ops(runtime: _Runtime) -> ControlOps:
    controller = MagicMock()
    controller.kick = runtime.run_controller.kick
    controller.loop_for = runtime._loop_for
    return ControlOps(
        runtime._store,
        runtime.workspace,
        LoopRegistry(),
        CancellationRegistry(),
        controller,
        MagicMock(),
    )


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


def _run_intents(events: list[Any]) -> list[WorkspaceMutationEvent]:
    return [
        e
        for e in events
        if isinstance(e, WorkspaceMutationEvent) and e.operation == "agent.run-intent.request-plan"
    ]


def _planning_statuses(events: list[Any]) -> list[StatusEvent]:
    return [
        e
        for e in events
        if isinstance(e, StatusEvent)
        and e.status == ConversationStatus.RUNNING
        and e.detail == "planning"
    ]


async def _assert_stayed_finished_no_replan(
    rt: _Runtime, store: SqliteEventStore
) -> None:
    """The authoritative-accept postcondition: FINISHED state, no planning
    ingress, no run-intent, no PlanEvent, no kick, no loop engagement."""
    events = await store.get_events(CID)
    state = await store.get_state(CID)
    assert state.execution_status == ConversationStatus.FINISHED
    assert not _planning_statuses(events), "no RUNNING/planning status may land"
    assert not _run_intents(events), "no run-intent marker may land"
    assert not [e for e in events if isinstance(e, PlanEvent)], "no PlanEvent may land"
    rt.run_controller.kick.assert_not_called()
    rt._loop_for.assert_not_called()
    rt._loop_for.return_value.enter_planning.assert_not_awaited()


# ---- accept_finished: FINISHED conversation ----------------------------------


async def test_accept_finished_stays_finished_no_events_without_text(tmp_path: Path) -> None:
    """The bare frame (the Mark-done click sends no text): the conversation stays
    FINISHED and the event log is untouched — the frame is the whole signal."""
    store = SqliteEventStore(":memory:")
    await _finished_conversation(store)
    rt = _make_rt(store, tmp_path)
    ops = _make_ops(rt)
    before = await store.get_events(CID)

    await ops.accept_finished(CID)

    after = await store.get_events(CID)
    assert [e.id for e in after] == [e.id for e in before], "no events may land"
    await _assert_stayed_finished_no_replan(rt, store)


async def test_accept_finished_wins_over_conflicting_prose(tmp_path: Path) -> None:
    """AUTHORITATIVE: prose riding the frame is NEVER intent-matched. 'add a dark
    mode toggle' — a clear change request that MUST replan through request_plan —
    is echoed as a user note only; the conversation stays FINISHED."""
    store = SqliteEventStore(":memory:")
    await _finished_conversation(store)
    rt = _make_rt(store, tmp_path)
    ops = _make_ops(rt)

    text = "add a dark mode toggle"
    await ops.accept_finished(CID, text)

    events = await store.get_events(CID)
    echoes = [
        e
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.USER
        and e.message.content == text
    ]
    assert len(echoes) == 1, "the note must be appended exactly once as a user message"
    await _assert_stayed_finished_no_replan(rt, store)


async def test_same_prose_via_request_plan_still_replans(tmp_path: Path) -> None:
    """The contrast that makes the previous test meaningful: the IDENTICAL prose
    through the free-text path (request_plan) engages the replan ingress + kick.
    Intent is decided by the FRAME, not the words."""
    store = SqliteEventStore(":memory:")
    await _finished_conversation(store)
    rt = _make_rt(store, tmp_path)
    ops = _make_ops(rt)

    await ops.request_plan(CID, "add a dark mode toggle")

    events = await store.get_events(CID)
    state = await store.get_state(CID)
    assert state.execution_status == ConversationStatus.RUNNING
    assert len(_planning_statuses(events)) == 1
    assert len(_run_intents(events)) == 1
    rt.run_controller.kick.assert_called_once()


async def test_accept_finished_wins_over_ship_it_prose_too(tmp_path: Path) -> None:
    """A stop phrase riding the frame changes nothing either — same authoritative
    accept, with the note echoed. (Belt-and-suspenders: no text ever routes this
    frame anywhere else.)"""
    store = SqliteEventStore(":memory:")
    await _finished_conversation(store)
    rt = _make_rt(store, tmp_path)
    ops = _make_ops(rt)

    await ops.accept_finished(CID, "ship it")

    events = await store.get_events(CID)
    echoes = [
        e
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.USER
        and e.message.content == "ship it"
    ]
    assert len(echoes) == 1
    await _assert_stayed_finished_no_replan(rt, store)


# ---- accept_finished: non-FINISHED conversations are a no-op -----------------


@pytest.mark.parametrize(
    "seed_status",
    [None, ConversationStatus.AWAITING_PLAN_APPROVAL, ConversationStatus.RUNNING],
)
async def test_accept_finished_noop_when_not_finished(
    tmp_path: Path, seed_status: ConversationStatus | None
) -> None:
    """There is nothing to accept on IDLE (no status event), AWAITING_PLAN_APPROVAL,
    or RUNNING: no events land (not even the note), no kick, state unchanged —
    this control must never wind down or redirect a live run."""
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    if seed_status is not None:
        await store.append(CID, StatusEvent(status=seed_status))
    rt = _make_rt(store, tmp_path)
    ops = _make_ops(rt)
    before = await store.get_events(CID)
    state_before = (await store.get_state(CID)).execution_status

    await ops.accept_finished(CID, "we're done")

    after = await store.get_events(CID)
    assert [e.id for e in after] == [e.id for e in before], "no events may land"
    assert (await store.get_state(CID)).execution_status == state_before
    rt.run_controller.kick.assert_not_called()


# ---- WS seam: dispatch + imported read-only refusal --------------------------


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, data: dict[str, Any]) -> None:
        self.sent.append(data)


async def test_ws_accept_finished_dispatches_to_conversation_control() -> None:
    """The WS control seam routes the frame (with its optional note) to
    conversation_control.accept_finished — the wire path the UI control uses."""
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    runtime = MagicMock()
    runtime.conversation_control.accept_finished = AsyncMock()

    frame = WSClientFrame(type="accept_finished", content="looks great")
    await _handle_frame(store, _FakeWS(), CID, frame, runtime)

    runtime.conversation_control.accept_finished.assert_awaited_once_with(CID, "looks great")


async def test_ws_accept_finished_refused_for_imported_conversation() -> None:
    """Imported conversations are read-only; the frame can append the user note,
    so the WS seam must refuse it like every other write frame."""
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local", origin="imported")
    runtime = MagicMock()
    runtime.conversation_control.accept_finished = AsyncMock()
    ws = _FakeWS()

    frame = WSClientFrame(type="accept_finished")
    await _handle_frame(store, ws, CID, frame, runtime)

    runtime.conversation_control.accept_finished.assert_not_awaited()
    assert any(
        f.get("type") == "error" and f.get("error", {}).get("detail") == "imported_read_only"
        for f in ws.sent
    ), "the imported_read_only refusal must be sent back on the socket"
