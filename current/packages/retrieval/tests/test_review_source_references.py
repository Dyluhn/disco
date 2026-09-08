"""A source reference must not lose the condition it points at during review."""

import json

from disco.retrieval.deep_research import _review_context as context
from disco.retrieval.models import Passage
from disco.retrieval.source_excerpts import SourceExcerpt


def test_linked_source_condition_is_retained_with_exact_offsets():
    opening = "The storage rule has an [exception](https://spec.test/doc#exception).\n"
    text = opening + "Unrelated text.\n" * 200
    start = len(text)
    text += (
        "## [Exception](https://spec.test/doc#exception)\n"
        "Only implementations meeting the status requirements may ignore the prohibition.\n"
    )
    selected = SourceExcerpt(opening, 0, len(opening))
    windows = context._linked_windows(text, [selected])
    assert windows[0] == selected
    assert windows[1].start == start
    assert "Only implementations meeting" in windows[1].text
    assert all(w.text == text[w.start : w.end] for w in windows)


def test_external_links_and_unselected_references_do_not_expand():
    opening = "Read [another source](https://other.test/doc#exception).\n"
    text = opening + "\n" + "## [Exception](https://spec.test/doc#exception)\nA condition.\n"
    window = SourceExcerpt(opening, 0, len(opening))
    assert context._linked_windows(text, [window]) == [window]


def test_linked_windows_do_not_recursively_follow_reference_chains(monkeypatch):
    monkeypatch.setattr(context, "_REVIEW_EXCERPT_CHARS", 120)
    first = "See [first](#first).\n"
    text = first + "filler\n" * 100
    text += "## [First](#first)\nSee [second](#second).\n" + "filler\n" * 100
    second_start = len(text)
    text += "## [Second](#second)\nThis must not be expanded recursively.\n"
    windows = context._linked_windows(text, [SourceExcerpt(first, 0, len(first))])
    assert len(windows) == 2
    assert all(w.end <= second_start for w in windows)


def test_linked_context_stays_inside_existing_pack_budget(monkeypatch):
    monkeypatch.setattr(context, "REVIEW_EVIDENCE_CHAR_BUDGET", 3500)
    text = "storage policy " * 150 + "[Exception](#exception)\n" + "filler\n" * 1000
    text += "## [Exception](#exception)\nOnly a compliant implementation may override this rule.\n"
    source = Passage(
        id="p", text=text, source_title="Specification", source_url="https://spec.test/doc"
    )
    rendered = context.review_evidence_context(
        "storage policy",
        {},
        "",
        [("Rule", "The storage policy has an exception [[p]].")],
        {"p": source},
    )
    assert len(rendered) <= context.REVIEW_EVIDENCE_CHAR_BUDGET
    rows = [json.loads(line) for line in rendered.splitlines() if line.startswith("{")]
    assert all(
        row["excerpt"] == text[row["start"] : row["end"]] for row in rows if "excerpt" in row
    )
