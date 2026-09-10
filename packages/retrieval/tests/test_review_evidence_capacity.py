"""Review excerpts retain nearby qualifications inside the existing context allowance."""

import json

from disco.retrieval.deep_research import _review_context as context
from disco.retrieval.models import Passage


def source(identifier, qualification, size=20000):
    opening = "## Service life\nThe device has a stated service life target of twenty years.\n"
    filler = "Assembly notes describe the equipment and preparation. "
    prefix = (opening + filler * 400)[:15000]
    text = (prefix + qualification + "\n" + filler * 400)[:size]
    return Passage(
        id=identifier,
        source_title="Primary engineering study",
        source_url="https://example.test/" + identifier,
        text=text,
    )


def records(view):
    return [json.loads(line) for line in view.splitlines() if line.startswith("{")]


def test_near_full_pool_keeps_both_sources_material_qualifications(monkeypatch):
    first = source(
        "one", "Qualification A: only a laboratory design; field outcomes are unmeasured."
    )
    second = source(
        "two", "Qualification B: the estimate assumes controlled temperature throughout."
    )
    monkeypatch.setattr(context, "REVIEW_EVIDENCE_CHAR_BUDGET", 40000)

    view = context.review_evidence_context(
        "Assess service life",
        {},
        "The devices have a twenty-year service life target [[one]] [[two]].",
        [],
        {"one": first, "two": second},
    )

    rows = records(view)
    assert "Qualification A: only a laboratory design" in view
    assert "Qualification B: the estimate assumes controlled temperature" in view
    assert len(view) <= 40000
    for row in rows:
        if "excerpt" in row:
            original = {"[[one]]": first.text, "[[two]]": second.text}[row["citation_id"]]
            assert row["excerpt"] == original[row["start"] : row["end"]]
            assert row["end"] - row["start"] <= context.MAX_EVIDENCE_RANGE_CHARS


def test_expansion_accounts_for_json_escaping_without_losing_selected_text(monkeypatch):
    text = "## Service life\nThe service life target is twenty years.\n" + (
        '"quoted"\\detail\n' * 900
    )
    passage = Passage(
        id="one", source_title="Primary study", source_url="https://example.test/spec", text=text
    )
    monkeypatch.setattr(context, "REVIEW_EVIDENCE_CHAR_BUDGET", 6000)

    view = context.review_evidence_context(
        "Assess service life",
        {},
        "The service life target is twenty years [[one]].",
        [],
        {"one": passage},
    )

    assert len(view) <= 6000
    rows = [row for row in records(view) if "excerpt" in row]
    assert any("The service life target is twenty years." in row["excerpt"] for row in rows)
    for row in rows:
        assert row["excerpt"] == text[row["start"] : row["end"]]


def test_expansion_preserves_disjoint_selected_ranges_and_claim_span_bound(monkeypatch):
    from disco.retrieval.source_excerpts import SourceExcerpt

    text = "a" * 50000
    passage = Passage(
        id="one",
        source_title="Long primary study",
        source_url="https://example.test/long",
        text=text,
    )
    windows = [
        SourceExcerpt(text[100:1300], 100, 1300),
        SourceExcerpt(text[48000:49200], 48000, 49200),
    ]
    monkeypatch.setattr(context, "REVIEW_EVIDENCE_CHAR_BUDGET", 40000)
    monkeypatch.setattr(context, "_source_windows", lambda *_args: windows)
    view = context.review_evidence_context(
        "Assess operating conditions", {}, "Conditions differ [[one]].", [], {"one": passage}
    )
    rows = [row for row in records(view) if "excerpt" in row]
    assert len(view) <= 40000
    assert sum(row["end"] - row["start"] for row in rows) > 35000
    for window in windows:
        assert any(row["start"] <= window.start and window.end <= row["end"] for row in rows)
    for row in rows:
        assert row["excerpt"] == text[row["start"] : row["end"]]
        assert row["end"] - row["start"] <= context.MAX_EVIDENCE_RANGE_CHARS
