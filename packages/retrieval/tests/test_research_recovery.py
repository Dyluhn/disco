"""Recovery retains identity and consumed work, and rejects damaged durable state."""

from pathlib import Path

import pytest
from disco.retrieval.deep_research._agent_state import _AgentState
from disco.retrieval.deep_research._budget import SourceBudget
from disco.retrieval.deep_research._durable_files import atomic_write_bytes
from disco.retrieval.deep_research._recovery_state import (
    AgentCheckpoint,
    LoopCursor,
    RecoveryCheckpoint,
)
from disco.retrieval.deep_research.depth import bounds_for
from disco.retrieval.deep_research.recovery import (
    ResearchRecoveryError,
    read_checkpoint,
    write_checkpoint,
)
from disco.retrieval.models import Passage, RetrievalResult


def saved_checkpoint(cid="conv_recovery"):
    bound = bounds_for("quick")
    state = _AgentState(budget=SourceBudget(bound.max_sources))
    passage = Passage(
        id="p1",
        source_url="https://source.test/report",
        source_title="Original",
        text="An original observation with enough substantive evidence to be admitted.",
    )
    state.admit_retrieved(
        RetrievalResult(passages=[passage], all_hits=[], extracted=[], issued_queries=[])
    )
    state.turns_charged = state.turns_completed = 2
    state.trail = [
        {"kind": "steer", "text": "Keep the original limitations visible."},
        {"kind": "search", "query": "original limitations", "turn": 1},
    ]
    state.coverage = {"covered": [{"angle": "limits", "evidence_ids": ["p1"]}], "open": []}
    state.reissue.enqueue("untested counterexample", turn=1, reason="transport_failed")
    return RecoveryCheckpoint(
        conversation_id=cid,
        run_id="execution_one",
        query="Investigate limits",
        depth_tier="quick",
        stage="research",
        bound=bound,
        state=AgentCheckpoint.capture(state),
        cursor=LoopCursor(next_turn=2, ceiling_tokens=8192),
    )


def test_checkpoint_roundtrip_preserves_budget_steering_identity_and_pending_work(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("DISCO_DATA_DIR", str(tmp_path))
    saved = saved_checkpoint()
    reference = write_checkpoint(saved)
    loaded = read_checkpoint(saved.conversation_id, reference)
    restored = loaded.state.restore(loaded.bound)
    assert restored.turns_charged == 2
    assert restored.budget.used == 1
    assert restored.budget.remaining == saved.bound.max_sources - 1
    assert restored.trail == saved.state.trail
    assert restored.coverage == saved.state.coverage
    assert restored.pool == saved.state.passages
    assert restored.reissue.queries == ("untested counterexample",)
    assert loaded.cursor.next_turn == 2


def test_checkpoints_share_passage_bodies_without_truncating_them(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCO_DATA_DIR", str(tmp_path))
    first = saved_checkpoint()
    first.state.passages[0] = first.state.passages[0].model_copy(
        update={"text": "Full source body. " * 2000}
    )
    write_checkpoint(first)
    second = first.model_copy(deep=True)
    second.state.decision_summary = "A different next decision."
    reference = write_checkpoint(second)
    directory = tmp_path / "research-recovery" / first.conversation_id
    assert len(list((directory / "passages").glob("*.json"))) == 1
    assert len(list((directory / "checkpoints").glob("*.json"))) == 2
    assert read_checkpoint(first.conversation_id, reference).state.passages == first.state.passages


def test_corrupt_content_and_cross_conversation_references_are_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCO_DATA_DIR", str(tmp_path))
    saved = saved_checkpoint()
    reference = write_checkpoint(saved)
    with pytest.raises(ResearchRecoveryError):
        read_checkpoint("conv_someone_else", reference)
    target = (
        tmp_path
        / "research-recovery"
        / saved.conversation_id
        / "checkpoints"
        / f"{reference['checkpoint_id']}.json"
    )
    target.write_text("{}")
    with pytest.raises(ResearchRecoveryError, match="hash"):
        read_checkpoint(saved.conversation_id, reference)


def test_failed_atomic_replacement_keeps_the_previous_complete_file(tmp_path, monkeypatch):
    from disco.retrieval.deep_research import _durable_files

    target = tmp_path / "pool.json"
    atomic_write_bytes(target, b'{"version":"previous"}')

    def interrupted(source: Path, destination: Path):
        raise OSError("interrupted before the atomic replacement")

    monkeypatch.setattr(_durable_files.os, "replace", interrupted)
    with pytest.raises(OSError):
        atomic_write_bytes(target, b'{"version":"new"}')
    assert target.read_bytes() == b'{"version":"previous"}'
    assert list(tmp_path.iterdir()) == [target]


async def test_cancel_drains_checkpoint_writer_before_deletion(tmp_path, monkeypatch):
    import asyncio
    import threading

    from disco.retrieval.deep_research import _run_checkpoint
    from disco.retrieval.deep_research.recovery import remove_research_artifacts

    monkeypatch.setenv("DISCO_DATA_DIR", str(tmp_path))
    started, release = threading.Event(), threading.Event()
    original = _run_checkpoint.write_checkpoint
    saved = saved_checkpoint()

    def delayed(checkpoint):
        started.set()
        assert release.wait(timeout=5)
        return original(checkpoint)

    monkeypatch.setattr(_run_checkpoint, "write_checkpoint", delayed)
    emitted = []

    async def emit(kind, payload):
        emitted.append(kind)

    owner = _run_checkpoint.RunCheckpoints(saved.conversation_id, saved.query, "quick", None, None)
    task = asyncio.create_task(owner.commit(saved, emit))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "cancel must drain the writer thread before returning"
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert emitted == []
    remove_research_artifacts(saved.conversation_id)
    assert not (tmp_path / "research-recovery" / saved.conversation_id).exists()


def test_artifact_deletion_is_conversation_scoped(tmp_path, monkeypatch):
    from disco.retrieval.deep_research.recovery import remove_research_artifacts

    monkeypatch.setenv("DISCO_DATA_DIR", str(tmp_path))
    own, other = saved_checkpoint(), saved_checkpoint("conv_other")
    write_checkpoint(own)
    reference = write_checkpoint(other)
    pools = tmp_path / "pools"
    pools.mkdir()
    (pools / f"{own.conversation_id}.json").write_text("owned")
    (pools / "conv_other.json").write_text("other")
    with pytest.raises(ResearchRecoveryError):
        remove_research_artifacts("../conv_other")
    remove_research_artifacts(own.conversation_id)
    remove_research_artifacts(own.conversation_id)
    assert not (pools / f"{own.conversation_id}.json").exists()
    assert (pools / "conv_other.json").read_text() == "other"
    assert (
        read_checkpoint(other.conversation_id, reference).conversation_id == other.conversation_id
    )
