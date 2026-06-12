"""Cluster 7 — Knowledge + Datasource events: pinned, condensation-immune,
LLMConvertible context."""

from __future__ import annotations

from disco.core import (
    CondensationEvent,
    DatasourceEvent,
    KnowledgeEvent,
    LLMConvertible,
    ObservationEvent,
    ToolResult,
)
from disco.core.view import View


def _seq(events):
    return [e.model_copy(update={"seq": i}) for i, e in enumerate(events, start=1)]


def test_knowledge_event_renders_to_context():
    k = KnowledgeEvent(scope="**/*.tsx", snippet="Always use function components.")
    assert isinstance(k, LLMConvertible)
    msg = k.to_llm_message()
    assert "Always use function components" in msg.content
    assert "**/*.tsx" in msg.content
    assert "<knowledge" in msg.content


def test_datasource_event_renders_to_context():
    d = DatasourceEvent(name="Yahoo v8", docs="GET /v8/finance/chart/{symbol}")
    assert isinstance(d, LLMConvertible)
    msg = d.to_llm_message()
    assert "Yahoo v8" in msg.content
    assert "/v8/finance/chart" in msg.content
    assert "<datasource" in msg.content


def test_knowledge_and_datasource_survive_condensation():
    # A tombstone forgetting the whole middle must NOT drop the pinned
    # knowledge/datasource events.
    events = _seq(
        [
            KnowledgeEvent(snippet="house style: 8px grid"),
            ObservationEvent(
                tool_result=ToolResult(call_id="c", tool_name="shell", success=True, content="x"),
                action_id="a",
            ),
            DatasourceEvent(name="Stripe", docs="POST /v1/charges with amount, currency"),
            ObservationEvent(
                tool_result=ToolResult(call_id="c2", tool_name="shell", success=True, content="y"),
                action_id="a2",
            ),
        ]
    )
    tomb = CondensationEvent(
        forgotten_start_seq=1, forgotten_end_seq=4, summary="[summary]", summary_role="user"
    ).model_copy(update={"seq": 5})
    view = View.of([*events, tomb])
    joined = " ".join(m.content for m in view.messages)
    assert "8px grid" in joined  # KnowledgeEvent pinned
    assert "POST /v1/charges" in joined  # DatasourceEvent pinned
    # The ordinary observations WERE forgotten (replaced by the summary).
    assert "[summary]" in joined


def test_events_round_trip_through_the_union():
    from disco.core import EventAdapter

    for e in (
        KnowledgeEvent(scope="x", snippet="s"),
        DatasourceEvent(name="n", docs="d"),
    ):
        dumped = e.model_dump(mode="json")
        restored = EventAdapter.validate_python(dumped)
        assert restored == e
