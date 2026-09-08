"""Grounding labels retain their meaning through checking and persistence."""

from disco.retrieval.grounding import (
    _best_entail,
    _claim_spans,
    _retain_supported_claims,
    _verify_claims,
)
from disco.retrieval.models import Passage


class Classified:
    def __init__(self, label: str, score: float, available: bool = True):
        self.label, self.value, self.available = label, score, available
        self.premises: list[str] = []

    def entail(self, premise: str, hypothesis: str) -> str:
        self.premises.append(premise)
        return self.label

    def score(self, premise: str, hypothesis: str) -> float:
        return self.value

    def verification_available(self, premise: str, hypothesis: str) -> bool:
        return self.available


def passage(text: str) -> Passage:
    return Passage(id="p1", source_url="https://example.test", source_title="Source", text=text)


def test_a_high_score_cannot_override_an_explicit_contradiction_label() -> None:
    label, _score = _best_entail(
        "Only one writer may write at a time.",
        "Two writers may write at once.",
        Classified("contradict", 0.95),
    )
    assert label == "contradict"


def test_a_qualification_after_the_first_twelve_sentences_reaches_the_checker() -> None:
    nli = Classified("contradict", 0.01)
    text = "An introductory detail establishes general background. " * 20
    text += "Network filesystems cannot supply the shared-memory coordination."
    _best_entail(text, "Network filesystems supply shared-memory coordination.", nli)

    assert any("cannot supply the shared-memory coordination" in p for p in nli.premises)


def test_failed_measurement_is_unavailable_not_a_successful_neutral_check() -> None:
    rows = _verify_claims(
        "Only one writer can write [[p1]].",
        {"p1": passage("Only one writer can write.")},
        Classified("neutral", 0.0, available=False),
    )
    assert rows[0]["verification_status"] == "unavailable"
    assert rows[0]["verdict"] == "weak"


def test_missing_citation_is_unresolved_rather_than_a_measured_contradiction() -> None:
    rows = _verify_claims("Only one writer can write [[missing]].", {}, Classified("entail", 1.0))
    assert rows[0]["verification_status"] == "unresolved"
    assert rows[0]["verification_reason"] == "missing_evidence"
    assert rows[0]["verdict"] == "unsupported"


def test_a_real_contradiction_is_distinct_from_missing_support() -> None:
    rows = _verify_claims(
        "Two writers can write [[p1]].",
        {"p1": passage("Only one writer can write.")},
        Classified("contradict", 0.01),
    )
    assert rows[0]["verification_status"] == "contradicted"


def test_table_cells_are_checked_with_row_identity_and_column_meaning() -> None:
    table = (
        "| Mode | Waits | Busy handler |\n"
        "| --- | --- | --- |\n"
        "| PASSIVE | does not wait [[p1]] | never invoked [[p1]] |\n"
    )
    rows = _verify_claims(table, {"p1": passage("PASSIVE never waits.")}, Classified("entail", 0.9))
    assert len(rows) == 1
    assert rows[0]["claim"]["text"] == (
        "Mode: PASSIVE; Waits: does not wait; Busy handler: never invoked"
    )
    assert rows[0]["claim"]["cited_passage_ids"] == ["p1"]


def test_inline_citations_keep_subject_and_qualification_together() -> None:
    text = "Readers proceed concurrently [[p1]], but only one writer may commit [[p2]]."
    spans = _claim_spans(text)
    assert len(spans) == 1
    assert spans[0].text == "Readers proceed concurrently, but only one writer may commit"
    assert spans[0].cited_passage_ids == ("p1", "p2")
    assert text[spans[0].start : spans[0].end] == text


def test_uncited_tail_is_checked_without_losing_the_sentence_subject() -> None:
    spans = _claim_spans("The device works [[p1]], only when connected to power.")
    assert len(spans) == 1
    assert spans[0].text == "The device works, only when connected to power"


def test_separate_uncited_assertion_does_not_acquire_its_neighbors_citation() -> None:
    spans = _claim_spans("The device works [[p1]]. It also runs without power.")
    assert len(spans) == 2
    assert spans[1].text == "It also runs without power"
    assert spans[1].cited_passage_ids == ()


def test_saved_clause_ledgers_remain_usable_when_filtering_an_older_answer() -> None:
    text = "Readers proceed concurrently [[p1]], but writers remain serial [[p2]]."
    legacy = [
        {
            "claim": {"text": "Readers proceed concurrently", "cited_passage_ids": ["p1"]},
            "verdict": "supported",
        },
        {
            "claim": {"text": ", but writers remain serial", "cited_passage_ids": ["p2"]},
            "verdict": "supported",
        },
    ]
    assert _retain_supported_claims(text, legacy) == text
    legacy[1]["verdict"] = "weak"
    # Do not keep the first clause while silently discarding its qualification.
    assert _retain_supported_claims(text, legacy) == ""
