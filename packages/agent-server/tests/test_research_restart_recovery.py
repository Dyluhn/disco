"""Startup recovery uses committed state, preserves budgets, and never fabricates a report."""

from unittest.mock import MagicMock

import pytest
from disco.agent_server import ConversationRuntime
from disco.core import (
    ActionEvent,
    ConversationStatus,
    ErrorEvent,
    EventSource,
    LLMMessage,
    MessageEvent,
    ReportEvent,
    ReportSection,
    ResearchCheckpointEvent,
    SqliteEventStore,
    StatusEvent,
    ToolCall,
)
from disco.retrieval.deep_research._agent_state import _AgentState
from disco.retrieval.deep_research._budget import SourceBudget
from disco.retrieval.deep_research._recovery_state import (
    AgentCheckpoint,
    LoopCursor,
    RecoveryCheckpoint,
)
from disco.retrieval.deep_research.depth import bounds_for
from disco.retrieval.deep_research.recovery import RECOVERY_ACTION, write_checkpoint
from disco.tools import ProcessSandboxService

CID = "conv_restart_recovery"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCO_DATA_DIR", str(tmp_path))
    value = SqliteEventStore(":memory:")
    value.create_conversation(CID, owner_id="local", surface="deep_research")
    yield value
    value.close()


async def begin(store):
    await store.append(
        CID,
        MessageEvent(
            source=EventSource.USER,
            message=LLMMessage(role="user", content="Investigate limits"),
        ),
    )
    await store.append(CID, StatusEvent(status=ConversationStatus.RUNNING))


def save(turns, run_id="same_execution"):
    bound = bounds_for("quick")
    state = _AgentState(budget=SourceBudget(bound.max_sources))
    state.turns_completed = state.turns_charged = turns
    state.trail = [{"kind": "steer", "text": "Keep the original limitations visible."}]
    return write_checkpoint(
        RecoveryCheckpoint(
            conversation_id=CID,
            run_id=run_id,
            query="Investigate limits",
            depth_tier="quick",
            stage="research",
            bound=bound,
            state=AgentCheckpoint.capture(state),
            cursor=LoopCursor(next_turn=turns, ceiling_tokens=8192),
        )
    )


async def commit(store, reference):
    await store.append(
        CID,
        ActionEvent(
            thought="Saved research boundary",
            tool_call=ToolCall(tool_name=RECOVERY_ACTION, arguments=reference),
        ),
    )


async def reconcile(store):
    runtime = ConversationRuntime(
        store, router=MagicMock(), sandbox_service=ProcessSandboxService()
    )
    await runtime.lifecycle.reconcile_orphaned_runs()
    return await store.get_events(CID)


def damage(tmp_path, reference):
    target = (
        tmp_path / "research-recovery" / CID / "checkpoints" / f"{reference['checkpoint_id']}.json"
    )
    target.write_text("damaged")
    return target


async def test_latest_corrupt_checkpoint_falls_back_to_committed_state(store, tmp_path):
    await begin(store)
    earlier = save(1)
    await commit(store, earlier)
    latest = save(2)
    await commit(store, latest)
    damaged = damage(tmp_path, latest)
    events = await reconcile(store)
    checkpoint = next(event for event in events if isinstance(event, ResearchCheckpointEvent))
    assert checkpoint.meta["research_recovery_ref"] == earlier
    assert checkpoint.meta["recovery_fallback"] is True
    assert checkpoint.meta["turns_remaining"] == bounds_for("quick").max_research_turns - 1
    assert checkpoint.trail == [{"kind": "steer", "text": "Keep the original limitations visible."}]
    assert (await store.get_state(CID)).execution_status is ConversationStatus.PAUSED
    assert damaged.read_text() == "damaged"
    assert not any(isinstance(event, ReportEvent) for event in events)
    assert await reconcile(store) == events


@pytest.mark.parametrize("failure", ["corrupt", "unsupported"])
async def test_no_readable_committed_checkpoint_is_an_explicit_error(store, tmp_path, failure):
    await begin(store)
    reference = save(2)
    if failure == "corrupt":
        damage(tmp_path, reference)
    else:
        reference["schema_version"] = 999
    await commit(store, reference)
    events = await reconcile(store)
    assert (await store.get_state(CID)).execution_status is ConversationStatus.ERROR
    assert any(
        isinstance(event, ErrorEvent) and event.code == "deep_research_recovery_unavailable"
        for event in events
    )
    assert not any(isinstance(event, (ReportEvent, ResearchCheckpointEvent)) for event in events)
    assert list((tmp_path / "research-recovery" / CID / "checkpoints").glob("*.json"))


async def test_uncommitted_file_never_becomes_recovery_state(store):
    await begin(store)
    committed = save(1)
    await commit(store, committed)
    save(3)
    events = await reconcile(store)
    checkpoint = next(event for event in events if isinstance(event, ResearchCheckpointEvent))
    assert checkpoint.meta["research_recovery_ref"] == committed
    assert checkpoint.meta["recovery_fallback"] is False


async def test_legacy_committed_report_finishes_without_second_report(store):
    await begin(store)
    await store.append(
        CID,
        ReportEvent(
            query="Already written",
            summary="Saved result",
            sections=[ReportSection(id="findings", title="Findings", markdown="Saved result")],
        ),
    )
    events = await reconcile(store)
    assert (await store.get_state(CID)).execution_status is ConversationStatus.FINISHED
    assert sum(isinstance(event, ReportEvent) for event in events) == 1
    assert await reconcile(store) == events


async def test_corrupt_resumed_boundary_falls_back_within_the_same_research_run(store, tmp_path):
    await begin(store)
    earlier = save(1)
    await commit(store, earlier)
    await reconcile(store)
    await store.append(CID, StatusEvent(status=ConversationStatus.RUNNING))
    latest = save(2)
    await commit(store, latest)
    damage(tmp_path, latest)
    events = await reconcile(store)
    checkpoint = [event for event in events if isinstance(event, ResearchCheckpointEvent)][-1]
    assert (await store.get_state(CID)).execution_status is ConversationStatus.PAUSED
    assert checkpoint.meta["research_recovery_ref"] == earlier
    assert checkpoint.meta["recovery_fallback"] is True


async def test_corrupt_new_run_never_resurrects_a_previous_research_run(store, tmp_path):
    await begin(store)
    await commit(store, save(1))
    await reconcile(store)
    await store.append(CID, StatusEvent(status=ConversationStatus.RUNNING))
    latest = save(2, run_id="different_execution")
    await commit(store, latest)
    damage(tmp_path, latest)
    events = await reconcile(store)
    assert (await store.get_state(CID)).execution_status is ConversationStatus.ERROR
    assert sum(isinstance(event, ResearchCheckpointEvent) for event in events) == 1
