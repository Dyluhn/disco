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

from types import SimpleNamespace

import pytest
from disco.core.events import PlanEvent
from disco.core.loop.plans import (
    Planner,
    validate_plan_done_conditions,
    validate_raw_plan_done_conditions,
)


def _planner() -> Planner:
    return Planner(  # type: ignore[arg-type] — parse-only stand-in
        loop=SimpleNamespace(_plan_step_predicates={}, _autonomous=False)
    )


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


def test_steps_item_wrapper_unwraps_for_minimax_dialect() -> None:
    ev = _planner().plan_from_args(
        {
            "summary": "Plan",
            "steps": {"item": [{"title": "Scaffold"}, {"title": "Wire it up"}]},
        },
        events=[],
    )

    assert [s.title for s in ev.steps] == ["Scaffold", "Wire it up"]


def test_done_condition_validation_normalizes_guest_paths_without_prefix_collisions() -> None:
    ev = _planner().plan_from_args(
        {
            "summary": "Plan",
            "steps": [
                {
                    "title": "Directory mistake",
                    "done_condition": {
                        "kind": "file_exists",
                        "path": "/workspace/release/fonts",
                    },
                },
                {
                    "title": "Nested file",
                    "done_condition": {
                        "kind": "file_exists",
                        "path": "workspace/release/fonts/proof.woff2",
                    },
                },
                {
                    "title": "Similar prefix is a file",
                    "done_condition": {
                        "kind": "file_exists",
                        "path": "release/fonts-backup",
                    },
                },
            ],
        },
        events=[],
    )

    errors = validate_plan_done_conditions(ev)
    assert len(errors) == 1
    assert "release/fonts" in errors[0]
    assert "release/fonts/proof.woff2" in errors[0]
    assert "fonts-backup" not in errors[0]


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:3000",
        "http://sub.localhost:8080",
        "http://127.0.0.42:3000",
        "http://[::1]:3000",
    ],
)
def test_done_condition_validation_rejects_loopback_http(url: str) -> None:
    ev = _planner().plan_from_args(
        {
            "summary": "Plan",
            "steps": [
                {
                    "title": "Serve",
                    "done_condition": {"kind": "http_ok", "url": url},
                }
            ],
        },
        events=[],
    )

    assert "local preview host" in validate_plan_done_conditions(ev)[0]


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/health",
        "http://192.0.2.1:8080/health",
        "http://[2001:db8::1]/health",
    ],
)
def test_done_condition_validation_preserves_concrete_external_http(url: str) -> None:
    ev = _planner().plan_from_args(
        {
            "summary": "Plan",
            "steps": [
                {
                    "title": "Check deployed service",
                    "done_condition": {
                        "kind": "http_ok",
                        "url": url,
                    },
                }
            ],
        },
        events=[],
    )

    assert validate_plan_done_conditions(ev) == []


@pytest.mark.parametrize(
    "url",
    [
        "http://should-be-verified-later",
        "https://preview",
        "http://internal:3000/health",
    ],
)
def test_done_condition_validation_rejects_single_label_placeholder_http(
    url: str,
) -> None:
    ev = _planner().plan_from_args(
        {
            "summary": "Plan",
            "steps": [
                {
                    "title": "Check a future service",
                    "done_condition": {"kind": "http_ok", "url": url},
                }
            ],
        },
        events=[],
    )

    errors = validate_plan_done_conditions(ev)
    assert len(errors) == 1
    assert "concrete, already-known fully qualified hostname or IP address" in errors[0]
    assert "omit the condition" in errors[0]


def test_raw_done_condition_validation_never_silently_drops_malformed_gate() -> None:
    errors = validate_raw_plan_done_conditions(
        {
            "steps": [
                {"title": "missing path", "done_condition": {"kind": "file_exists"}},
                {"title": "wrong shape", "done_condition": "file_exists output.txt"},
                {"title": "explicit omission", "done_condition": None},
                {"title": "no condition"},
            ]
        }
    )

    assert len(errors) == 2
    assert all("not silently discarded" in error for error in errors)


def test_done_condition_validation_rejects_impossible_file_and_command_shapes() -> None:
    ev = _planner().plan_from_args(
        {
            "summary": "Plan",
            "steps": [
                {
                    "title": "root directory",
                    "done_condition": {"kind": "file_exists", "path": "/workspace"},
                },
                {
                    "title": "traversal",
                    "done_condition": {
                        "kind": "file_exists",
                        "path": "/workspace/../../etc/passwd",
                    },
                },
                {
                    "title": "hard denied",
                    "done_condition": {"kind": "command", "cmd": "rm -rf /"},
                },
                {
                    "title": "safe command",
                    "done_condition": {"kind": "command", "cmd": "test -s output.txt"},
                },
            ],
        },
        events=[],
    )

    errors = validate_plan_done_conditions(ev)
    assert len(errors) == 3
    assert sum("safe exact workspace file" in error for error in errors) == 2
    assert sum("hard-denied" in error for error in errors) == 1


def _command_plan(command: str) -> PlanEvent:
    return _planner().plan_from_args(
        {
            "summary": "Command gate",
            "steps": [
                {
                    "title": "Verify",
                    "done_condition": {"kind": "command", "cmd": command},
                }
            ],
        },
        events=[],
    )


@pytest.mark.parametrize(
    "command",
    [
        "curl -f http://localhost:8080",
        "curl -a http://localhost:8080",
        "curl -O http://localhost:8080",
        "wget -p http://localhost:8080",
        "curl http'://'localhost:8080",
        'curl "$URL"',
        "timeout 5 npm run dev",
        "env -u FOO npm run dev",
        "bash -ec 'npm run dev'",
        "python manage.py runserver",
        "python -m flask run",
        "npm exec vite",
        "pnpm exec vite",
        "npm run --silent dev",
        "yarn run --cwd app dev",
        "command -- npm run dev",
    ],
)
def test_command_done_condition_rejects_local_preview_lifecycle_shapes(command: str) -> None:
    errors = validate_plan_done_conditions(_command_plan(command))
    assert len(errors) == 1
    assert "approved plan verifier" in errors[0]
    assert "preview_start" in errors[0]


@pytest.mark.parametrize(
    "command",
    [
        "grep -q 'http://localhost:8080' README.md",
        "printf '%s\\n' http://localhost:8080 | grep -q localhost",
        "vite --config vite.config.js build",
        "make -n serve",
        "http GET https://example.com",
        "curl -f https://example.com/health",
        "curl -H x:y https://example.com/health",
        "http --auth user:pass GET https://example.com",
        "test -f index.html && npm test",
    ],
)
def test_command_done_condition_preserves_finite_verification_shapes(command: str) -> None:
    assert validate_plan_done_conditions(_command_plan(command)) == []


# H368 — command strings are not statically interpreted as programming
# languages. Safety/liveness is enforced by the execution boundary and plan
# authority lifecycle, so both the historical bad verifier and legitimate
# lookalikes remain structurally valid plan-owned conditions.
@pytest.mark.parametrize(
    "command",
    [
        "python -c \"import sys; exec(open('primes.py').read()) if input('check?') else None\"",
        "python -c \"input=lambda prompt: 'ok'; assert input('check?') == 'ok'\"",
        'node -e "const x={question:()=>42}; if(x.question()!=42) process.exit(1)"',
        'python -c "import sys; assert not sys.stdin.isatty()"',
        "bash -c 'bash -c \"printf ok | grep -q ok\"'",
    ],
)
def test_h368_command_language_is_not_statically_adjudicated(command: str) -> None:
    assert validate_plan_done_conditions(_command_plan(command)) == []
