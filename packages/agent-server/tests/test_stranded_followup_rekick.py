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

    rt.kick.assert_called_once_with(CID)
    # The follow-up's seq is recorded so a stalled same-seq segment can't loop.
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
    rt.kick.assert_called_once_with(CID)


# ── strand repro: STUCK (RUNNING → max-attempts wedge) ──────────────────────────


@pytest.mark.asyncio
async def test_stuck_wedge_rekicks_stranded_followup(tmp_path, monkeypatch):
    rt = _rt(tmp_path, monkeypatch)
    rt._store.create_conversation(CID, owner_id="local")
    await rt._store.append(CID, _user("build me a landing page"))
    await rt._store.append(CID, _action())
    await rt._store.append(CID, StatusEvent(status=ConversationStatus.RUNNING))
    await rt._store.append(CID, _user("make it dark mode"))  # stranded follow-up
    # Already re-kicked once for the silent stall → the next finalize takes the
    # STUCK-append (wedge) path rather than the non-terminal recovery re-kick.
    rt._nonterminal_rekicks[CID] = 1
    rt.kick = MagicMock()  # type: ignore[method-assign]

    await rt._finalize_clean_return(CID)

    state = await rt._store.get_state(CID)
    assert state.execution_status is ConversationStatus.STUCK
    rt.kick.assert_called_once_with(CID)


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
