"""F-3: post-FINISHED 'ship-it / accept-as-is' intent must NOT force a re-plan.

After a build FINISHES, a user message that clearly means "stop, we're done"
should echo the user text to the log but leave the conversation FINISHED —
no RUNNING/planning status, no run-intent, no PlanEvent, no task kick.

Real change/deploy intents must still fall through to the replan path, which
publishes the planning ingress — the user echo, a RUNNING/planning StatusEvent,
and the durable ``agent.run-intent.request-plan`` (run_protocol_version=1)
marker — as ONE atomic transaction through the real WorkspaceCoordinator, then
kicks a new turn carrying the new user turn's seq. A peer therefore sees either
the old execution state or the complete planning ingress, never the new
instruction under the old tool scope. The guard is a no-op when the conversation
is NOT FINISHED.

Exercised against the real ControlOps + WorkspaceCoordinator + event store (the
``test_run_status_authority.py::_Runtime`` faithful-double pattern), asserting
the observable event log rather than superseded mock bookkeeping. The old
two-step replan (``_loop_for(cid).enter_planning`` + ``record_run_intent_locked``)
was folded into the atomic ingress and is proven dead here.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from disco.agent_server.control_ops import ControlOps, _is_ship_it_intent
from disco.agent_server.workspace_service import WorkspaceCoordinator
from disco.core import (
    ConversationStatus,
    EventSource,
    LLMMessage,
    MessageEvent,
    SqliteEventStore,
    StatusEvent,
    WorkspaceMutationEvent,
)
from disco.core.events import PlanEvent
from disco.tools.projects import ProjectStore

CID = "conv-f3-test"


# ---- faithful runtime double -------------------------------------------------


@pytest.fixture(autouse=True)
def _deploy_lock_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The real WorkspaceCoordinator ingress rides a cross-process fence; give it
    a per-test lock directory (mirrors test_run_status_authority.py)."""
    monkeypatch.setenv("DISCO_DEPLOY_LOCK_DIR", str(tmp_path / "locks"))


class _Runtime:
    """Faithful runtime double for ControlOps.request_plan: a REAL
    WorkspaceCoordinator over a real event store, plus trackable kick/_loop_for
    spies. Mirrors test_run_status_authority.py::_Runtime and the real
    ConversationRuntime wiring — workspace_lock -> coordinator.lock, and
    _BUILD_LIKE_SURFACES == {"build", "agent"} (runtime.py:1142). Because the
    ingress runs the production coordinator, assertions read the real event log,
    not mock bookkeeping."""

    _BUILD_LIKE_SURFACES = frozenset({"build", "agent"})

    def __init__(self, store: SqliteEventStore, project_store: ProjectStore) -> None:
        self._store = store
        self._project_store = project_store
        self._tasks: dict[str, Any] = {}
        self._workspace = WorkspaceCoordinator(self)
        self.kick = MagicMock()
        self._loop_for = MagicMock()
        self._loop_for.return_value.enter_planning = AsyncMock()

    def _surface_of(self, conversation_id: str) -> str:
        return "build"

    def _project_store_now(self) -> ProjectStore:
        return self._project_store

    def workspace_lock(self, conversation_id: str) -> asyncio.Lock:
        return self._workspace.lock(conversation_id)


# ---- helpers -----------------------------------------------------------------


def _make_rt(store: SqliteEventStore, tmp_path: Path) -> _Runtime:
    """Faithful runtime: real store + real WorkspaceCoordinator, trackable
    kick/_loop_for spies. A replan lands observable events in `store`."""
    return _Runtime(store, ProjectStore(str(tmp_path / "projects")))


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


