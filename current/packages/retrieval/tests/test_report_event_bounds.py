"""Persistence-size regressions for the final Deep Research report event."""

from __future__ import annotations

from disco.core import (
    EventSource,
    ReportSection,
    ResearchCheckpointEvent,
    SqliteEventStore,
)
from disco.retrieval.deep_research.engine import ReportFromRun
from disco.retrieval.models import Passage, SearchHit


def _passage(index: int, *, cited: bool) -> Passage:
    label = "cited" if cited else "reviewed"
    return Passage(
        id=f"{label}-{index}",
        source_url=f"https://example.com/{label}/{index}",
        source_title=f"Source {index}",
        text=(f"Evidence from source {index}. " * 1_000),
    )


def _hit(index: int) -> SearchHit:
    return SearchHit(
        url=f"https://discovery.example/{index}",
        title=f"Discovery result {index}",
        snippet=(f"Discovery snippet {index}. " * 200),
        source_engine="fixture",
        rank=index,
        status="ok",
    )


async def test_oversized_report_compacts_evidence_but_persists_full_report() -> None:
    cited = [_passage(index, cited=True) for index in range(90)]
    reviewed = [_passage(index, cited=False) for index in range(240)]
    hits = [_hit(index) for index in range(900)]
    summary = "Executive synthesis with decision-relevant findings. " * 100
    markdown = ("Full reader-facing report prose remains intact. " * 1_500).strip()
    claims = [
        {
            "section_id": "s0",
            "claim": {
                "text": f"Verified claim {index}",
                "cited_passage_ids": [f"cited-{index % len(cited)}"],
            },
            "verdict": "supported",
            "best_passage_id": f"cited-{index % len(cited)}",
            "entailment_score": 0.99,
        }
        for index in range(180)
    ]
    result = ReportFromRun(
        query="What does the evidence show?",
        summary=summary,
        sections=[
            ReportSection(
                id="s0",
                title="Evidence-led finding",
                markdown=markdown,
                cited_passage_ids=[passage.id for passage in cited],
            )
        ],
        cited_passages=cited,
        reviewed_passages=reviewed,
        all_hits=hits,
        unsupported_count=0,
        bounded_by=None,
        depth_tier="exhaustive",
        claims=claims,
        completed_probes=["wide survey", "counterevidence"],
        pending_probes=["remaining edge case"],
    )

    event = result.to_event()

    assert event.summary == summary
    assert event.sections[0].markdown == markdown
    assert event.sections[0].cited_passage_ids == [passage.id for passage in cited]
    assert {passage["id"] for passage in event.passages} == {
        passage.id for passage in cited
    }
    assert event.claims == claims
    assert event.completed_probes == ["wide survey", "counterevidence"]
    assert event.pending_probes == ["remaining edge case"]
    assert event.reviewed_passages
    assert event.all_hits
    assert event.meta["report_evidence_compacted"] is True
    assert event.meta["reviewed_passages_total"] == len(reviewed)
    assert event.meta["all_hits_total"] == len(hits)

    # The authoritative store check includes the real event envelope and its
    # sequence-number rewrite, not merely a hand-estimated dict size.
    store = SqliteEventStore(":memory:")
    stored = await store.append("long-report", event)
    replayed = await store.get_events("long-report")
    assert stored.source is EventSource.AGENT
    assert replayed == [stored]
    assert replayed[0].sections[0].markdown == markdown
    store.close()


def test_small_report_event_remains_lossless() -> None:
    cited = [_passage(0, cited=True)]
    reviewed = [_passage(0, cited=False)]
    hits = [_hit(0)]
    result = ReportFromRun(
        query="Small report",
        summary="Summary [[cited-0]]",
        sections=[
            ReportSection(
                id="s0",
                title="Finding",
                markdown="Body [[cited-0]]",
                cited_passage_ids=["cited-0"],
            )
        ],
        cited_passages=cited,
        reviewed_passages=reviewed,
        all_hits=hits,
        unsupported_count=0,
        bounded_by=None,
        depth_tier="quick",
    )

    event = result.to_event()

    assert event.passages == [cited[0].model_dump()]
    assert event.reviewed_passages == [reviewed[0].model_dump()]
    assert event.all_hits == [hits[0].model_dump()]
    assert event.meta == {}


async def test_oversized_stop_persists_as_bounded_checkpoint_not_report() -> None:
    passages = [_passage(index, cited=False) for index in range(240)]
    hits = [_hit(index) for index in range(900)]
    trail = [
        {"kind": "search", "query": f"research angle {index}", "admitted": 1}
        for index in range(80)
    ]
    result = ReportFromRun(
        query="Paused investigation",
        summary="",
        sections=[],
        cited_passages=[],
        reviewed_passages=passages,
        all_hits=hits,
        unsupported_count=0,
        bounded_by="stopped",
        depth_tier="exhaustive",
        completed_probes=[entry["query"] for entry in trail],
        research_trail=trail,
        recency_window="month",
    )

    event = result.to_event()

    assert isinstance(event, ResearchCheckpointEvent)
    assert event.query == "Paused investigation"
    assert event.trail == trail
    assert event.completed_queries == [entry["query"] for entry in trail]
    assert event.recency_window == "month"
    assert event.passages
    assert event.all_hits
    assert event.meta["checkpoint_evidence_compacted"] is True

    store = SqliteEventStore(":memory:")
    stored = await store.append("paused-report", event)
    replayed = await store.get_events("paused-report")
    assert isinstance(stored, ResearchCheckpointEvent)
    assert replayed == [stored]
    store.close()
