"""Engine-rekick fix (PART 2) — a follow-up appended while a build run is
FINALIZING must be recovered by a post-terminal re-kick.

The bug: `kick()` is a no-op while the prior run task is still live, and the
done-callback finalizers (`_finalize_clean_return` / `_terminalize_crashed`)
terminalize but never re-kick — so a follow-up that landed during the
finalization window was stranded forever (nothing started the run() that would
process it). With the predicate un-masked (PART 1), the shared
`_maybe_rekick_for_stranded_followup` re-kicks at every terminal conclusion when
the work-gate is open, guarded so it fires ONCE per new follow-up.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from disco.agent_server.runtime import ConversationRuntime
from disco.core import (
    ActionEvent,
    ConversationStatus,
    EventSource,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    SqliteEventStore,
    StatusEvent,
    ToolCall,
    ToolResult,
)
from disco.core.llm import ConfigStore, SecretBox, SecretStore
from disco.core.llm.errors import NoEligibleModel

CID = "conv_strand"


def _rt(tmp_path, monkeypatch) -> ConversationRuntime:
    monkeypatch.setenv("PMX_DB", str(tmp_path / "c.db"))
    return ConversationRuntime(
        SqliteEventStore(":memory:"),
        config_store=ConfigStore(tmp_path / "config.json"),
        secret_store=SecretStore(tmp_path / "s.json", box=SecretBox(None)),
    )


def _user(text: str) -> MessageEvent:
    return MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content=text))


def _action() -> ActionEvent:
    return ActionEvent(
        thought="writing",
        tool_call=ToolCall(tool_name="file_write", arguments={"path": "x", "content": "y"}),
    )


def _obs() -> ObservationEvent:
    return ObservationEvent(
        tool_result=ToolResult(call_id="c1", tool_name="file_write", success=True, content="ok"),
        action_id="a1",
    )


async def _seed_build(rt: ConversationRuntime, *, terminal: ConversationStatus | None) -> None:
    """Original goal + real work; optionally a terminal marker."""
    rt._store.create_conversation(CID, owner_id="local")
    await rt._store.append(CID, _user("build me a landing page"))
    await rt._store.append(CID, _action())
    await rt._store.append(CID, _obs())
    if terminal is not None:
        await rt._store.append(CID, StatusEvent(status=terminal))


# ── strand repro: CLEAN finalize (FINISHED) ────────────────────────────────────


@pytest.mark.asyncio
async def test_clean_finalize_rekicks_stranded_followup(tmp_path, monkeypatch):
    rt = _rt(tmp_path, monkeypatch)
    await _seed_build(rt, terminal=ConversationStatus.FINISHED)
    # Follow-up lands during the finalization window (after the terminal marker).
    await rt._store.append(CID, _user("now also add a footer"))
    rt.kick = MagicMock()  # type: ignore[method-assign]

    await rt._finalize_clean_return(CID)

    rt.kick.assert_called_once_with(CID, claimed_user_seq=5)
    # The follow-up's seq is recorded so a stalled same-seq segment can't loop.
    assert rt._post_terminal_rekick_seq[CID] == 5


@pytest.mark.asyncio
async def test_newer_user_turn_after_run_claim_is_rekicked_exactly_once(tmp_path, monkeypatch):
    rt = _rt(tmp_path, monkeypatch)
    await _seed_build(rt, terminal=ConversationStatus.FINISHED)
    # The run claimed its original user turn (seq 1) synchronously when its task
    # started. A genuinely newer follow-up lands while that task is finalizing.
    rt._run_claimed_user_seq[CID] = 1
    await rt._store.append(CID, _user("now also add a footer"))  # seq 5
    blocker = asyncio.create_task(asyncio.Event().wait())
    rt._tasks[CID] = blocker
    rt.kick(CID, claimed_user_seq=5)
    assert rt._run_claimed_user_seq[CID] == 1  # live task must not claim the follow-up
    rt._tasks.pop(CID)
    blocker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await blocker
    rt.kick = MagicMock()  # type: ignore[method-assign]

    await rt._finalize_clean_return(CID)
    await rt._finalize_clean_return(CID)

    rt.kick.assert_called_once_with(CID, claimed_user_seq=5)
    assert rt._post_terminal_rekick_seq[CID] == 5


# ── strand repro: CRASH terminalize (ERROR) ────────────────────────────────────


@pytest.mark.asyncio
async def test_crash_terminalize_rekicks_stranded_followup(tmp_path, monkeypatch):
    rt = _rt(tmp_path, monkeypatch)
    await _seed_build(rt, terminal=None)  # no terminal marker yet → not concluded
    await rt._store.append(CID, _user("change the headline copy"))
    rt.kick = MagicMock()  # type: ignore[method-assign]

    await rt._terminalize_crashed(CID, RuntimeError("boom"), None)

    # ERROR was appended by the terminalizer, then the stranded follow-up re-kicked.
    state = await rt._store.get_state(CID)
    assert state.execution_status is ConversationStatus.ERROR
    rt.kick.assert_called_once_with(CID, claimed_user_seq=4)


@pytest.mark.asyncio
async def test_deterministic_preflight_error_does_not_retry_original_user_turn(
    tmp_path, monkeypatch
):
    """A terminal configuration verdict claims the turn; it is not a follow-up."""
    rt = _rt(tmp_path, monkeypatch)
    rt.set_surface(CID, "agent")
    rt._store.create_conversation(CID, owner_id="local")
    initial = await rt._store.append(CID, _user("complete the task"))

    class _Loop:
        async def run(self):  # pragma: no cover - preflight must stop before the loop
            raise AssertionError("misconfigured driver must not enter the agent loop")

    class _MisconfiguredRouter:
        calls = 0

        async def complete(self, request, *, context=None):
            self.calls += 1
            raise NoEligibleModel(
                "role agent_driver is assigned to an unknown model; fix the assignment"
            )

    router = _MisconfiguredRouter()
    rt._loops[CID] = _Loop()  # type: ignore[assignment]
    rt._router_now = lambda **_kwargs: router  # type: ignore[method-assign]

    rt.kick(CID, claimed_user_seq=initial.seq)
    for _ in range(20):
        await asyncio.sleep(0)

    events = await rt._store.get_events(CID)
    errors = [
        event
        for event in events
        if isinstance(event, StatusEvent) and event.status is ConversationStatus.ERROR
    ]
    reminders = [
        event
        for event in events
        if isinstance(event, MessageEvent) and event.source is EventSource.ENVIRONMENT
    ]
    assert router.calls == 1
    assert len(errors) == 1
    assert "misconfigured" in (errors[0].detail or "")
    assert len(reminders) == 1
    assert "fix the assignment" in (reminders[0].message.content or "")


# ── strand repro: STUCK (RUNNING → max-attempts wedge) ──────────────────────────


@pytest.mark.asyncio
async def test_stuck_wedge_rekicks_stranded_followup(tmp_path, monkeypatch):
    rt = _rt(tmp_path, monkeypatch)
    rt._store.create_conversation(CID, owner_id="local")
    await rt._store.append(CID, _user("build me a landing page"))
    await rt._store.append(CID, _action())
    await rt._store.append(CID, StatusEvent(status=ConversationStatus.RUNNING))
    await rt._store.append(CID, _user("make it dark mode"))  # stranded follow-up
    # Genuinely wedged: at the no-progress re-kick cap AND no forward progress
    # since the last re-kick (watermark past the last productive action), so this
    # finalize takes the STUCK-append (wedge) path rather than another recovery
    # re-kick. (A run that keeps making progress resets the budget and never STUCKs.)
    rt._nonterminal_rekicks[CID] = rt._MAX_NONTERMINAL_REKICKS
    rt._last_rekick_progress_seq[CID] = 10_000
    rt.kick = MagicMock()  # type: ignore[method-assign]

    await rt._finalize_clean_return(CID)

    state = await rt._store.get_state(CID)
    assert state.execution_status is ConversationStatus.STUCK
    rt.kick.assert_called_once_with(CID, claimed_user_seq=4)


# ── progress-reset: a WORKING iteration never falsely STUCKs ────────────────────


@pytest.mark.asyncio
async def test_progressing_run_never_stucks(tmp_path, monkeypatch):
    """Regression (Pillar-B iteration transient-STUCK): a run that keeps making
    forward progress across many non-terminal returns must NEVER terminalize to
    STUCK — each new successful productive action resets the wedge budget."""
    rt = _rt(tmp_path, monkeypatch)
    rt._store.create_conversation(CID, owner_id="local")
    await rt._store.append(CID, _user("build me a landing page"))
    await rt._store.append(CID, StatusEvent(status=ConversationStatus.RUNNING))
    rt.kick = MagicMock()  # type: ignore[method-assign]

    boundaries = rt._MAX_NONTERMINAL_REKICKS + 5
    for i in range(boundaries):
        act = ActionEvent(
            thought="writing",
            tool_call=ToolCall(tool_name="file_write", arguments={"path": f"f{i}", "content": "y"}),
        )
        await rt._store.append(CID, act)
        await rt._store.append(
            CID,
            ObservationEvent(
                tool_result=ToolResult(
                    call_id=f"c{i}", tool_name="file_write", success=True, content="ok"
                ),
                action_id=act.id,
            ),
        )
        await rt._finalize_clean_return(CID)
        state = await rt._store.get_state(CID)
        assert state.execution_status is ConversationStatus.RUNNING, f"false STUCK at boundary {i}"
    assert rt.kick.call_count == boundaries


@pytest.mark.asyncio
async def test_no_progress_run_still_stucks(tmp_path, monkeypatch):
    """Safety net intact: a run making NO productive progress across the cap of
    non-terminal returns DOES terminalize to STUCK (never RUNNING forever)."""
    rt = _rt(tmp_path, monkeypatch)
    rt._store.create_conversation(CID, owner_id="local")
    await rt._store.append(CID, _user("build me a landing page"))
    await rt._store.append(CID, StatusEvent(status=ConversationStatus.RUNNING))
    rt.kick = MagicMock()  # type: ignore[method-assign]

    stuck = False
    for _ in range(rt._MAX_NONTERMINAL_REKICKS + 2):
        await rt._finalize_clean_return(CID)
        if (await rt._store.get_state(CID)).execution_status is ConversationStatus.STUCK:
            stuck = True
            break
    assert stuck, "a no-progress wedge must still reach STUCK"


# ── guard: a clean finish with NO follow-up never re-kicks ──────────────────────


@pytest.mark.asyncio
async def test_no_followup_does_not_rekick(tmp_path, monkeypatch):
    rt = _rt(tmp_path, monkeypatch)
    await _seed_build(rt, terminal=ConversationStatus.FINISHED)  # no follow-up
    rt.kick = MagicMock()  # type: ignore[method-assign]

    await rt._finalize_clean_return(CID)

    rt.kick.assert_not_called()
    assert CID not in rt._post_terminal_rekick_seq


# ── guard: same-seq never loops; a NEWER follow-up recovers once ────────────────


@pytest.mark.asyncio
async def test_same_seq_does_not_loop_but_newer_seq_recovers(tmp_path, monkeypatch):
    rt = _rt(tmp_path, monkeypatch)
    await _seed_build(rt, terminal=ConversationStatus.FINISHED)
    await rt._store.append(CID, _user("add a footer"))  # seq 5
    rt.kick = MagicMock()  # type: ignore[method-assign]

    # First conclusion → re-kicks for the follow-up once.
    await rt._finalize_clean_return(CID)
    assert rt.kick.call_count == 1
    assert rt._post_terminal_rekick_seq[CID] == 5

    # The re-kicked run produced NO real progress and concluded again (FINISHED
    # appended, still the same latest follow-up seq) → must NOT re-kick again.
    await rt._store.append(CID, StatusEvent(status=ConversationStatus.FINISHED))  # seq 6
    await rt._finalize_clean_return(CID)
    assert rt.kick.call_count == 1  # no loop on the same follow-up

    # A genuinely NEWER follow-up → recovers exactly once more.
    await rt._store.append(CID, _user("actually make it blue"))  # seq 7
    await rt._finalize_clean_return(CID)
    assert rt.kick.call_count == 2
    assert rt._post_terminal_rekick_seq[CID] == 7
