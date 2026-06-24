"""`Planner.plan_from_args` parse-layer hygiene (build plan/progress display fixes).

Two live defects (build mode, MiniMax — conv_4a1e9405dba54aadb90e03589870630e):

  1. A stray `</summary>` tag echoed by the model leaked into the stored plan
     summary verbatim ("...road plane.</summary>") and rendered literally.
  2. A `submit_plan` with a summary but NO real steps inserted a fake
     "(the planner returned no concrete steps)" placeholder step, which the UI
     rendered as a broken numbered "1." step.

`plan_from_args` reads only its `arguments`/`events` inputs for these shapes (it
touches the loop only to harvest C18 predicates, which these args omit), so the
tests drive it through a Planner over a trivial stand-in loop.
"""

from __future__ import annotations

from disco.core.events import PlanEvent
from disco.core.loop.plans import Planner


def _planner() -> Planner:
    return Planner(loop=object())  # type: ignore[arg-type]  # plan_from_args ignores the loop here


def test_trailing_summary_close_tag_is_stripped() -> None:
    """The exact live shape: a summary ending in a stray `</summary>` is cleaned."""
    ev = _planner().plan_from_args(
        {
            "summary": "Move the car spawn onto a clear stretch of road.</summary>",
            "steps": [{"title": "Tune the spawn point"}],
        },
        events=[],
    )
    assert isinstance(ev, PlanEvent)
    assert ev.summary == "Move the car spawn onto a clear stretch of road."
    assert "</summary>" not in ev.summary
    assert "<summary>" not in ev.summary


def test_fully_wrapped_summary_tags_are_stripped() -> None:
    """A summary the model wrapped in `<summary>…</summary>` is unwrapped."""
    ev = _planner().plan_from_args(
        {"summary": "<summary>Build the thing.</summary>", "steps": ["Do it"]},
        events=[],
    )
    assert ev.summary == "Build the thing."


def test_leaked_tags_stripped_from_step_titles_and_details() -> None:
    """Wrapper tags leaked into step text are stripped at the parse layer too."""
    ev = _planner().plan_from_args(
        {
            "summary": "A plan",
            "steps": [
                {"title": "First step</step>", "detail": "<detail>details here</detail>"},
            ],
        },
        events=[],
    )
    assert ev.steps[0].title == "First step"
    assert ev.steps[0].detail == "details here"


def test_legitimate_markup_in_summary_is_preserved() -> None:
    """We strip ONLY the plan-field tag vocabulary — a plan ABOUT html keeps its
    `<div>`/`<style>` markup."""
    ev = _planner().plan_from_args(
        {"summary": "Render a <div> with a <style> block", "steps": ["x"]},
        events=[],
    )
    assert ev.summary == "Render a <div> with a <style> block"


def test_no_steps_keeps_steps_empty_not_a_fake_placeholder() -> None:
    """A summary with no real steps yields an EMPTY steps list — never the legacy
    '(the planner returned no concrete steps)' placeholder that rendered as a
    broken numbered step."""
    ev = _planner().plan_from_args(
        {"summary": "Move the spawn off the intersection.</summary>", "steps": []},
        events=[],
    )
    assert ev.steps == []
    assert "planner returned no concrete steps" not in repr(ev.steps)


def test_blank_and_malformed_steps_drop_to_empty() -> None:
    """Blank strings / tag-only / contentless dict steps are dropped, not faked."""
    ev = _planner().plan_from_args(
        {"summary": "Plan", "steps": ["   ", "</step>", {}, {"detail": "no title"}]},
        events=[],
    )
    assert ev.steps == []


def test_non_string_field_values_are_coerced_not_crashed() -> None:
    """Malformed-value tolerance: a model that submits a NON-string value (int,
    list, dict) for summary / context / step title / detail must NOT crash the
    parse — the value is stringified (as the old `str(...)`-coercing code did),
    then tag-stripped."""
    ev = _planner().plan_from_args(
        {
            "summary": 42,  # int, not str
            "context": ["a", "b"],  # list, not str
            "steps": [
                {"title": 7, "detail": {"k": "v"}},  # int title, dict detail
                ["nested"],  # not str, not dict → skipped, no crash
            ],
        },
        events=[],
    )
    assert isinstance(ev, PlanEvent)
    assert ev.summary == "42"
    assert ev.context == "['a', 'b']"
    assert len(ev.steps) == 1  # the bare list step is skipped, not crashed
    assert ev.steps[0].title == "7"
    assert ev.steps[0].detail == "{'k': 'v'}"


def test_real_steps_still_survive() -> None:
    """The happy path is unchanged: real steps come through intact."""
    ev = _planner().plan_from_args(
        {
            "summary": "Plan",
            "steps": [
                {"title": "Scaffold", "detail": "make files"},
                "Wire it up",
            ],
        },
        events=[],
    )
    assert [s.title for s in ev.steps] == ["Scaffold", "Wire it up"]
    assert ev.steps[0].detail == "make files"
    assert ev.steps[1].detail is None
