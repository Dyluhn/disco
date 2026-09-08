"""Focused writer context must retain conditions around multiple findings."""

from disco.retrieval.deep_research import _writer_evidence as writer_evidence
from disco.retrieval.models import Passage
from disco.retrieval.source_excerpts import SourceExcerpt


def _passage(text: str) -> Passage:
    return Passage(
        id="primary",
        source_url="https://example.test/standard",
        source_title="Primary standard",
        text=text,
    )


def _text_with_markers(length: int = 6000) -> str:
    text = "x" * length
    markers = {
        100: "LEFT SELECTED EVIDENCE",
        2100: "LEFT NEIGHBOR CONDITION",
        2300: "INTERVENING QUALIFICATION",
        4000: "RIGHT NEIGHBOR CONDITION",
        4200: "RIGHT SELECTED EVIDENCE",
    }
    for start, marker in markers.items():
        text = text[:start] + marker + text[start + len(marker) :]
    return text


def test_disjoint_focus_windows_expand_around_each_finding(monkeypatch):
    passage = _passage(_text_with_markers())
    fixed_windows = {
        "left": SourceExcerpt(text=passage.text[100:300], start=100, end=300),
        "right": SourceExcerpt(text=passage.text[4200:4400], start=4200, end=4400),
    }

    def fixed_excerpt(text, focus, *, max_chars):
        return fixed_windows[focus]

    monkeypatch.setattr(writer_evidence, "relevant_excerpt", fixed_excerpt)
    rendered = writer_evidence._focused_body(passage, ["left", "right"], 5000, ())

    assert "LEFT SELECTED EVIDENCE" in rendered
    assert "RIGHT SELECTED EVIDENCE" in rendered
    assert "LEFT NEIGHBOR CONDITION" in rendered
    assert "INTERVENING QUALIFICATION" in rendered
    assert "RIGHT NEIGHBOR CONDITION" in rendered
    assert len(rendered) <= 5000
    assert passage.text[100:300] in rendered
    assert passage.text[4200:4400] in rendered


def test_near_full_pool_expansion_merges_to_complete_source(monkeypatch):
    text = "y" * 9000
    text = text[:100] + "LEFT FINDING" + text[100 + len("LEFT FINDING") :]
    text = text[:6700] + "RIGHT FINDING" + text[6700 + len("RIGHT FINDING") :]
    passage = _passage(text)
    fixed_windows = {
        "left": SourceExcerpt(text=text[100:300], start=100, end=300),
        "right": SourceExcerpt(text=text[6700:6900], start=6700, end=6900),
    }

    def fixed_excerpt(source_text, focus, *, max_chars):
        return fixed_windows[focus]

    monkeypatch.setattr(writer_evidence, "relevant_excerpt", fixed_excerpt)
    rendered = writer_evidence._focused_body(passage, ["left", "right"], len(text) + 500, ())

    assert text in rendered
    assert "[source characters 0:9000]" in rendered
    assert len(rendered) <= len(text) + 500


def test_disjoint_windows_fit_the_whole_rendered_pool_budget(monkeypatch):
    passage = _passage(_text_with_markers())
    fixed_windows = {
        "left target": SourceExcerpt(text=passage.text[100:300], start=100, end=300),
        "right target": SourceExcerpt(text=passage.text[4200:4400], start=4200, end=4400),
    }

    def fixed_excerpt(text, focus, *, max_chars):
        return fixed_windows[focus]

    monkeypatch.setattr(writer_evidence, "relevant_excerpt", fixed_excerpt)
    rendered = writer_evidence.format_evidence_pool(
        [passage],
        char_budget=5000,
        query="right target",
        coverage={"covered": [{"angle": "left target", "evidence_ids": [passage.id]}]},
    )

    assert "LEFT NEIGHBOR CONDITION" in rendered
    assert "INTERVENING QUALIFICATION" in rendered
    assert "RIGHT NEIGHBOR CONDITION" in rendered
    assert len(rendered) <= 5000


def test_no_query_zero_tail_allowance_does_not_restore_the_whole_source():
    passage = _passage("body content " * 300)
    header = f"[primary] Primary standard\n{passage.source_url}\n"
    omission = "\n… [middle omitted] …\n"
    budget = len(header) + len(omission) + 3

    rendered = writer_evidence.format_evidence_pool([passage], char_budget=budget)

    assert len(rendered) <= budget
    assert omission in rendered
    assert passage.text not in rendered


def test_metadata_larger_than_budget_returns_a_bounded_limitation_notice():
    passage = Passage(
        id="primary",
        source_url="https://example.test/" + "u" * 200,
        source_title="Oversized title " * 30,
        text="body",
    )
    notice = writer_evidence._CONTEXT_LIMITATION_NOTICE

    rendered = writer_evidence.format_evidence_pool([passage], char_budget=len(notice))

    assert rendered == notice
    assert len(rendered) <= len(notice)


def test_url_is_counted_as_title_fallback_in_exact_metadata_budget():
    passage = _passage("complete body").model_copy(update={"source_title": ""})
    header = f"[primary] {passage.source_url}\n{passage.source_url}\n"
    rendered = writer_evidence.format_evidence_pool(
        [passage], char_budget=len(header) + len(passage.text)
    )

    assert rendered == f"{header}{passage.text}"


def test_zero_budget_is_empty_even_when_sources_exist():
    assert writer_evidence.format_evidence_pool([_passage("body")], char_budget=0) == ""
