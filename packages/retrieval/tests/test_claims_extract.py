"""A4.1 — section-claim extractor tests."""

from __future__ import annotations

from disco.retrieval.deep_research.claims import extract_section_claims

_PASSAGES = {
    "p0": "African swallows cruise at 11 m/s.",
    "p1": "European swallows are smaller and slower.",
}


def test_extracts_claim_text_and_cited_passage_texts() -> None:
    md = "The African swallow flies at about 11 m/s [[p0]]. The European swallow is slower [[p1]]."
    claims = extract_section_claims(md, _PASSAGES)
    assert len(claims) == 2
    c0_text, c0_passages = claims[0]
    assert "African swallow" in c0_text
    assert c0_passages == ["African swallows cruise at 11 m/s."]
    assert claims[1][1] == ["European swallows are smaller and slower."]


def test_multiple_citations_on_one_claim() -> None:
    md = "Both swallows differ in speed [[p0]][[p1]]."
    claims = extract_section_claims(md, _PASSAGES)
    assert len(claims) == 1
    assert len(claims[0][1]) == 2  # both passages attached


def test_skips_unknown_passage_ids() -> None:
    md = "An unsupported assertion [[p9]]."  # p9 not in the map
    assert extract_section_claims(md, _PASSAGES) == []


def test_strips_markdown_emphasis_from_claim() -> None:
    md = "The **African** swallow is fast [[p0]]."
    claims = extract_section_claims(md, _PASSAGES)
    assert "**" not in claims[0][0]
    assert "African" in claims[0][0]


def test_no_citations_no_claims() -> None:
    assert extract_section_claims("Just prose, no citations here.", _PASSAGES) == []
