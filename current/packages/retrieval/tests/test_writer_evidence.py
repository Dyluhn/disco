"""Source compaction must not hide the evidence research actually acquired."""

import hashlib

from disco.retrieval.deep_research._writer_evidence import format_evidence_pool
from disco.retrieval.models import Passage


def source(text):
    return Passage(
        id="primary",
        source_url="https://example.test/standard",
        source_title="Primary standard",
        text=text,
    )


def inspection(passage, start, end):
    return {
        "kind": "source_inspection",
        "ok": True,
        "source_id": passage.id,
        "start": start,
        "end": end,
        "source_sha256": hashlib.sha256(passage.text.encode()).hexdigest(),
    }


def test_task_focused_compaction_keeps_rules_in_the_middle_of_a_long_standard():
    rule = (
        "Response storage MUST NOT retain private credentials. "
        "This is not a general deletion guarantee."
    )
    passage = source(
        "Navigation and contents. " * 500 + rule + " References and author addresses." * 500
    )
    rendered = format_evidence_pool(
        [passage], char_budget=1800, query="Response storage and private credentials"
    )
    assert rule in rendered
    assert passage.source_url in rendered
    assert len(rendered) <= 1800
    assert len(passage.text) > 20000


def test_inspected_qualification_survives_when_it_differs_from_query_keywords():
    qualification = "The measured operating lifetime is only two years."
    text = "Deployment storage capacity measured at the demonstration. " * 50
    text += " Appendix filler." * 300 + qualification + " Closing notes." * 300
    passage = source(text)
    start = text.index(qualification)
    rendered = format_evidence_pool(
        [passage],
        char_budget=2600,
        query="Deployment storage capacity",
        trail=[inspection(passage, start, start + len(qualification))],
    )
    assert qualification in rendered
    assert "Deployment storage capacity" in rendered
    assert len(rendered) <= 2600


def test_stale_inspection_does_not_select_a_range_in_different_source_bytes():
    qualification = "A stale measurement is not this source's finding."
    text = "Deployment storage capacity measured. " * 100 + " Middle filler." * 400 + qualification
    passage = source(text)
    start = text.index(qualification)
    row = inspection(passage, start, len(text))
    row["source_sha256"] = "0" * 64
    rendered = format_evidence_pool(
        [passage], char_budget=1600, query="Deployment storage capacity", trail=[row]
    )
    assert qualification not in rendered


def test_small_task_view_still_includes_source_text():
    passage = source("Deployment storage capacity measured. " * 100)
    rendered = format_evidence_pool([passage], char_budget=400, query="Deployment storage capacity")
    assert "Deployment storage capacity" in rendered
    assert len(rendered) <= 400


def test_complete_pool_is_not_compacted_by_unequal_research_weights():
    main = source("Required configuration and qualifications. " * 30)
    small = source("Short observation.").model_copy(update={"id": "small"})
    coverage = {
        "covered": [
            {"angle": f"Observation {index}", "evidence_ids": ["small"]} for index in range(20)
        ]
    }
    rendered = format_evidence_pool(
        [main, small], char_budget=2200, query="Required configuration", coverage=coverage
    )
    assert main.text.strip() in rendered
    assert small.text in rendered
    assert len(rendered) <= 2200


def test_short_sources_release_their_unused_budget_to_long_source():
    main = source("Required configuration and qualifications. " * 30)
    short_sources = [
        source("Brief observation.").model_copy(update={"id": f"short{index}"})
        for index in range(4)
    ]
    oversized = source("Unrelated appendix content. " * 500).model_copy(update={"id": "large"})
    rendered = format_evidence_pool(
        [main, *short_sources, oversized], char_budget=4200, query="Required configuration"
    )
    assert main.text.strip() in rendered
    assert all(p.text in rendered for p in short_sources)
    assert len(rendered) <= 4200


def test_local_context_expansion_keeps_qualification_after_the_selected_rule():
    qualification = "Operation is unsafe until the former leader is fenced."
    text = "Deployment capacity storage. " * 100 + " Unrelated notes." * 280
    text += "Deployment capacity storage configuration synchronization. " * 36
    text += " Routine implementation details." * 100 + qualification
    text += " Appendix and references." * 300
    passage = source(text)
    rendered = format_evidence_pool(
        [passage],
        char_budget=12500,
        query="Deployment capacity storage configuration synchronization",
    )
    assert qualification in rendered
    assert len(rendered) <= 12500
    assert passage.text == text


def test_the_evidence_budget_scales_with_the_depth_tier():
    """An exhaustive run admits far more source text than a quick one, and a
    fixed pool budget divides it into shares too small to carry the notes."""
    from disco.retrieval.deep_research.depth import bounds_for

    assert bounds_for("quick").evidence_char_budget == 220_000
    assert bounds_for("standard_deep").evidence_char_budget == 220_000
    assert bounds_for("exhaustive").evidence_char_budget == 260_000


def test_format_evidence_pool_honours_the_tier_budget():
    from disco.retrieval.deep_research.depth import bounds_for

    passages = [
        source("Required configuration and qualifications. " * 400).model_copy(
            update={"id": f"p{index}"}
        )
        for index in range(6)
    ]

    quick = format_evidence_pool(
        passages, char_budget=bounds_for("quick").evidence_char_budget, query="configuration"
    )
    deep = format_evidence_pool(
        passages, char_budget=bounds_for("exhaustive").evidence_char_budget, query="configuration"
    )

    assert len(quick) <= 220_000
    assert len(deep) <= 260_000
    assert len(deep) >= len(quick)
