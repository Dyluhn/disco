"""Reviewer evidence retains qualifications and obeys the copied-range contract."""

import json

from _writer_doubles import pool
from disco.retrieval.deep_research import _review_context
from disco.retrieval.deep_research._claim_review import (
    MAX_EVIDENCE_RANGE_CHARS,
    _source_range,
)


def test_complete_documents_preserve_remote_qualifications_in_valid_ranges():
    passages = pool(2)
    passages[0] = passages[0].model_copy(
        update={
            "text": "General introduction with no relevant terminology. " * 1800
            + "The exception applies only when every participant is on the same host."
        }
    )
    passages[1] = passages[1].model_copy(
        update={
            "text": "A different document. " * 2300
            + "Normal synchronization can lose committed changes after power loss."
        }
    )
    by_id = {passage.id: passage for passage in passages}
    context = _review_context.review_evidence_context(
        "Compare the options.",
        {},
        "Compare [[p1]] with [[p2]].",
        [],
        by_id,
    )
    records = [json.loads(line) for line in context.splitlines()[1:]]
    assert len(context) <= _review_context.REVIEW_EVIDENCE_CHAR_BUDGET
    for passage in passages:
        chunks = [row for row in records if row.get("citation_id") == f"[[{passage.id}]]"]
        assert "".join(row["excerpt"] for row in chunks) == passage.text
        assert chunks[0]["start"] == 0
        assert chunks[-1]["end"] == len(passage.text)
        for chunk in chunks:
            start, end = _source_range(chunk, passage)
            assert end - start <= MAX_EVIDENCE_RANGE_CHARS
            assert passage.text[start:end] == chunk["excerpt"]


def test_serialized_escaping_cannot_exceed_the_evidence_budget(monkeypatch):
    monkeypatch.setattr(_review_context, "REVIEW_EVIDENCE_CHAR_BUDGET", 2000)
    passage = pool(1)[0].model_copy(update={"text": '"\\\n' * 450})
    context = _review_context.review_evidence_context(
        "Compare the options.",
        {},
        "An option [[p1]].",
        [],
        {passage.id: passage},
    )
    assert len(context) <= 2000
    assert "omitted" in context


def test_oversized_sources_still_supply_claim_focused_exact_windows():
    passage = pool(1)[0].model_copy(
        update={
            "text": "Background unrelated to the decision. " * 7000
            + "The measured capacity applies only to the pilot plant, not the planned expansion."
        }
    )
    context = _review_context.review_evidence_context(
        "Compare capacity.",
        {},
        "The measured capacity includes the planned expansion [[p1]].",
        [],
        {passage.id: passage},
    )
    assert len(context) <= _review_context.REVIEW_EVIDENCE_CHAR_BUDGET
    assert "not the planned expansion" in context
    for row in map(json.loads, context.splitlines()[1:]):
        if "excerpt" in row:
            start, end = _source_range(row, passage)
            assert passage.text[start:end] == row["excerpt"]
