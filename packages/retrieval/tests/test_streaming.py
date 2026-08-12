"""Re-scope post-processing in the research stream — `_drop_weak`.

Drops non-supported claims and strips their citation markers from the prose, so a
'drop weak' re-scope leaves only what held up. Pure function over the answer dict.
"""

from __future__ import annotations

from disco.retrieval.models import Passage
from disco.retrieval.streaming import _drop_weak, _verify_claims


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
