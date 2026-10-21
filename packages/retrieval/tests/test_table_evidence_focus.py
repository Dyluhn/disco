"""Table evidence selection retains row meaning without searching citation furniture."""

import json
from itertools import islice
from pathlib import Path

import pytest
from disco.retrieval.grounding import _clean_premise, _table_claim_spans, _verify_claims
from disco.retrieval.models import Passage
from disco.retrieval.source_excerpts import relevant_excerpt


class RecordingNLI:
    def __init__(self, label: str = "neutral", available: bool = True) -> None:
        self.label = label
        self.available = available
        self.calls: list[tuple[str, str]] = []

    def entail(self, premise: str, hypothesis: str) -> str:
        self.calls.append((premise, hypothesis))
        return self.label

    def score(self, premise: str, hypothesis: str) -> float:
        assert (premise, hypothesis) == self.calls[-1]
        return 0.2

    def verification_available(self, premise: str, hypothesis: str) -> bool:
        assert (premise, hypothesis) == self.calls[-1]
        return self.available


def passage(text: str, passage_id: str = "p1") -> Passage:
    return Passage(
        id=passage_id, source_url="https://example.test", source_title="Source", text=text
    )


def captured_insee_text() -> str:
    # Read only the existing extraction interaction; never replay or call a model.
    path = Path(__file__).resolve().parents[3] / (
        "development/harness/cassettes/loop_paris_standard_low_v3.cassette.jsonl"
    )
    with path.open() as stream:
        row = json.loads(next(islice(stream, 19, 20)))
    assert row["seam"] == "extract"
    assert row["input"]["url"].endswith("geo=COM-75056")
    return row["output"]["content"]


@pytest.mark.parametrize(
    ("label", "available", "status"),
    [
        ("entail", True, "supported"),
        ("neutral", True, "unresolved"),
        ("contradict", True, "contradicted"),
        ("neutral", False, "unavailable"),
    ],
)
def test_captured_population_row_reaches_nli_without_overruling_its_label(
    label: str, available: bool, status: str
) -> None:
    text = captured_insee_text()
    table = (
        "| Geography | Population (2023) | Source |\n"
        "| --- | --- | --- |\n"
        "| City proper (commune) | 2,103,778 | [[p1]] |\n"
    )
    original_claim = "Geography: City proper (commune); Population (2023): 2,103,778; Source: "
    population_row = "PopulationCommune : Paris (75056)\nPopulation en 20232 103 778"
    cleaned = _clean_premise(text)
    old_selection = relevant_excerpt(cleaned, original_claim, max_chars=1600)
    assert (old_selection.start, old_selection.end) == (800, 2400)
    assert population_row not in old_selection.text

    nli = RecordingNLI(label, available)
    rows = _verify_claims(table, {"p1": passage(text)}, nli)
    assert len(rows) == len(nli.calls) == 1
    premise, hypothesis = nli.calls[0]
    assert premise == cleaned[:1600]
    assert population_row in premise
    assert hypothesis == original_claim.strip()
    assert rows[0]["claim"] == {"text": original_claim, "cited_passage_ids": ["p1"]}
    assert rows[0]["verification_status"] == status
    span = _table_claim_spans(table)[0]
    assert table[span.start : span.end] == table.splitlines()[2]


def test_each_table_row_keeps_its_own_entity_values_and_citations() -> None:
    text = "Town: Alpha; Population: 12 residents.\n" + "Background. " * 170
    text += "Town: Beta; Population: 18 residents.\n" + "Background. " * 170
    table = (
        "| Town | Population | Reference |\n| --- | --- | --- |\n"
        "| Alpha | 12 residents | [[a]] |\n"
        "| Beta | 18 residents | [[b]] |\n"
    )
    nli = RecordingNLI()
    rows = _verify_claims(table, {key: passage(text, key) for key in ("a", "b")}, nli)
    assert len(rows) == len(nli.calls) == 2
    for index, (entity, count, other, source) in enumerate(
        [("Alpha", 12, "Beta", "a"), ("Beta", 18, "Alpha", "b")]
    ):
        premise, hypothesis = nli.calls[index]
        assert f"Town: {entity}; Population: {count} residents." in premise
        assert other not in premise
        assert len(premise) <= 1600
        assert hypothesis == f"Town: {entity}; Population: {count} residents; Reference:"
        assert rows[index]["claim"]["cited_passage_ids"] == [source]
        assert rows[index]["verification_status"] == "unresolved"


def test_nonempty_source_and_zero_values_remain_searchable() -> None:
    table = (
        "| Source | Count | Evidence |\n| --- | --- | --- |\n| Reservoir | 0 | [[p1]] [[p2]] |\n"
    )
    span = _table_claim_spans(table)[0]
    assert span.evidence_focus == "Source: Reservoir; Count: 0"
    assert span.text == "Source: Reservoir; Count: 0; Evidence: "
    assert span.cited_passage_ids == ("p1", "p2")


def test_empty_cells_do_not_borrow_another_row_or_neighboring_prose() -> None:
    table = (
        "Unrelated city Gamma.\n\n"
        "| Town | Population | Evidence |\n| --- | --- | --- |\n"
        "| Alpha | 12 residents | [[p1]] |\n"
        "| | 18 residents | [[p2]] |\n"
    )
    first, second = _table_claim_spans(table)
    assert first.evidence_focus == "Town: Alpha; Population: 12 residents"
    assert second.evidence_focus == "Population: 18 residents"
    assert second.text == "Town: ; Population: 18 residents; Evidence: "
    assert second.cited_passage_ids == ("p2",)


def test_prose_still_selects_using_its_unchanged_hypothesis() -> None:
    text = captured_insee_text()
    claim = (
        "The 2023 census (RP2023) puts the Paris commune (INSEE code 75056) at "
        "2,103,778 inhabitants, with a density of about 19,960 per km²"
    )
    nli = RecordingNLI()
    rows = _verify_claims(f"{claim} [[p1]].", {"p1": passage(text)}, nli)
    assert nli.calls == [
        (relevant_excerpt(_clean_premise(text), claim, max_chars=1600).text, claim)
    ]
    assert rows[0]["claim"] == {"text": claim, "cited_passage_ids": ["p1"]}
