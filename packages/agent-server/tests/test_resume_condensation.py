"""DC-05c — resume-time condensation of degenerate trailing segments (unit tests)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from disco.agent_server import ConversationRuntime
from disco.core import (
    ActionEvent,
    CondensationEvent,
    ConversationStatus,
    EventSource,
    KnowledgeEvent,
    MessageEvent,
    ObservationEvent,
    PlanEvent,
    SqliteEventStore,
    StatusEvent,
    ToolCall,
    ToolResult,
    View,
    event_from_json_dict,
)
from disco.core.llm import (
    CompletionResponse,
    ConfigStore,
    DefaultLLMRouter,
    RouterConfig,
    TokenUsage,
)
from disco.core.migration import migrate_event

FIXTURE_PATH = (
    Path(__file__).parent.parent.parent.parent
    / "test-record"
    / "marathon"
    / "events-conv_c1b4675689484c63b1f45d92d45b95be-attempt3-loop-snapshot.json"
)

class _FakeProvider:
    name = "fake"
    async def complete(self, req, *, model):
        return CompletionResponse(
            text="done",
            tool_calls=[],
            usage=TokenUsage(input_tokens=1, output_tokens=1),
            finish_reason="stop",
            model_used=model,
            request_id=req.request_id,
            routing=None,
        )
    
    async def stream_complete(self, req, *, model):
        yield CompletionResponse(
            text="done",
            tool_calls=[],
            usage=TokenUsage(input_tokens=1, output_tokens=1),
            finish_reason="stop",
            model_used=model,
            request_id=req.request_id,
            routing=None,
        )

    def supports(self, requirement, *, model):
        return True

def _runtime(store: SqliteEventStore) -> ConversationRuntime:
    cfg = RouterConfig.model_validate({
        "models": {"m": {"model_id": "m", "provider": "fake", "context_window": 8192}},
        "default_model": "m",
    })
    router = DefaultLLMRouter(cfg, {"fake": _FakeProvider()})
    return ConversationRuntime(
        store, router=router, config_store=ConfigStore(path=Path("/dev/null"))
    )

def _msg(role: str, content: str, seq: int) -> MessageEvent:
    return MessageEvent(
        seq=seq,
        source=EventSource.AGENT if role == "assistant" else EventSource.USER,
        message={"role": role, "content": content}
    )

def _knowledge(snippet: str, seq: int, scope: str = "") -> KnowledgeEvent:
    return KnowledgeEvent(seq=seq, snippet=snippet, scope=scope)

def _plan(summary: str, seq: int, revision: int = 1) -> PlanEvent:
    return PlanEvent(seq=seq, summary=summary, steps=[], revision=revision)

def _action(tool: str, seq: int) -> ActionEvent:
    return ActionEvent(
        seq=seq,
        thought="thinking",
        tool_call=ToolCall(call_id=f"call_{seq}", tool_name=tool, arguments={})
    )

@pytest.mark.asyncio
async def test_healthy_tail():
    # healthy tail (actions present) → None
    store = SqliteEventStore(":memory:")
    runtime = _runtime(store)
    events = [
        _msg("user", "hi", 1),
        _action("ls", 2),
        ObservationEvent(
            seq=3,
            action_id="call_2",
            tool_result=ToolResult(
                call_id="call_2", tool_name="ls", success=True, content="files"
            ),
        ),
        _msg("assistant", "I see files", 4)
    ]
    assert runtime._condense_trailing_degeneracy(events) is None

@pytest.mark.asyncio
async def test_breaker_paused_tail():
    # breaker-paused tail (3 prose messages then PAUSED) → None
    # (below threshold 6 — DC-05a owns that case)
    store = SqliteEventStore(":memory:")
    runtime = _runtime(store)
    events = [
        _msg("user", "hi", 1),
        _action("ls", 2),
        _msg("assistant", "one", 3),
        _msg("assistant", "two", 4),
        _msg("assistant", "three", 5),
    ]
    assert runtime._condense_trailing_degeneracy(events) is None

@pytest.mark.asyncio
async def test_degenerate_prose_tail():
    # degenerate tail (8 agent messages, 0 actions) → tombstone spanning them
    store = SqliteEventStore(":memory:")
    runtime = _runtime(store)
    events = [
        _msg("user", "hi", 1),
        _action("ls", 2), # last action
    ]
    # append 8 agent messages
    for i in range(8):
        events.append(_msg("assistant", f"message {i}", 3 + i))
    
    tombstone = runtime._condense_trailing_degeneracy(events)
    assert tombstone is not None
    assert tombstone.forgotten_start_seq == 3
    assert tombstone.forgotten_end_seq == 10
    assert "8 degenerate turns" in tombstone.summary
    assert "0 duplicate knowledge entries" in tombstone.summary
    assert "0 plan revisions" in tombstone.summary

@pytest.mark.asyncio
async def test_duplicate_knowledge_tail():
    # duplicate-knowledge tail (1 fact ×10) → tombstone,
    # FIRST instance's seq outside the forgotten span
    store = SqliteEventStore(":memory:")
    runtime = _runtime(store)
    fact = "Always use absolute paths."
    events = [
        _msg("user", "hi", 1),
        _knowledge(fact, 2), # First instance (seq 2)
    ]
    # 10 more instances
    for i in range(10):
        events.append(_knowledge(fact, 3 + i))
    
    tombstone = runtime._condense_trailing_degeneracy(events)
    assert tombstone is not None
    # Seq 2 (first instance) is OUTSIDE. So span starts at seq 3.
    assert tombstone.forgotten_start_seq == 3
    assert tombstone.forgotten_end_seq == 12
    assert "10 degenerate turns" in tombstone.summary
    assert "10 duplicate knowledge entries" in tombstone.summary

@pytest.mark.asyncio
async def test_defect4_replay():
    # DEFECT-4 attempt-3 replay
    if not FIXTURE_PATH.exists():
        pytest.skip("Fixture not found")
        
    with open(FIXTURE_PATH) as f:
        data = json.load(f)
    
    events = []
    for d in data:
        events.append(event_from_json_dict(migrate_event(d)))

    store = SqliteEventStore(":memory:")
    runtime = _runtime(store)
    
    tombstone = runtime._condense_trailing_degeneracy(events)
    assert tombstone is not None
    
    # "then View.of(events + [tombstone]) renders WITHOUT the ×179 repeats"
    view_full = View.of(events)
    view_condensed = View.of(events + [tombstone])
    
    # The fixture has ~179 repeats of one fact.
    # The condensed view should be much smaller.
    assert len(view_condensed.messages) < len(view_full.messages) - 100

@pytest.mark.asyncio
async def test_integration_resume():
    # integration: resume_conversation on a degenerate log → tombstone in store
    store = SqliteEventStore(":memory:")
    runtime = _runtime(store)
    cid = "test-cid"
    
    # Setup degenerate conversation in PAUSED state
    await store.append(cid, _msg("user", "hi", 1))
    for i in range(10):
        await store.append(cid, _msg("assistant", f"repeat {i}", 2 + i))
    # Must append PAUSED status to be legal for resume
    await store.append(cid, StatusEvent(seq=12, status=ConversationStatus.PAUSED))
    
    # resume
    res = await runtime.resume_conversation(cid)
    assert res["ok"] is True
    
    # Check store for tombstone
    final_events = await store.get_events(cid)
    
    tombstones = [e for e in final_events if isinstance(e, CondensationEvent)]
    assert len(tombstones) == 1
    assert tombstones[0].forgotten_start_seq == 2
    # The segment includes the StatusEvent at seq 12.
    assert tombstones[0].forgotten_end_seq == 12
    
    # Verify reconstruction events came AFTER tombstone
    tomb_idx = next(i for i, e in enumerate(final_events) if isinstance(e, CondensationEvent))
    running_idx = next(
        i
        for i, e in enumerate(final_events)
        if isinstance(e, StatusEvent) and e.status == ConversationStatus.RUNNING
    )
    assert tomb_idx < running_idx
