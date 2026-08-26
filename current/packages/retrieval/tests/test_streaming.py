"""Re-scope post-processing in the research stream — `_drop_weak`.

Drops non-supported claims and strips their citation markers from the prose, so a
'drop weak' re-scope leaves only what held up. Pure function over the answer dict.
"""

from __future__ import annotations

import datetime
import json

import pytest
from disco.retrieval.models import ExtractedDoc, Passage, SearchHit
from disco.retrieval.streaming import (
    _build_final_answer,
    _drop_weak,
    _extract_round_passages,
    _RoundResult,
    _verify_claims,
)


def _answer() -> dict:
    return {
        "query": "q",
        "blocks": [
            {
                "kind": "prose",
                "id": "b0",
                "text": "Strong fact [[p0]]. Shaky fact [[p1]].",
                "cited_passage_ids": ["p0", "p1"],
            }
        ],
        "claims": [
            {"claim": {"text": "Strong fact", "cited_passage_ids": ["p0"]}, "verdict": "supported"},
            {"claim": {"text": "Shaky fact", "cited_passage_ids": ["p1"]}, "verdict": "weak"},
        ],
        "passages": [{"id": "p0"}, {"id": "p1"}],
        "unsupported_count": 0,
    }


def test_drop_weak_keeps_supported_and_strips_weak_citations():
    # Historical sealed id retained; the corrected boundary removes the weak
    # statement itself in addition to its citation marker.
    out = _drop_weak(_answer())
    # only the supported claim remains
    assert [c["verdict"] for c in out["claims"]] == ["supported"]
    # the weak statement itself is gone; the strong one stays
    text = out["blocks"][0]["text"]
    assert "[[p0]]" in text
    assert "[[p1]]" not in text
    assert "Shaky fact" not in text
    assert out["blocks"][0]["cited_passage_ids"] == ["p0"]
    assert out["unsupported_count"] == 0


def test_drop_weak_is_a_noop_when_all_supported():
    answer = _answer()
    answer["claims"][1]["verdict"] = "supported"
    out = _drop_weak(answer)
    assert out["blocks"][0]["text"] == answer["blocks"][0]["text"]  # nothing stripped
    assert len(out["claims"]) == 2


class _Entails:
    def entail(self, premise: str, hypothesis: str) -> str:
        return "entail"

    def score(self, premise: str, hypothesis: str) -> float:
        return 1.0


def test_verifier_rejects_uncited_prose_and_unknown_source_ids():
    passage = Passage(id="p0", source_url="https://x", source_title="X", text="Known fact.")
    claims = _verify_claims(
        "Known fact. [[p0]] Uncited assertion. Invented claim. [[made_up]]",
        {"p0": passage},
        _Entails(),
    )

    assert [claim["verdict"] for claim in claims] == [
        "supported",
        "unsupported",
        "unsupported",
    ]


@pytest.mark.parametrize(
    "text",
    [
        "Hubinger et al. (2024) demonstrated sleeper-agent behavior [[p0]].",
        (
            'A probe used the contrast "Are you doing something dangerous? '
            'Yes/No" to detect defection [[p0]].'
        ),
    ],
)
def test_verifier_does_not_split_abbreviations_or_quoted_questions(text: str) -> None:
    passage = Passage(
        id="p0",
        source_url="https://example.com/paper",
        source_title="Paper",
        text=text,
    )

    claims = _verify_claims(text, {"p0": passage}, _Entails())

    assert len(claims) == 1
    assert claims[0]["verdict"] == "supported"
    assert claims[0]["claim"]["cited_passage_ids"] == ["p0"]


def test_verifier_does_not_split_numeric_abbreviations_in_report_prose() -> None:
    """The acceptance report's ``c. 1250 - c. 1150`` must remain one claim."""
    text = (
        "One research starter places the catastrophic events between approximately "
        "1225 and 1150 BCE [[p0]], while another overview places the decline quite "
        "suddenly between around 1200–1150 BCE [[p1]], and a third describes that "
        "between c. 1250 - c. 1150 BCE, major cities were destroyed, whole "
        "civilizations fell, diplomatic and trade relations were severed, writing "
        "systems vanished, and there was widespread devastation and death on a scale "
        "never experienced before [[p2]]."
    )
    passages = {
        passage_id: Passage(
            id=passage_id,
            source_url=f"https://example.com/{passage_id}",
            source_title="Historical source",
            text="Supporting evidence.",
        )
        for passage_id in ("p0", "p1", "p2")
    }

    claims = _verify_claims(text, passages, _Entails())

    assert len(claims) == 3
    assert all(claim["verdict"] == "supported" for claim in claims)
    assert any(
        "a third describes that between c. 1250" in claim["claim"]["text"]
        for claim in claims
    )