async def _assert_replan_landed(
    rt: _Runtime,
    store: SqliteEventStore,
    *,
    text: str,
    claimed_user_seq: int,
    events_before: list[Any],
) -> None:
    """Observable proof the atomic planning ingress + kick actually landed.

    Reads the REAL event log (not mock bookkeeping): exactly one user echo, one
    RUNNING/planning StatusEvent, and one agent.run-intent.request-plan(rpv=1)
    marker, appended contiguously and strictly after everything that pre-existed
    (the old workspace seal is invalidated AFTER the prior state, never before),
    with a single kick carrying the new user turn's seq. The superseded two-step
    replan path (_loop_for(...).enter_planning) is never taken.
    """
    events = await store.get_events(CID)
    state = await store.get_state(CID)

    # Exactly one kick, carrying the new user turn's seq.
    rt.kick.assert_called_once_with(CID, claimed_user_seq=claimed_user_seq)

    # The conversation is now RUNNING (planning), not its prior state.
    assert state.execution_status == ConversationStatus.RUNNING

    echoes = [
        e
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.USER
        and e.message.content == text
    ]
    assert len(echoes) == 1, "exactly one user echo must land"

    planning = _planning_statuses(events)
    assert len(planning) == 1, "exactly one RUNNING/planning status must land"

    run_intents = _run_intents(events)
    assert len(run_intents) == 1, "exactly one run-intent marker must land"
    assert run_intents[0].run_protocol_version == 1

    # Atomic ordering: echo -> RUNNING/planning -> run-intent are contiguous, and
    # the whole ingress lands strictly after every pre-existing event.
    assert planning[0].seq == echoes[0].seq + 1
    assert run_intents[0].seq == planning[0].seq + 1
    assert echoes[0].seq > max((e.seq or 0 for e in events_before), default=0)

    # The superseded two-step replan (_loop_for(...).enter_planning) is dead.
    rt._loop_for.assert_not_called()
    rt._loop_for.return_value.enter_planning.assert_not_awaited()


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


async def test_ship_it_post_finish_appends_message_no_planning_status(tmp_path: Path) -> None:
    """'publish it and be done' on a FINISHED conversation: user message is
    appended (UI echo resolves), but NO planning ingress and NO re-plan."""
    store = SqliteEventStore(":memory:")
    await _finished_conversation(store)
    rt = _make_rt(store, tmp_path)
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
    assert not _planning_statuses(events), "no RUNNING/planning status on ship-it path"

    # No durable run-intent marker must have landed (observable guard replacing
    # the vacuous assertion against the superseded record_run_intent_locked name).
    assert not _run_intents(events), "no run-intent marker on ship-it path"

    # The conversation must still be FINISHED.
    state = await store.get_state(CID)
    assert state.execution_status == ConversationStatus.FINISHED

    # No PlanEvent (no revision-2 plan).
    plan_events = [e for e in events if isinstance(e, PlanEvent)]
    assert not plan_events, "no PlanEvent on ship-it path"

    # The replan machinery must NOT have been engaged.
    rt._loop_for.assert_not_called()
    rt._loop_for.return_value.enter_planning.assert_not_awaited()
    rt.kick.assert_not_called()


async def test_ship_it_case_insensitive(tmp_path: Path) -> None:
    """Ship-it check is case-insensitive — 'YOU'RE DONE' must be caught."""
    store = SqliteEventStore(":memory:")
    await _finished_conversation(store)
    rt = _make_rt(store, tmp_path)
    ops = ControlOps(rt)

    await ops.request_plan(CID, "YOU'RE DONE")

    state = await store.get_state(CID)
    assert state.execution_status == ConversationStatus.FINISHED
    assert not _run_intents(await store.get_events(CID))
    rt.kick.assert_not_called()


async def test_ship_it_all_stop_phrases_skip_replan(tmp_path: Path) -> None:
    """Every stop phrase (both substring + exact sets) must skip the replan ingress
    + kick, INCLUDING the real traced phrasing with a leading clause (codex MAJOR),
    and a trailing-punctuation / extra-whitespace variant."""
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
        rt = _make_rt(store, tmp_path)
        ops = ControlOps(rt)

        await ops.request_plan(CID, phrase)

        rt.kick.assert_not_called()
        assert not _run_intents(await store.get_events(CID)), (
            f"no run-intent marker may land for phrase {phrase!r}"
        )
        rt._loop_for.return_value.enter_planning.assert_not_awaited()


async def test_mid_sentence_stop_phrase_still_replans(tmp_path: Path) -> None:
    """A short stop phrase embedded in a real change request must NOT skip replan —
    'you're done with X, now add Y' is a change request, not a ship-it. The full
    atomic planning ingress must land and a new turn kicks at the new user seq."""
    store = SqliteEventStore(":memory:")
    await _finished_conversation(store)
    rt = _make_rt(store, tmp_path)
    ops = ControlOps(rt)
    before = await store.get_events(CID)

    text = "you're done with the header, now add a footer"
    await ops.request_plan(CID, text)

    await _assert_replan_landed(rt, store, text=text, claimed_user_seq=3, events_before=before)


# ---- request_plan: replan path (FINISHED + real change intent) ---------------


