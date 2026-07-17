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
        "nice -n 10 npm run dev",
        "stdbuf -oL curl http://localhost:8080",
    ],
)
def test_command_done_condition_rejects_local_preview_lifecycle_shapes(command: str) -> None:
    errors = validate_plan_done_conditions(_command_plan(command))
    assert len(errors) == 1
    assert "immutable finish gate" in errors[0]
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
        "nice -n 10 npm test",
        "stdbuf -oL grep -q ok output.txt",
    ],
)
def test_command_done_condition_preserves_finite_verification_shapes(command: str) -> None:
    assert validate_plan_done_conditions(_command_plan(command)) == []


# ---------------------------------------------------------------------------
# H368 — stdin-dependent / interactive-prompt rejection (negative)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command, expected_substring",
    [
        # Exact RUN-768 predicate — input() without source
        (
            "python -c \"import sys; exec(open('primes.py').read()) if input('check?') else None\"",
            "calls input()",
        ),
        # Python input() even with piped input (always rejected)
        (
            "echo yes | python -c "
            "\"import sys; exec(open('primes.py').read()) if input('check?') else None\"",
            "calls input()",
        ),
        (
            "python -c \"import builtins; builtins.input('check?')\"",
            "calls input()",
        ),
        (
            "python -c \"from builtins import input as ask; ask('check?')\"",
            "calls input()",
        ),
        # Python input() with < redirection (always rejected — prompt API)
        (
            "python -c \"x = input('enter: '); print(x)\" < /dev/null",
            "calls input()",
        ),
        # Python sys.stdin.readline() without explicit source
        (
            'python -c "import sys; sys.stdin.readline()"',
            "sys.stdin",
        ),
        (
            'python -c "from sys import stdin; stdin.readline()"',
            "sys.stdin",
        ),
        # Python open(0).read() without source
        (
            'python -c "open(0).read()"',
            "open(0)",
        ),
        # Python os.read(0, ...) without source
        (
            'python -c "import os; os.read(0, 1024)"',
            "os.read(0",
        ),
        (
            'python -c "import os as operating_system; operating_system.read(0, 1024)"',
            "os.read(0",
        ),
        (
            'python -c "from os import read as read_fd; read_fd(0, 1024)"',
            "os.read(0",
        ),
        (
            'python -c "import os; os.fdopen(0).read()"',
            "reads from stdin",
        ),
        (
            'python -c "fd = 0; open(fd).read()"',
            "reads from stdin",
        ),
        (
            'python -c "open(file=0).read()"',
            "reads from stdin",
        ),
        (
            'python -c "import sys; sys.__stdin__.readline()"',
            "sys.stdin",
        ),
        (
            'python -c "from sys import __stdin__; __stdin__.readline()"',
            "sys.stdin",
        ),
        # Direct shell read without source
        ("read -r line", "calls the shell `read` builtin"),
        # Shell readarray without source
        ("readarray -t lines", "calls the shell `readarray` builtin"),
        # Shell mapfile without source
        ("mapfile -t lines", "calls the shell `mapfile` builtin"),
        # Shell select without source
        ("select opt in a b c; do break; done", "calls the shell `select` builtin"),
        # while read without source
        ("while read -r line; do echo $line; done", "uses `while read`"),
        ("builtin read -r line", "calls the shell `read` builtin"),
        (
            "if true; then read -r line; fi",
            "calls the shell `read` builtin",
        ),
        (
            "for item in one; do read -r line; done",
            "calls the shell `read` builtin",
        ),
        (
            "bash -c '(read -r value)'",
            "calls the shell `read` builtin",
        ),
        (
            "bash -c '{ read -r value; }'",
            "calls the shell `read` builtin",
        ),
        (
            "if python -c \"input('check?')\"; then true; fi",
            "calls input()",
        ),
        (
            "time node -e \"prompt('check?')\"",
            "calls prompt()",
        ),
        (
            'time -f %E python -c "import sys; sys.stdin.readline()"',
            "sys.stdin",
        ),
        (
            "bash -c '(python -c \"input(1)\")'",
            "calls input()",
        ),
        (
            'nice -n 10 python -c "import sys; sys.stdin.readline()"',
            "sys.stdin",
        ),
        (
            'stdbuf -oL python -c "import sys; sys.stdin.readline()"',
            "sys.stdin",
        ),
        (
            "ionice -c 3 bash -c 'read -r value'",
            "calls the shell `read` builtin",
        ),
        (
            'chrt -f 10 python -c "import sys; sys.stdin.readline()"',
            "sys.stdin",
        ),
        (
            'chrt -T 100 -D 200 -P 300 -f 10 python -c "import sys; sys.stdin.readline()"',
            "sys.stdin",
        ),
        (
            'rlwrap python -c "import sys; sys.stdin.readline()"',
            "interactive `rlwrap` wrapper",
        ),
        (
            "time rlwrap true",
            "interactive `rlwrap` wrapper",
        ),
        # Nested shell reader without outer source
        ("bash -c 'read -r value'", "calls the shell `read` builtin"),
        (
            "bash -c 'bash -c \"read -r value\"'",
            "calls the shell `read` builtin",
        ),
        # JS process.stdin without source
        (
            "node -e \"process.stdin.on('data', d => console.log(d))\"",
            "process.stdin",
        ),
        # JS Deno.stdin without source
        (
            'deno eval "await Deno.stdin.readable"',
            "Deno.stdin",
        ),
        # JS Bun.stdin without source
        (
            'bun -e "const d = Bun.stdin"',
            "Bun.stdin",
        ),
        # JS prompt() — always rejected
        (
            "node -e \"const x = prompt('enter value')\"",
            "calls prompt()",
        ),
        (
            "node -e \"const rl = require('node:readline').createInterface("
            "{input: process.stdin}); rl.question('value?', () => {})\"",
            "calls prompt()",
        ),
        # JS prompt() even with piped input — always rejected
        (
            "echo yes | node -e \"const x = prompt('ok?')\"",
            "calls prompt()",
        ),
        # 2<file does NOT provide stdin, so shell read is still rejected
        ("read -r line 2</dev/null", "calls the shell `read` builtin"),
        ("read -r line 2<</dev/null", "calls the shell `read` builtin"),
        ("read -r line 2<>/dev/null", "calls the shell `read` builtin"),
    ],
)
def test_command_done_condition_rejects_stdin_dependent_commands(
    command: str, expected_substring: str
) -> None:
    errors = validate_plan_done_conditions(_command_plan(command))
    assert len(errors) == 1, f"expected 1 error, got {errors}"
    assert expected_substring in errors[0], f"expected {expected_substring!r} in {errors[0]!r}"
    assert "immutable finish gate" in errors[0]
    assert "reads from stdin" in errors[0] or "prompts interactively" in errors[0]


