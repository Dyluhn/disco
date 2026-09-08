from disco.retrieval.deep_research.quality_audit import (
    ClaimRecord,
    corroboration_counts,
    find_high_specificity_claims,
    hedge_boilerplate_metrics,
    near_duplicate_body_paragraphs,
    source_concentration,
)


def _work(source: str) -> str:
    return {"a1": "work-a", "a2": "work-a", "b1": "work-b", "c1": "work-c"}.get(source, source)


def test_specific_claims_count_distinct_works_and_domains() -> None:
    claims = (
        ClaimRecord(
            "c1",
            "The first release affected 42% of users and cost $3 million.",
            ("a1", "a2", "b1"),
        ),
        ClaimRecord("c2", "The rollout was gradual.", ("a1",)),
    )
    findings = find_high_specificity_claims(
        claims,
        _work,
        lambda source: {"a1": "one.test", "a2": "one.test", "b1": "two.test"}[source],
    )
    assert findings[0].matched_rules == ("percentage", "currency", "number", "superlative")
    assert findings[0].work_count == 2
    assert findings[0].domain_count == 2
    assert not findings[0].needs_corroboration
    assert len(findings) == 1


def test_corroboration_does_not_count_two_passages_from_one_work() -> None:
    result = corroboration_counts(
        (ClaimRecord("c", "A result", ("a1", "a2")),),
        _work,
        lambda source: "one.test" if source in {"a1", "a2"} else source,
    )
    assert result[0].work_count == 1
    assert result[0].domain_count == 1


def test_source_concentration_is_claim_level() -> None:
    claims = (
        ClaimRecord("1", "one", ("a1", "a2")),
        ClaimRecord("2", "two", ("a1",)),
        ClaimRecord("3", "three", ("b1",)),
        ClaimRecord("4", "four", ("c1",)),
    )
    metric = source_concentration(claims, _work, threshold=0.5)
    assert metric.work_claim_counts == (("work-a", 2), ("work-b", 1), ("work-c", 1))
    assert metric.dominant_work == "work-a"
    assert metric.dominant_share == 0.5
    assert metric.flagged


def test_duplicate_body_paragraphs_ignore_citations_and_summary() -> None:
    repeated = (
        "The measured result remained robust across every reported deployment, "
        "including the larger evaluation cohort and the independent replication "
        "that followed the original study"
    )
    body = (f"{repeated} [[a]].", f"{repeated} [[b]].", "A different finding.")
    duplicates = near_duplicate_body_paragraphs(f"{repeated} [[summary]].", body)
    assert [(item.first_index, item.second_index) for item in duplicates] == [(0, 1)]


def test_duplicate_check_ignores_short_repeated_fragments() -> None:
    assert near_duplicate_body_paragraphs(
        "", ("The result is robust [[a]].", "The result is robust [[b]].")
    ) == ()


def test_hedge_metrics_report_repeated_boilerplate() -> None:
    metrics = hedge_boilerplate_metrics(
        ("This may work.", "This may work, but evidence is mixed.", "Certain.")
    )
    assert metrics.paragraph_count == 3
    assert metrics.hedge_phrase_count == 3
    assert ("may", 2) in metrics.repeated_phrases
