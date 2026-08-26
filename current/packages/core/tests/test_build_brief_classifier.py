"""AppKit EPIC B — the deterministic Build Brief classifier.

Locks the classifier to the SHARED golden fixture (consumed by the TS classifier
test too, so the two implementations can't drift) and proves determinism: the same
request always yields the same brief.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from disco.core.appkit import BuildBrief, appkit_applicable, classify_build_brief

_GOLDEN = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "disco"
    / "core"
    / "appkit"
    / "build_brief_golden.json"
)


def _cases() -> list[dict]:
    return json.loads(_GOLDEN.read_text(encoding="utf-8"))["cases"]


def test_golden_fixture_exists_and_is_nonempty() -> None:
    cases = _cases()
    assert cases, "golden fixture must have cases"
    # Every case carries a request + a full expected brief.
    for c in cases:
        assert "request" in c and "expected" in c
        assert set(c["expected"]) == set(BuildBrief().model_dump())


@pytest.mark.parametrize("case", _cases(), ids=lambda c: c["request"][:32] or "<empty>")
def test_classifier_matches_golden(case: dict) -> None:
    got = classify_build_brief(case["request"]).model_dump()
    assert got == case["expected"], (
        f"classifier drifted from golden for {case['request']!r}:\n"
        f"  got      {got}\n  expected {case['expected']}"
    )


def test_classifier_is_deterministic() -> None:
    # Same input twice → byte-identical output (no model call, no randomness).
    for case in _cases():
        a = classify_build_brief(case["request"]).model_dump()
        b = classify_build_brief(case["request"]).model_dump()
        assert a == b


def test_app_kind_and_audience_examples() -> None:
    g = classify_build_brief("Build me a snake game with a leaderboard")
    assert g.app_kind == "game"
    assert "leaderboard" in g.must_have_sections

    biz = classify_build_brief("An analytics dashboard for business teams")
    assert biz.app_kind == "dashboard"
    assert biz.audience == "business"

    # No build signal at all → unknown / general / empty sections.
    blank = classify_build_brief("hmm")
    assert blank.app_kind == "unknown"
    assert blank.audience == "general"
    assert blank.must_have_sections == []


def test_primary_goal_collapses_whitespace_and_caps_length() -> None:
    brief = classify_build_brief("  build   a\n\ntodo   app  ")
    assert brief.primary_goal == "build a todo app"
    long = "build " + "x" * 500
    assert len(classify_build_brief(long).primary_goal) == 200


def test_key_entities_drop_stopwords_and_dedupe() -> None:
    brief = classify_build_brief("build a a a recipe recipe manager for cooks")
    # 'build'/'a'/'for' are filler/stopwords; 'recipe' de-duped.
    assert brief.key_entities == ["recipe", "manager", "cooks"]


def test_authorization_is_functional_and_style_words_do_not_imply_it() -> None:
    rbac = classify_build_brief("Build a forum with RBAC for members and moderators")
    assert "authorization" in rbac.must_have_sections

    # A visual direction is orthogonal: it should not silently add an
    # authorization requirement to an otherwise equivalent request.
    brutalist = classify_build_brief("Build a brutalist forum")
    plain = classify_build_brief("Build a forum")
    assert "authorization" not in brutalist.must_have_sections
    assert brutalist.must_have_sections == plain.must_have_sections


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Build a forum with RBAC", True),
        ("Build a forum with authentication and a database", True),
        ("Build a brutalist forum", False),
        ("Build a beautiful website", False),
        ("Build a landing page", False),
        ("Build a blog", False),
        ("Build a web app", False),
        ("Build a web app with authentication", True),
        ("Build an API with authentication", False),
        ("Build a CLI that imports CSV files", False),
        ("Build a snake game", False),
        ("Build a SaaS dashboard", True),
    ],
)
def test_appkit_applicability_is_web_and_functional_not_visual(
    text: str, expected: bool
) -> None:
    assert appkit_applicable(classify_build_brief(text)) is expected