# ---------------------------------------------------------------------------
# H368 — legitimate stdin-supplied commands (positive — must NOT be rejected)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        # H342: read -r with process substitution (< <(...) supplies stdin)
        "read -r value < <(printf 71); [[ $value == 71 ]]",
        # Python sys.stdin with < input.txt
        'python -c "import sys; print(sys.stdin.read())" < input.txt',
        # Python sys.stdin with pipe source
        'printf 42 | python -c "import sys; print(sys.stdin.read())"',
        # Bash |& also supplies the next command's stdin.
        'printf 42 |& python -c "import sys; print(sys.stdin.read())"',
        # <> and <& are explicit fd-0 sources.
        'python -c "import sys; print(sys.stdin.read())" <> input.txt',
        'python -c "import sys; print(sys.stdin.read())" 0<&3',
        # Python open(0) with pipe source
        'echo ok | python -c "print(open(0).read())"',
        # Nested bash -c with outer < redirection inheriting stdin
        "bash -c 'read -r value; echo $value' < input.txt",
        "bash -c 'bash -c \"read -r value\"' < input.txt",
        "bash -c 'if true; then read -r value; fi' < input.txt",
        "bash -c 'for item in one; do read -r value; done' < input.txt",
        ("bash -c 'if python -c \"import sys; sys.stdin.read()\"; then true; fi' < input.txt"),
        'time python -c "import sys; sys.stdin.read()" < input.txt',
        'time -f %E python -c "from sys import __stdin__; __stdin__.read()" < input.txt',
        'printf x | nice -n 10 python -c "import sys; sys.stdin.read()"',
        'stdbuf -oL python -c "open(file=0).read()" < input.txt',
        'ionice -c 3 bash -c "read -r value" < input.txt',
        'chrt -f 10 python -c "import sys; sys.__stdin__.read()" < input.txt',
        ('chrt -T 100 -D 200 -P 300 -f 10 python -c "import sys; sys.stdin.read()" < input.txt'),
        # Nested bash -c read with heredoc source
        "bash -c 'read -r value' <<< 'hello'",
        # JS process.stdin with pipe source (piped, stdin supplied)
        "echo data | node -e \"process.stdin.on('data', d => console.log(d))\"",
        # JS Deno.stdin with < source
        'deno eval "await Deno.stdin.readable" < input.txt',
        # JS Bun.stdin with <<< source
        'bun -e "process.stdout.write(Bun.stdin.value)" <<< hello',
        # Ordinary finite verification (no stdin dependency at all)
        "test -f index.html && npm test",
        'printf 42 | python -c "import sys; print(sys.stdin.read())" | grep -q 42',
        "grep -q success output.txt",
        # Python string literal containing 'input()' text — must not false-positive
        "python -c \"print('input() is a builtin')\"",
        # Python string literal containing 'sys.stdin' — must not false-positive
        "python -c \"msg = 'use sys.stdin'; print(msg)\"",
        # Shell grep containing 'read' in a string — must not false-positive
        "grep -q 'read the docs' README.md",
    ],
)
def test_command_done_condition_preserves_legitimate_stdin_commands(
    command: str,
) -> None:
    errors = validate_plan_done_conditions(_command_plan(command))
    assert errors == [], f"unexpected rejection: {errors}"
