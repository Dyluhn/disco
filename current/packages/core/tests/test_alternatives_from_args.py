"""BW-03 — `Planner.alternatives_from_args` must DECOUPLE a "labeled choice"
from a "runnable recovery pick": an option without a tool_name is still a valid
clickable card, plain-string options become labels, and a content-free
"Option N" placeholder is NEVER emitted.

`alternatives_from_args` reads only its `arguments`/`events` inputs (it does not
touch the back-referenced loop), so the tests drive it through a Planner built
over a trivial stand-in loop.
"""

from __future__ import annotations

from disco.core.events import AlternativesEvent
from disco.core.loop.plans import Planner


def _planner() -> Planner:
    return Planner(loop=object())  # type: ignore[arg-type]  # method ignores the loop


def test_label_only_option_without_tool_name_is_kept() -> None:
    """An option with a label but NO tool_name is a valid choice, not dropped."""
    ev = _planner().alternatives_from_args(
        {
            "question": "Which direction?",
            "options": [
                {"title": "Refactor the parser"},  # label-only, no tool
                {"title": "Add a retry", "tool_name": "shell", "arguments": {"cmd": "x"}},
            ],
        },
        events=[],
    )
    assert isinstance(ev, AlternativesEvent)
    titles = [o.title for o in ev.options]
    assert "Refactor the parser" in titles  # label-only survived
    assert "Add a retry" in titles
    label_only = next(o for o in ev.options if o.title == "Refactor the parser")
    assert label_only.tool_name == ""  # decoupled: choice without a runnable tool


def test_plain_string_options_become_labels() -> None:
    """Bare strings (model shape drift) are accepted as choice labels."""
    ev = _planner().alternatives_from_args(
        {"question": "Pick one", "options": ["Use Postgres", "Use SQLite", "   "]},
        events=[],
    )
    assert ev is not None
    titles = [o.title for o in ev.options if o.id != "__continue__"]
    assert titles == ["Use Postgres", "Use SQLite"]  # blank string dropped


def test_never_falls_back_to_content_free_option_n() -> None:
    """An option carrying no label, description, or tool is skipped — never
    rendered as a meaningless "Option N" placeholder."""
    ev = _planner().alternatives_from_args(
        {
            "question": "Pick",
            "options": [
                {},  # no content at all → skipped
                {"description": "Roll back the migration"},  # description IS the label
                {"tool_name": "git_revert", "arguments": {}},  # tool name is the label
            ],
        },
        events=[],
    )
    assert ev is not None
    real = [o for o in ev.options if o.id != "__continue__"]
    titles = [o.title for o in real]
    assert all(not t.startswith("Option ") for t in titles)
    assert "Roll back the migration" in titles  # description promoted to label
    assert "git_revert" in titles  # tool name promoted to label as last resort
    assert len(real) == 2  # the empty {} option was skipped, not placeholdered
