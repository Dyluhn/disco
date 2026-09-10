"""The request's UTC date survives execution, checkpoints and model contexts."""

from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from disco.agent_server import ConversationRuntime
from disco.agent_server._deep_research_service_parts import execute
from disco.core import (
    EventSource,
    LLMMessage,
    MessageEvent,
    ResearchCheckpointEvent,
    SqliteEventStore,
)
from disco.retrieval.deep_research._agent_state import _AgentState
from disco.retrieval.deep_research._budget import SourceBudget
from disco.retrieval.deep_research._recovery_state import AgentCheckpoint
from disco.retrieval.deep_research._review_context import quality_audit_context
from disco.retrieval.deep_research._task_context import research_reference_context
from disco.retrieval.deep_research._turn_context import _turn_user_message
from disco.retrieval.deep_research.agent import ResearchOutcome
from disco.retrieval.deep_research.depth import DepthTier, bounds_for
from disco.retrieval.deep_research.writer import _report_instruction


def _message(text, timestamp, source=EventSource.USER):
    return MessageEvent(
        source=source,
        timestamp=datetime.fromisoformat(timestamp),
        message=LLMMessage(
            role="user" if source == EventSource.USER else "assistant", content=text
        ),
    )


def test_reference_uses_first_nonempty_user_request_in_utc():
    events = [
        _message("Welcome", "2026-09-04T08:00:00+00:00", EventSource.AGENT),
        _message("  ", "2026-09-04T09:00:00+00:00"),
        _message("Compare current deployments", "2026-09-05T23:30:00-05:00"),
        _message("Focus on costs", "2026-09-07T12:00:00+00:00"),
    ]
    assert execute._request_reference_trail(events) == [
        {"kind": "research_reference", "date": "2026-09-06"}
    ]
    assert execute._request_reference_trail(events[:2]) == []


@pytest.mark.parametrize("resume", [False, True])
async def test_execution_seeds_date_once_and_preserves_checkpoint_date(monkeypatch, resume):
    store = SqliteEventStore(":memory:")
    runtime = ConversationRuntime(store)
    runtime.settings._set_surface("c1", "deep_research")
    await store.append("c1", _message("Current evidence", "2026-09-06T12:00:00+00:00"))
    saved_trail = [{"kind": "research_reference", "date": "2026-08-30"}]
    checkpoint = ResearchCheckpointEvent(query="Current evidence", trail=saved_trail)
    monkeypatch.setattr(
        execute, "build_retrieval_deps", AsyncMock(return_value=({}, frozenset(), ()))
    )
    monkeypatch.setattr(execute, "run_preflight", AsyncMock(return_value=None))
    monkeypatch.setattr(execute, "build_router_and_engine", lambda *a, **k: (object(), object()))
    monkeypatch.setattr(execute, "build_run", lambda *a, **k: object())
    engine = AsyncMock()
    monkeypatch.setattr(execute, "run_engine", engine)
    monkeypatch.setattr(execute, "publish_result", AsyncMock())
    try:
        await execute._run_to_report(
            runtime.deep_research, "c1", resume_from=checkpoint if resume else None
        )
        expected = saved_trail if resume else [{"kind": "research_reference", "date": "2026-09-06"}]
        assert engine.await_args.kwargs["resume_research_trail"] == expected
    finally:
        await runtime.aclose()


def test_checkpointed_reference_reaches_research_writer_and_review_without_recency_filter():
    bound = bounds_for(DepthTier.QUICK)
    state = _AgentState(budget=SourceBudget(bound.max_sources))
    state.trail = [{"kind": "research_reference", "date": "2026-09-06"}]
    checkpoint = AgentCheckpoint.model_validate_json(
        AgentCheckpoint.capture(state).model_dump_json()
    )
    restored = checkpoint.restore(bound)
    outcome = ResearchOutcome("Compare deployment evidence", [], [], restored.trail, None)
    query = "Compare conditions in 2024 with current deployment"
    contexts = [
        _turn_user_message(query, restored, [], turns_left=8, total_turns=8, bound=bound),
        _report_instruction(query, outcome, None),
        quality_audit_context([], [], {}, query=query, coverage={}, trail=restored.trail),
    ]
    for context in contexts:
        assert "REFERENCE DATE (UTC, original user request): 2026-09-06" in context
        assert "explicitly requested historical or future date" in context
        assert query in context
    assert research_reference_context([]) == ""
    assert research_reference_context([{"kind": "research_reference", "date": "invalid"}]) == ""
