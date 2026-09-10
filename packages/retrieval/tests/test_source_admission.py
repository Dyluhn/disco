"""Admission must retain the fetched source that later inspection can read."""

import pytest
from disco.retrieval.deep_research._agent_state import _AgentState, _report_usable_passage
from disco.retrieval.deep_research._budget import SourceBudget
from disco.retrieval.deep_research._source_inspection import Inspection, inspect_sources
from disco.retrieval.models import ExtractedDoc, Passage, RetrievalResult, SearchHit


def _result(*, corpus_id=None, fetched_ok=True, extra_document=False):
    selected = Passage(
        id="selected_p1",
        source_url="https://example.test/policy",
        source_title="Policy",
        text="This paragraph describes the service in general terms.",
        corpus_id=corpus_id,
        char_start=100,
        char_end=152,
    )
    content = (
        "# Service privacy policy\n\n"
        + selected.text
        + "\n\n"
        + "General background about the service. " * 120
        + "\n\nRetention is limited to 25 hours; this excludes aggregate statistics."
    )
    document = ExtractedDoc(
        url=selected.source_url,
        title=selected.source_title,
        content=content,
        passages=[selected],
        fetched_ok=fetched_ok,
        status="ok" if fetched_ok else "error",
    )
    docs = [document]
    if extra_document:
        docs.append(document.model_copy(update={"url": "https://unselected.test/other"}))
    return RetrievalResult(
        passages=[selected],
        all_hits=[SearchHit(url=selected.source_url, title="Policy")],
        extracted=docs,
        issued_queries=["service policy"],
    )


def test_admitted_source_retains_the_fetched_document_for_later_inspection():
    result = _result(extra_document=True)
    state = _AgentState(SourceBudget(1))

    assert state.admit_retrieved(result) == 1
    source = state.pool[0]
    inspect_sources(state, (Inspection(source.id, focus="retention aggregate statistics"),), 1)

    assert source.text == result.extracted[0].content
    assert source.char_start == 0 and source.char_end == len(source.text)
    assert source.id != result.passages[0].id  # the old narrower span keeps its own identity
    assert "Retention is limited to 25 hours" in state.trail[-1]["text"]
    assert len(state.pool) == 1 and state.budget.used == 1
    assert state.admit_retrieved(result) == 0
    assert state.budget.used == 1


def test_full_source_identity_does_not_depend_on_which_chunk_was_ranked_first():
    first = _result()
    second = first.model_copy(
        update={"passages": [first.passages[0].model_copy(update={"id": "selected_p2"})]}
    )
    states = [_AgentState(SourceBudget(1)), _AgentState(SourceBudget(1))]
    for state, result in zip(states, (first, second), strict=True):
        state.admit_retrieved(result)

    assert states[0].pool[0].id == states[1].pool[0].id
    assert states[0].pool[0].text == states[1].pool[0].text


@pytest.mark.parametrize("options", [{"corpus_id": "private-space"}, {"fetched_ok": False}])
def test_corpus_spans_and_unavailable_full_documents_keep_the_original_passage(options):
    result = _result(**options)
    state = _AgentState(SourceBudget(1))
    assert state.admit_retrieved(result) == 1
    assert state.pool == result.passages


def test_a_passage_only_provider_keeps_its_existing_provenance():
    result = _result().model_copy(update={"extracted": []})
    state = _AgentState(SourceBudget(1))
    assert state.admit_retrieved(result) == 1
    assert state.pool == result.passages


@pytest.mark.parametrize(
    "text",
    [
        "This privacy policy limits retention of resolver logs to twenty-five hours.",
        "| Product | Capacity |\n| --- | --- |\n| Stationary battery | 100 megawatt-hours |",
        "Published: 2026-09-05\n\nThe measured efficiency declined at low ambient temperatures.",
        "该储能电站已经正式投入运行，首期建设规模为十兆瓦时，后续工程尚未完成。",
    ],
)
def test_topics_tables_dates_and_non_latin_text_are_not_extraction_furniture(text):
    assert _report_usable_passage(_result().passages[0].model_copy(update={"text": text}))


@pytest.mark.parametrize(
    "text",
    ["", "...", "Last updated: 2026-09-05", "Home | Privacy Policy | Terms of Service | Contact"],
)
def test_empty_text_and_pure_navigation_still_do_not_consume_source_capacity(text):
    assert not _report_usable_passage(_result().passages[0].model_copy(update={"text": text}))
