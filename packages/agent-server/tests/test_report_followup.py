"""RP-13 — Report follow-up: a follow-up continuation reuses the report corpus.

Asserts that a follow-up question on a finished deep-research report results in
an answer that reuses the report's passages as grounding — the report's sources
appear in the follow-up's grounding context.
"""

from __future__ import annotations

import re

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


def _scaled_report(passages: list[dict[str, object]]) -> ReportEvent:
    return ReportEvent(
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


def _quoted(prompt: str) -> dict[str, int]:
    """{quoted passage id: characters of its OWN text in the block}, in order.

    Each entry starts at a line-initial ``[[id]]`` label and runs to the next
    one; the body is what follows the label line, minus the closing separator.
    """
    head = prompt.index("--- SOURCE PASSAGES ---\n")
    block = prompt[head : prompt.index("\n--- END SOURCES ---", head)]
    marks = list(re.finditer(r"^\[\[([^\]\n]+)\]\]", block, re.MULTILINE))
    entries: dict[str, int] = {}
    for index, mark in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(block)
        body = block[mark.start() : end].split("\n", 1)[1]
        entries[mark.group(1)] = len(body.removesuffix("---\n").rstrip())
    return entries


def test_followup_quotes_a_selection_with_real_text_instead_of_every_id():
    """Regression: the block named every source and quoted none of them.

    The 12,000-char budget was apportioned across the WHOLE saved corpus, so on
    a real report (90-152 passages) each entry was a truncated label plus 3-47
    characters of body — measured at 3 characters each on the largest saved
    report, whose answer then cited 47 ids it had never been shown the text of.
    The budget now buys a paragraph of each passage the question reaches for,
    and the header still names the corpus it was drawn from.
    """
    from disco.agent_server.deep_research_service import (
        _FOLLOW_UP_SOURCE_PASSAGES,
        _build_follow_up_prompt,
    )

    passages = [
        {
            "id": f"source-{index}",
            "source_title": f"Release source {index}",
            "text": (f"Evidence from source {index}. " * 300),
        }
        for index in range(40)
    ]

    prompt = _build_follow_up_prompt(
        _scaled_report(passages), passages, "List the releases."
    )

    quoted = _quoted(prompt)
    assert len(quoted) == _FOLLOW_UP_SOURCE_PASSAGES
    assert "Saved source corpus: 40 passages." in prompt
    # Every quoted passage carries a readable excerpt rather than a label and a
    # fragment: 300 characters is several sentences of its own text.
    assert min(quoted.values()) >= 300


def test_followup_selection_prefers_the_passages_the_question_is_about():
    """The selection is by word overlap with the follow-up question."""
    from disco.agent_server.deep_research_service import _build_follow_up_prompt

    passages: list[dict[str, object]] = [
        {"id": f"filler-{index}", "source_title": "Filler", "text": "Unrelated. " * 200}
        for index in range(40)
    ]
    passages.append(
        {
            "id": "quantization",
            "source_title": "Quantization notes",
            "text": "Quantization to Q4 halves the memory footprint. " * 40,
        }
    )

    prompt = _build_follow_up_prompt(
        _scaled_report(passages), passages, "What did it say about quantization?"
    )

    assert next(iter(_quoted(prompt))) == "quantization"


def test_followup_selection_keeps_a_passage_the_question_names_by_id():
    """A reader who asks about [[id]] is given that passage, wherever it ranks.

    The named passage shares no word with the question and sits last in the
    corpus, so nothing but the pin puts it in the block.
    """
    from disco.agent_server.deep_research_service import _build_follow_up_prompt

    passages: list[dict[str, object]] = [
        {"id": f"filler-{index}", "source_title": "Filler", "text": "Latency budget. " * 200}
        for index in range(40)
    ]
    passages.append(
        {"id": "buried", "source_title": "Buried", "text": "Unrelated evidence. " * 200}
    )

    prompt = _build_follow_up_prompt(
        _scaled_report(passages), passages, "What does [[buried]] say about latency?"
    )

    assert next(iter(_quoted(prompt))) == "buried"


def test_followup_selection_falls_back_to_corpus_order_when_nothing_overlaps():
    """A "summarise this" follow-up matches no passage, and must still get some.

    The selection is a stable sort rather than a filter, so every score being
    zero leaves the corpus's own order — which is cited-passages-first. The
    empty-corpus wall exists for a corpus that IS empty, never for a selector
    that found nothing.
    """
    from disco.agent_server.deep_research_service import (
        _FOLLOW_UP_SOURCE_PASSAGES,
        _build_follow_up_prompt,
    )

    passages = [
        {
            "id": f"source-{index}",
            "source_title": f"Release source {index}",
            "text": (f"Evidence numbered {index}. " * 300),
        }
        for index in range(40)
    ]

    prompt = _build_follow_up_prompt(_scaled_report(passages), passages, "Summarise.")

    assert list(_quoted(prompt)) == [
        f"source-{index}" for index in range(_FOLLOW_UP_SOURCE_PASSAGES)
    ]


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
