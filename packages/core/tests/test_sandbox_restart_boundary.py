"""Host-observed runtime loss is semantic across persistence and strict replay."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from disco.core import (
    ActionEvent,
    ConversationState,
    ConversationStatus,
    EventSource,
    LLMMessage,
    MessageEvent,
    SqliteEventStore,
    StatusEvent,
    ToolCall,
    WorkspaceMutationEvent,
)
from disco.core.events import (
    EventAdapter,
    current_workspace_agent_view_id,
    derive_final_workspace_fence,
    latest_workspace_run_intent,
    pending_workspace_run_intent,
    workspace_run_intent_admission_required,
)
from disco.core.loop import check_ledger
from disco.core.loop.engine_contracts import _ACTIVE_AGENT_VIEW_ID
from disco.core.loop.finish._shell_diagnostics import _record_state, _shell_caveat
from disco.core.loop.finish.finalize_parts.verify_receipt import reuse_finish_verify_receipt
from disco.core.loop.observation_execution import maybe_emit_sandbox_restart
from event_fakes import with_seqs
from harness.replay_runner import diff_sequences, normalize_event
from test_check_ledger import CID, _GateExecutor, _make_loop, _pure


async def _restart_events(view_id=None):
    loop = _make_loop(_GateExecutor(None))
    token = _ACTIVE_AGENT_VIEW_ID.set(view_id)
    try:
        await maybe_emit_sandbox_restart(loop, SimpleNamespace(generation=4), 3)
    finally:
        _ACTIVE_AGENT_VIEW_ID.reset(token)
    return await loop._events()


async def test_restart_boundary_is_semantic_and_dropped_or_changed_boundary_fails_replay():
    events = await _restart_events()
    marker, notice = events
    assert isinstance(marker, WorkspaceMutationEvent)
    assert marker.operation == "sandbox.restarted"
    assert marker.source is EventSource.SYSTEM
    assert isinstance(notice, MessageEvent)
    assert marker.seq < notice.seq
    normalized = normalize_event(marker)
    assert "meta" not in normalized
    assert normalized["operation"] == "sandbox.restarted"
    decoded = EventAdapter.validate_python(normalized)
    assert isinstance(decoded, WorkspaceMutationEvent)
    assert decoded.operation == "sandbox.restarted"
    assert (
        diff_sequences(events, [EventAdapter.validate_json(e.model_dump_json()) for e in events])
        == []
    )
    assert diff_sequences(events, [notice])
    assert diff_sequences(events, [marker.model_copy(update={"operation": "editor.write"}), notice])


async def test_restart_survives_sqlite_reopen_json_roundtrip_and_metadata_free_receipt_replay(
    tmp_path,
):
    path = str(tmp_path / "events.sqlite")
    store = SqliteEventStore(path)
    for event in _pure("check", "bash smoke.sh") + await _restart_events():
        await store.append(CID, event.model_copy(update={"seq": None}))
    store.close()
    reopened = SqliteEventStore(path)
    try:
        stored = await reopened.get_events(CID)
        decoded = [EventAdapter.validate_json(e.model_dump_json()) for e in stored]
        assert diff_sequences(stored, decoded) == []
        caveat = _shell_caveat(decoded)
        record = check_ledger.check_records(decoded)[0]
        state = _record_state(record, {}, decoded)
        assert state != _record_state(record, {}, decoded[:-2])
        assert caveat is not None and state in caveat
        metadata_free = [e.model_copy(update={"meta": {}}) for e in decoded]
        loop = _make_loop(_GateExecutor(None))
        assert not await reuse_finish_verify_receipt(loop, "bash smoke.sh", metadata_free)
        assert await loop._events() == []
    finally:
        reopened.close()


@pytest.mark.parametrize(
    "status",
    [
        ConversationStatus.RUNNING,
        ConversationStatus.WAITING_FOR_CONFIRMATION,
    ],
)
async def test_restart_keeps_same_intent_view_and_pending_gate(status):
    intent = WorkspaceMutationEvent(operation="agent.run-intent.user-turn", run_protocol_version=1)
    admission = WorkspaceMutationEvent(
        operation="agent.view-admitted",
        run_protocol_version=1,
        run_intent_id=intent.id,
        agent_view_id="view-current",
    )
    action = ActionEvent(
        thought="An explicit action awaits policy",
        agent_view_id="view-current",
        tool_call=ToolCall(tool_name="shell", arguments={"command": "bash smoke.sh"}),
    )
    events = with_seqs(
        [
            intent,
            admission,
            action,
            MessageEvent(
                source=EventSource.AGENT,
                agent_view_id="view-current",
                message=LLMMessage(role="assistant", content="Working"),
            ),
            StatusEvent(status=status, agent_view_id="view-current"),
        ]
    )
    before = ConversationState.reconstruct(CID, events)
    after_events = with_seqs(events + await _restart_events("view-current"))
    after = ConversationState.reconstruct(CID, after_events)
    assert latest_workspace_run_intent(after_events).id == intent.id
    assert current_workspace_agent_view_id(after_events) == "view-current"
    assert not workspace_run_intent_admission_required(after_events)
    assert pending_workspace_run_intent(after_events) is None
    assert after.execution_status is before.execution_status
    assert after.pending_action_id == before.pending_action_id
    if status is ConversationStatus.WAITING_FOR_CONFIRMATION:
        assert after.pending_action_id == action.id
    assert after.pending_alternatives_id == before.pending_alternatives_id
    assert after.active_agent_view_id == before.active_agent_view_id


async def test_restart_invalidates_old_final_fence_and_next_finish_fences_runtime_boundary():
    events = with_seqs([StatusEvent(status=ConversationStatus.FINISHED)])
    assert derive_final_workspace_fence(events) == (1, None)
    events = with_seqs(events + await _restart_events())
    with pytest.raises(ValueError, match="effect event must precede"):
        derive_final_workspace_fence(events)
    later = with_seqs(events + [StatusEvent(status=ConversationStatus.FINISHED)])
    terminal, effect = derive_final_workspace_fence(later)
    assert terminal == 4 and effect == 2


async def test_restart_metadata_or_prose_alone_does_not_manufacture_semantic_boundary():
    message = MessageEvent(
        source=EventSource.ENVIRONMENT,
        message=LLMMessage(role="user", content="sandbox restarted"),
        meta={"sandbox_restart": {"generation_before": 1, "generation_after": 9}},
    )
    restored = EventAdapter.validate_json(message.model_dump_json())
    assert not isinstance(restored, WorkspaceMutationEvent)
    assert diff_sequences([message], [message.model_copy(update={"meta": {}})]) == []
    events = with_seqs(_pure("check", "bash smoke.sh") + [restored])
    assert _shell_caveat(events) is None  # No typed runtime observation was supplied.
