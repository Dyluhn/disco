"""Table captions supply evidence candidates, never a verification verdict."""

import pytest
from disco.retrieval.grounding import _claim_spans, _verify_claims
from disco.retrieval.models import Passage

TABLE = "| Mode | Durable |\n| --- | --- |\n| FULL | yes |\n| NORMAL | no |\n"
CAPTION = "The documented durability matrix [[source]]:"


class Verifier:
    def __init__(self, label="entail", available=True):
        self.label = label
        self.available = available
        self.hypotheses = []

    def entail(self, premise, hypothesis):
        self.hypotheses.append(hypothesis)
        return self.label

    def score(self, premise, hypothesis):
        return 0.9

    def verification_available(self, premise, hypothesis):
        return self.available


def check(text, *, borrow=True, label="entail", available=True):
    verifier = Verifier(label, available)
    sources = {
        name: Passage(
            id=name, source_url="https://example.test", source_title="Source", text="Evidence."
        )
        for name in ("source", "other")
    }
    claims = _verify_claims(text, sources, verifier, borrow_paragraph_citations=borrow)
    return [row for row in claims if row["claim"]["text"].startswith("Mode:")], verifier


@pytest.mark.parametrize("gap", ["\n\n", "\n\n\n", "\n \t\n"])
def test_caption_across_blank_lines_checks_each_complete_row_without_rewriting_offsets(gap):
    text = CAPTION + gap + TABLE
    rows, verifier = check(text)
    assert len(rows) == 2
    assert all(row["borrowed_passage_ids"] == ["source"] for row in rows)
    assert all(row["claim"]["cited_passage_ids"] == [] for row in rows)
    assert "Mode: FULL; Durable: yes" in verifier.hypotheses
    assert "Mode: NORMAL; Durable: no" in verifier.hypotheses
    spans = [span for span in _claim_spans(text) if span.text.startswith("Mode:")]
    assert [text[span.start : span.end] for span in spans] == ["| FULL | yes |", "| NORMAL | no |"]


@pytest.mark.parametrize(
    "label,available,status",
    [
        ("entail", True, "supported"),
        ("neutral", True, "unresolved"),
        ("contradict", True, "contradicted"),
        ("entail", False, "unavailable"),
    ],
)
def test_caption_does_not_override_independent_row_verification(label, available, status):
    rows, _ = check(CAPTION + "\n\n" + TABLE, label=label, available=available)
    assert {row["verification_status"] for row in rows} == {status}


@pytest.mark.parametrize(
    "between",
    [
        "Unrelated paragraph.\n\n",
        "## New section\n\n",
        "- A list item\n\n",
        "```\nexample\n```\n\n",
        "~~~\nexample\n~~~\n\n",
    ],
)
def test_caption_does_not_cross_intervening_content(between):
    rows, _ = check(CAPTION + "\n\n" + between + TABLE)
    assert all(row.get("borrowed_passage_ids") == [] for row in rows)


def test_explicit_row_citation_overrides_caption_and_unknown_caption_stays_unresolved():
    text = CAPTION + "\n\n" + TABLE.replace("yes |", "yes [[other]] |")
    rows, _ = check(text)
    assert rows[0]["claim"]["cited_passage_ids"] == ["other"]
    assert not rows[0].get("borrowed_passage_ids")
    rows, verifier = check(CAPTION.replace("source", "unknown") + "\n\n" + TABLE)
    assert all(row["verification_status"] == "unresolved" for row in rows)
    assert not any(h.startswith("Mode:") for h in verifier.hypotheses)


def test_caption_does_not_change_short_answer_policy_or_flow_to_another_table():
    text = CAPTION + "\n\n" + TABLE
    rows, _ = check(text, borrow=False)
    assert all(row["verification_reason"] == "missing_evidence" for row in rows)
    rows, _ = check(text + "\n" + TABLE)
    assert rows[0]["borrowed_passage_ids"] == ["source"]
    assert all(row.get("borrowed_passage_ids") == [] for row in rows[2:])


@pytest.mark.parametrize(
    "caption",
    [
        "No source supplied:",
        "An unrelated fact [[source]].",
        "## Heading [[source]]:",
        "- List item [[source]]:",
        "> Quote [[source]]:",
        "First prose line\nLast prose line [[source]]:",
    ],
)
def test_only_a_standalone_colon_introduction_supplies_table_context(caption):
    rows, _ = check(caption + "\n\n" + TABLE)
    assert all(row.get("borrowed_passage_ids") == [] for row in rows)
