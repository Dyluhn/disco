"""[REL-RC A3] Bounded regex-harvest recovery for MiniMax-M3's mis-routed plan steps.
The model authors correct steps but serializes the whole {steps:[...]} object into the wrong
submit_plan parameter, leaving `steps` empty. We recover the titles without invoking any parser.
"""
from __future__ import annotations

from disco.core.loop.plans import _coerce_step, _harvest_steps


def test_harvest_recovers_run001_rev4_steps_object_in_summary() -> None:
    # VERBATIM-shaped from the live failure: a well-formed steps object stringified into `summary`,
    # `steps` kwarg empty. Titles in single quotes WITH inner double quotes.
    args = {
        "summary": (
            "{'steps': [{'title': 'Update hero copy to \"Grand Opening\"', "
            "'detail': 'edit index.html', 'done_condition': {'kind':'file_exists','path':'index.html'}}, "
            "{'title': 'Re-verify the page renders', 'detail': '...'}], 'context': 'two edits'}"
        ),
        "steps": [],
    }
    out = _harvest_steps(args)
    assert [s.title for s in out] == [
        'Update hero copy to "Grand Opening"',
        "Re-verify the page renders",
    ]


def test_harvest_steps_param_as_string_is_direct() -> None:
    # The steps array routed as a STRING into the `steps` slot — harvested directly (no envelope).
    args = {"steps": "[{'title': 'Build the hero'}, {'title': 'Add pricing'}]"}
    out = _harvest_steps(args)
    assert [s.title for s in out] == ["Build the hero", "Add pricing"]


def test_no_false_positive_title_without_steps_envelope() -> None:
    # An incidental `title:` in summary with NO steps: envelope must NOT fabricate a step.
    args = {"summary": "Set the page title: 'Welcome' and keep the blue hero.", "steps": []}
    assert _harvest_steps(args) == []


def test_double_quoted_titles_with_envelope() -> None:
    args = {"context": '{"steps": [{"title": "Step one"}, {"title": "Step two"}]}', "steps": []}
    assert [s.title for s in _harvest_steps(args)] == ["Step one", "Step two"]


def test_size_cap_skips_huge_blob() -> None:
    big = "{'steps': [" + ("{'title': 'x'}," * 20000) + "]}"  # > 64KB
    assert _harvest_steps({"summary": big, "steps": []}) == []


def test_match_count_capped_at_64() -> None:
    inside = "".join(f"{{'title': 'S{i}'}}," for i in range(200))
    args = {"steps": f"[{inside}]"}
    assert len(_harvest_steps(args)) == 64


def test_coerce_step_normal_shapes_unchanged() -> None:
    assert _coerce_step({"title": "A", "detail": "d"}).title == "A"
    assert _coerce_step("bare title").title == "bare title"
    assert _coerce_step({"no_title": 1}) is None
    assert _coerce_step(123) is None


def test_harvest_empty_when_nothing_recoverable() -> None:
    assert _harvest_steps({"summary": "just prose, no structure", "steps": []}) == []
    assert _harvest_steps({}) == []
