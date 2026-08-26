"""RP-13 — Report follow-up: a follow-up continuation reuses the report corpus.

Asserts that a follow-up question on a finished deep-research report results in
an answer that reuses the report's passages as grounding — the report's sources
appear in the follow-up's grounding context.
"""

from __future__ import annotations

from disco.core import (
    ConversationStatus,
    EventSource,
    LLMMessage,
    MessageEvent,
    ReportEvent,
    ReportSection,
    StatusEvent,
)
from disco.core.store.sqlite import SqliteEventStore


async def test_followup_triggered_by_new_user_message():
    """A USER message after a finished report should trigger follow-up detection."""
    store = SqliteEventStore(":memory:")
    cid = "rp13_detection"

    # Setup: a finished report
    report = ReportEvent(
        source=EventSource.AGENT,
        query="Original query",
        summary="Summary text.",
        sections=[ReportSection(id="s0", title="Section", markdown="Content.")],
        passages=[{"id": "p0", "text": "Some source text.", "source_title": "Source"}],
        all_hits=[],
    )
    await store.append(cid, report)
    await store.append(cid, StatusEvent(status=ConversationStatus.FINISHED))

    events = await store.get_events(cid)
    reports = [e for e in events if isinstance(e, ReportEvent)]

    # Before follow-up: no fresh user message
    from disco.agent_server.deep_research_service import DeepResearchService

    assert not DeepResearchService._has_fresh_user_message(events, reports)

    # After follow-up: user sends a question
    await store.append(
        cid,
        MessageEvent(
            source=EventSource.USER,
            message=LLMMessage(role="user", content="Tell me more about that."),
        ),
    )

    events_after = await store.get_events(cid)
    reports_after = [e for e in events_after if isinstance(e, ReportEvent)]
    assert DeepResearchService._has_fresh_user_message(events_after, reports_after)


async def test_report_passages_present_for_grounding():
    """The report's passages are available for reuse in a follow-up —
    the corpus is not lost after the first run finishes."""
    store = SqliteEventStore(":memory:")
    cid = "rp13_passages"

    report = ReportEvent(
        source=EventSource.AGENT,
        query="Original query",
        summary="Summary.",
        sections=[ReportSection(id="s0", title="S", markdown="Content.")],
        passages=[
            {"id": "p0", "text": "Passage 0 text.", "source_title": "Source A"},
            {"id": "p1", "text": "Passage 1 text.", "source_title": "Source B"},
            {"id": "p2", "text": "Passage 2 text.", "source_title": "Source C"},
        ],
        all_hits=[],
    )
    await store.append(cid, report)

    events = await store.get_events(cid)
    reports = [e for e in events if isinstance(e, ReportEvent)]
    assert len(reports) == 1
    stored_report = reports[0]
    assert len(stored_report.passages) == 3
    assert stored_report.passages[0]["text"] == "Passage 0 text."
    assert stored_report.passages[1]["id"] == "p1"
    assert stored_report.passages[2]["source_title"] == "Source C"

    # After the follow-up message, the report passages are still intact.
    await store.append(
        cid,
        MessageEvent(
            source=EventSource.USER,
            message=LLMMessage(role="user", content="Expand on that."),
        ),
    )
    await store.append(cid, StatusEvent(status=ConversationStatus.RUNNING, detail="follow_up"))

    events_after = await store.get_events(cid)
    reports_after = [e for e in events_after if isinstance(e, ReportEvent)]
    assert len(reports_after) == 1  # new answer is a message, not a new ReportEvent
    # The original report's passages are still present
    assert len(reports_after[0].passages) == 3


def test_followup_context_keeps_later_sources_after_oversized_passage():
    """One long early passage must not erase later cited sources or report text."""
    from disco.agent_server.deep_research_service import _build_follow_up_prompt

    report = ReportEvent(
        source=EventSource.AGENT,
        query="What shipped this week?",
        summary="Several releases were reported.",
        sections=[
            ReportSection(
                id="s0",
                title="Meta's open-weight releases",
                markdown="Muse Glimmer shipped, while Muse Spark 1.2 was announced.",
            )
        ],
        passages=[
            {"id": "first", "text": "short source", "source_title": "First"},
            {"id": "huge", "text": "x" * 20_000, "source_title": "Oversized"},
            {
                "id": "cnbc",
                "text": "CNBC reported the Muse Glimmer release.",
                "source_title": "CNBC",
            },
        ],
        all_hits=[],
    )

    prompt = _build_follow_up_prompt(report, report.passages, "What did CNBC report?")

    assert "## Meta's open-weight releases" in prompt
    assert "Muse Spark 1.2" in prompt
    assert "[[first]]" in prompt
    assert "[[huge]]" in prompt
    assert "[excerpt middle omitted]" in prompt
    assert "[[cnbc]] (CNBC)" in prompt
    assert "CNBC reported the Muse Glimmer release." in prompt


def test_followup_context_preserves_head_and_tail_of_long_evidence():
    from disco.agent_server.deep_research_service import _build_follow_up_prompt

    passage = {
        "id": "timeline",
        "source_url": "https://example.com/timeline",
        "source_title": "Timeline",
        "text": "EARLY FACT " + ("middle " * 3_000) + "LATE QUALIFICATION",
    }
    report = ReportEvent(
        source=EventSource.AGENT,
        query="What changed?",
        summary="A change occurred.",
        sections=[ReportSection(id="s0", title="Findings", markdown="A change occurred.")],
        passages=[passage],
        all_hits=[],
    )

    prompt = _build_follow_up_prompt(report, [passage], "What was qualified?")

    assert "EARLY FACT" in prompt
    assert "LATE QUALIFICATION" in prompt
    assert "[excerpt middle omitted]" in prompt


def test_followup_context_names_the_full_saved_source_corpus():
    """The model sees the corpus size and every passage identity even at scale."""
    from disco.agent_server.deep_research_service import _build_follow_up_prompt

    passages = [
        {
            "id": f"source-{index}",
            "source_title": f"Release source {index}",
            "text": (f"Evidence from source {index}. " * 300),
        }
        for index in range(40)
    ]
    report = ReportEvent(
        source=EventSource.AGENT,
        query="Which models shipped?",
        summary="Several releases were found.",
        sections=[
            ReportSection(
                id="s0", title="Findings", markdown="Several releases were found."
            )
        ],
        passages=passages,
        all_hits=[],
    )

    prompt = _build_follow_up_prompt(report, passages, "List the releases.")

    assert "Saved source corpus: 40 passages." in prompt
    assert all(f"[[source-{index}]]" in prompt for index in range(40))


async def test_followup_requires_existing_report():
    """_has_fresh_user_message returns False when there's no report at all."""
    store = SqliteEventStore(":memory:")
    cid = "rp13_no_report"

    await store.append(
        cid,
        MessageEvent(
            source=EventSource.USER,
            message=LLMMessage(role="user", content="A question."),
        ),
    )
    await store.append(cid, StatusEvent(status=ConversationStatus.FINISHED))

    events = await store.get_events(cid)
    reports = [e for e in events if isinstance(e, ReportEvent)]

    from disco.agent_server.deep_research_service import DeepResearchService

    assert not DeepResearchService._has_fresh_user_message(events, reports)