async def test_real_change_intent_post_finish_still_replans(tmp_path: Path) -> None:
    """'add a dark mode toggle' on a FINISHED conversation must still trigger the
    replan path: the atomic planning ingress (echo + RUNNING/planning + run-intent)
    lands and a new turn kicks carrying the new user seq (3, appended after the
    seed's user msg @1 and FINISHED @2)."""
    store = SqliteEventStore(":memory:")
    await _finished_conversation(store)
    rt = _make_rt(store, tmp_path)
    ops = ControlOps(rt)
    before = await store.get_events(CID)

    text = "add a dark mode toggle"
    await ops.request_plan(CID, text)

    await _assert_replan_landed(rt, store, text=text, claimed_user_seq=3, events_before=before)


async def test_publish_to_netlify_post_finish_still_replans(tmp_path: Path) -> None:
    """'publish to Netlify' is an ambiguous real-deploy intent — must replan."""
    store = SqliteEventStore(":memory:")
    await _finished_conversation(store)
    rt = _make_rt(store, tmp_path)
    ops = ControlOps(rt)
    before = await store.get_events(CID)

    text = "publish to Netlify"
    await ops.request_plan(CID, text)

    await _assert_replan_landed(rt, store, text=text, claimed_user_seq=3, events_before=before)


async def test_bare_publish_post_finish_still_replans(tmp_path: Path) -> None:
    """Bare 'publish it' is ambiguous — must replan, not be treated as stop."""
    store = SqliteEventStore(":memory:")
    await _finished_conversation(store)
    rt = _make_rt(store, tmp_path)
    ops = ControlOps(rt)
    before = await store.get_events(CID)

    text = "publish it"
    await ops.request_plan(CID, text)

    await _assert_replan_landed(rt, store, text=text, claimed_user_seq=3, events_before=before)


async def test_replan_ingress_is_atomic_no_orphan_kick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FAILURE-INJECTION: if the single append that carries the whole ingress
    fails, the error propagates, NO kick is issued (no orphan turn against a
    torn head), and NOTHING partial lands — the conversation stays exactly as it
    was. This is the ingress-isolation guarantee: a peer sees either the old
    execution state or the complete planning ingress, never a half-write."""
    store = SqliteEventStore(":memory:")
    await _finished_conversation(store)
    rt = _make_rt(store, tmp_path)
    ops = ControlOps(rt)
    before = await store.get_events(CID)

    async def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("injected ingress failure")

    monkeypatch.setattr(store, "append_many", _boom)

    with pytest.raises(RuntimeError, match="injected ingress failure"):
        await ops.request_plan(CID, "add a dark mode toggle")

    after = await store.get_events(CID)
    state = await store.get_state(CID)
    rt.kick.assert_not_called()
    assert [e.id for e in after] == [e.id for e in before], "nothing partial may land"
    assert state.execution_status == ConversationStatus.FINISHED
    assert not _planning_statuses(after)
    assert not _run_intents(after)


# ---- request_plan: guard only applies when FINISHED -------------------------


async def test_ship_it_phrase_mid_run_still_replans(tmp_path: Path) -> None:
    """The ship-it guard is ONLY for FINISHED conversations. The same phrase on
    a non-FINISHED conversation (e.g. IDLE — a fresh conversation) must fall
    through to the replan path. IDLE has no prior events, so the new user msg is
    seq 1 → claimed_user_seq=1."""
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    # IDLE by default (no StatusEvent appended) — NOT FINISHED.
    rt = _make_rt(store, tmp_path)
    ops = ControlOps(rt)
    before = await store.get_events(CID)

    text = "publish it and be done"
    await ops.request_plan(CID, text)

    await _assert_replan_landed(rt, store, text=text, claimed_user_seq=1, events_before=before)


async def test_ship_it_phrase_awaiting_approval_still_replans(tmp_path: Path) -> None:
    """AWAITING_PLAN_APPROVAL is not FINISHED — the stop-phrase check must not
    fire; the conversation continues through the replan path. The AWAITING status
    is seq 1, so the new user msg is seq 2 → claimed_user_seq=2."""
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    await store.append(
        CID,
        StatusEvent(status=ConversationStatus.AWAITING_PLAN_APPROVAL),
    )
    rt = _make_rt(store, tmp_path)
    ops = ControlOps(rt)
    before = await store.get_events(CID)

    text = "we're done"
    await ops.request_plan(CID, text)

    await _assert_replan_landed(rt, store, text=text, claimed_user_seq=2, events_before=before)