def test_verifier_does_not_split_initialism_before_compound_or_noun() -> None:
    text = (
        "The U.S.-China performance gap is described as effectively closed, with "
        "models trading the lead since early 2025; DeepSeek-R1 briefly matched the "
        "top U.S. model in February 2025, and as of March 2026 Anthropic's top model "
        "leads by just 2.7% [[p0]] [[p1]]."
    )
    passages = {
        passage_id: Passage(
            id=passage_id,
            source_url=f"https://example.com/{passage_id}",
            source_title="AI benchmark source",
            text="Supporting evidence.",
        )
        for passage_id in ("p0", "p1")
    }

    claims = _verify_claims(text, passages, _Entails())

    assert len(claims) == 1
    assert claims[0]["verdict"] == "supported"
    assert claims[0]["claim"]["cited_passage_ids"] == ["p0", "p1"]


@pytest.mark.parametrize(
    "text",
    [
        "Hubinger et al. demonstrated sleeper-agent behavior [[p0]].",
        "The study dates the event to c. 1250 BCE [[p0]].",
        "The U.S. model led the benchmark [[p0]].",
        "Government and industry investment reflects that manufacturing focus, with "
        "the U.S. Department of Commerce reporting growth [[p0]].",
        "The finding was synthesized by Eric H. Cline [[p0]].",
    ],
)
def test_verifier_keeps_common_nonterminal_forms_in_one_claim(text: str) -> None:
    passage = Passage(
        id="p0",
        source_url="https://example.com/source",
        source_title="Source",
        text="Supporting evidence.",
    )

    claims = _verify_claims(text, {"p0": passage}, _Entails())

    assert len(claims) == 1
    assert claims[0]["verdict"] == "supported"


def test_verifier_does_not_split_person_initial_before_proper_surname() -> None:
    text = (
        "The dominant explanatory framework, synthesized by Eric H. Cline in "
        "*1177 B.C.: The Year Civilization Collapsed* (2014, revised 2021), argues "
        "that no single factor was sufficient alone but their simultaneous "
        "convergence overwhelmed the highly interconnected international system "
        "[[p0]] [[p1]]."
    )
    passages = {
        passage_id: Passage(
            id=passage_id,
            source_url=f"https://example.com/{passage_id}",
            source_title="Historical source",
            text="Supporting evidence.",
        )
        for passage_id in ("p0", "p1")
    }

    claims = _verify_claims(text, passages, _Entails())

    assert len(claims) == 1
    assert claims[0]["verdict"] == "supported"
    assert claims[0]["claim"]["cited_passage_ids"] == ["p0", "p1"]


async def test_streaming_extraction_uses_canonical_source_identity() -> None:
    class _Extraction:
        async def extract_many(self, urls: list[str]) -> list[ExtractedDoc]:
            return [
                ExtractedDoc(
                    url="https://example.com/release",
                    title="Release",
                    content="release evidence",
                    passages=[
                        Passage(
                            id="p-release",
                            source_url="https://example.com/release",
                            source_title="Release",
                            text="release evidence",
                        )
                    ],
                    status="ok",
                )
            ]

    hit = SearchHit(
        url="http://example.com/release/?utm_source=feed#details",
        title="Release",
        published_at=datetime.date(2026, 8, 11),
    )
    _docs, _passages, all_hits, round_keys = await _extract_round_passages(
        _Extraction(),  # type: ignore[arg-type]
        [hit],
        frozenset(),
        extract_cap=1,
        discover_limit=1,
    )

    assert all_hits[0]["status"] == "ok"
    assert all_hits[0]["published_at"] == "2026-08-11"
    json.dumps(all_hits)
    assert round_keys == frozenset({"example.com/release"})


def test_dated_final_answer_is_json_serializable() -> None:
    passage = Passage(
        id="dated",
        source_url="https://example.com/release",
        source_title="Release",
        text="The model was released.",
        published_at=datetime.date(2026, 8, 11),
    )
    result = _RoundResult(
        blocks=[],
        claims=[],
        top=[passage],
        all_hits=[],
        supported=0,
        this_round_urls=frozenset(),
        token_frames=[],
        answer_text="",
    )

    answer = _build_final_answer("What shipped?", result, [])

    assert answer["passages"][0]["published_at"] == "2026-08-11"
    json.dumps(answer)
