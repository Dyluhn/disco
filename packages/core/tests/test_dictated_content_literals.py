"""REL-RC-O — dictated quoted content: extraction, metadata exclusion, and
application-title slot identity.

Split out of the former single test_dictated_content_finish_gate.py. Quoted
user literals in build prompts/follow-ups are event-derived content floors;
this module covers what counts as dictated content vs. metadata (serve
arguments, diagnostic-protocol scaffolding) and how the application-title
requirement slot tracks renames/retitles across revisions. See
test_dictated_content_scope.py for page/document scoping and scoped-edit
supersession, and test_dictated_content_app_bundle.py for the selected-app
entry/bundle inspection and finish-gate refusal/cap behavior.
"""

from __future__ import annotations

import pytest
from _dictated_content_support import _plan, _status, _user
from disco.core.loop.plan_conditions import (
    dictated_content_conditions_from_events,
    extract_dictated_content_literals,
)


def test_extracts_prompt_and_followup_literals_but_skips_commands_and_paths():
    text = (
        'Build a hero "Launch Day" with CTA \'Get Started\', keep "Plans / Pricing", '
        'then run "npm run build" and edit "src/app.js".'
    )
    assert extract_dictated_content_literals(text) == [
        "Launch Day",
        "Get Started",
        "Plans / Pricing",
    ]

    events = [
        _user(text, 1),
        _plan(1, 2),
        _status("plan_approved", 3),
        _user('Change every CTA button to say "Start Now"; run "python -m pytest -q".', 4),
        _status("planning", 5),
        _plan(2, 6),
    ]
    conditions = dictated_content_conditions_from_events(events)
    assert [(c.revision, c.literal) for c in conditions] == [
        (1, "Launch Day"),
        (1, "Get Started"),
        (1, "Plans / Pricing"),
        (2, "Start Now"),
    ]


def test_explicit_application_title_change_replaces_only_same_exact_slot():
    initial = _user(
        'Build an app titled exactly "Alpha" with CTA text "Keep me".',
        1,
    )
    replacement = _user('Change the app title to exactly "Beta".', 4)
    events = [
        initial,
        _plan(1, 2),
        _status("plan_approved", 3),
        replacement,
        _status("planning", 5),
        _plan(2, 6),
    ]

    conditions = dictated_content_conditions_from_events(events)

    assert [(condition.revision, condition.literal) for condition in conditions] == [
        (1, "Keep me"),
        (2, "Beta"),
    ]
    title = conditions[-1]
    assert title.requirement_slot == "application.title"
    assert title.supersedes_source_event_id == initial.id
    # Durable replay derives the same current requirement authority.
    assert dictated_content_conditions_from_events(list(events)) == conditions


def test_revised_release_panel_replaces_superseded_panel_literal():
    events = [
        _user("Add a visible release panel containing 'First revision 98016'.", 1),
        _plan(1, 2),
        _status("plan_approved", 3),
        _user(
            "Recover after the version restore: preserve the imported heading and "
            "add a release panel containing exactly 'Rollback recovered 98016', then verify it.",
            4,
        ),
        _status("planning", 5),
        _plan(2, 6),
    ]

    conditions = dictated_content_conditions_from_events(events)

    assert [(condition.revision, condition.literal) for condition in conditions] == [
        (2, "Rollback recovered 98016"),
    ]
    assert conditions[0].requirement_slot == "component:release:panel"
    assert conditions[0].supersedes_source_event_id == events[0].id


def test_distinct_named_content_slots_coexist_and_each_replaces_its_own_copy():
    events = [
        _user("Add a status banner containing 'Ready'.", 1),
        _plan(1, 2),
        _status("plan_approved", 3),
        _user(
            "Update the status banner to say 'Recovered' and add a release panel "
            "containing 'Restored'.",
            4,
        ),
        _status("planning", 5),
        _plan(2, 6),
    ]

    conditions = dictated_content_conditions_from_events(events)

    assert [(condition.requirement_slot, condition.literal) for condition in conditions] == [
        ("component:status:banner", "Recovered"),
        ("component:release:panel", "Restored"),
    ]
    assert conditions[0].supersedes_source_event_id == events[0].id


def test_direct_target_title_and_bare_title_change_share_identity_slot():
    initial = _user(
        'Build a strict AppKit tracker titled exactly "AppKit Rollback".',
        1,
    )
    replacement = _user(
        "Recover through semantic capabilities, set the title to exactly "
        '"AppKit Recovered", and verify.',
        4,
    )
    events = [
        initial,
        _plan(1, 2),
        _status("plan_approved", 3),
        replacement,
        _status("planning", 5),
        _plan(2, 6),
    ]

    conditions = dictated_content_conditions_from_events(events)

    assert [(condition.revision, condition.literal) for condition in conditions] == [
        (2, "AppKit Recovered"),
    ]
    assert conditions[0].requirement_slot == "application.title"
    assert conditions[0].supersedes_source_event_id == initial.id


def test_additive_content_does_not_replace_application_title():
    events = [
        _user('Build an app titled exactly "Alpha".', 1),
        _plan(1, 2),
        _status("plan_approved", 3),
        _user('Add a heading exactly "Beta".', 4),
        _status("planning", 5),
        _plan(2, 6),
    ]

    conditions = dictated_content_conditions_from_events(events)

    assert [(condition.revision, condition.literal) for condition in conditions] == [
        (1, "Alpha"),
        (2, "Beta"),
    ]
    assert conditions[-1].supersedes_source_event_id is None


def test_ambiguous_application_title_history_fails_closed_on_replacement():
    events = [
        _user(
            'Build an app titled exactly "Alpha" and a site titled exactly "Gamma".',
            1,
        ),
        _plan(1, 2),
        _status("plan_approved", 3),
        _user('Change the app title to exactly "Beta".', 4),
        _status("planning", 5),
        _plan(2, 6),
    ]

    conditions = dictated_content_conditions_from_events(events)

    assert [(condition.revision, condition.literal) for condition in conditions] == [
        (1, "Alpha"),
        (1, "Gamma"),
        (2, "Beta"),
    ]
    assert conditions[-1].supersedes_source_event_id is None


@pytest.mark.parametrize(
    "initial",
    [
        'Build an app with a button called "Launch".',
        'Build an app with a hero titled "Launch".',
        'Build a site with a CTA named "Launch".',
        'Build an application where the first card is called "Launch".',
        'Build a dashboard with a card titled exactly "Launch".',
        'Create a tracker containing a panel named "Launch".',
    ],
)
def test_component_copy_is_not_misclassified_as_application_title(initial):
    events = [
        _user(initial, 1),
        _plan(1, 2),
        _status("plan_approved", 3),
        _user('Change the app title to exactly "Beta".', 4),
        _status("planning", 5),
        _plan(2, 6),
    ]

    conditions = dictated_content_conditions_from_events(events)

    assert [(condition.revision, condition.literal) for condition in conditions] == [
        (1, "Launch"),
        (2, "Beta"),
    ]
    assert conditions[0].requirement_slot is None
    assert conditions[1].requirement_slot == "application.title"
    assert conditions[1].supersedes_source_event_id is None


def test_serve_argument_literals_are_metadata_not_dictated_content():
    text = (
        'Build a hero with heading "Launch Day". Then call the\nserve tool exactly once '
        'with path "/workspace/release" and\ntitle "Selected reliability release".'
    )

    assert extract_dictated_content_literals(text) == ["Launch Day"]
    assert extract_dictated_content_literals(
        'Build a page title "Selected reliability release".'
    ) == ["Selected reliability release"]


def test_serve_file_path_dot_does_not_turn_following_title_into_visible_copy():
    """H069 live-8: punctuation inside the path argument is not a sentence break."""
    text = (
        "Build a normal HTML document with visible h1 text 'SELECTED RELEASE ONE'. "
        "Use ordinary workspace write/shell tools. Verify the files and links. "
        "Then call the\nserve tool exactly once with path 'release/index.html' "
        "(the entry FILE, not release,\ndot, or the workspace root), title "
        "'Selected reliability release', and finish."
    )
    assert extract_dictated_content_literals(text) == ["SELECTED RELEASE ONE"]
    conditions = dictated_content_conditions_from_events(
        [_user(text, 1), _plan(1, 2), _status("plan_approved", 3)]
    )
    assert [condition.literal for condition in conditions] == ["SELECTED RELEASE ONE"]

    # An actual sentence boundary still ends serve metadata scope. The later
    # page title is visible content and must remain a hard finish condition.
    visible_copy = (
        "Call serve with path 'release/index.html'. "
        "Then make the page title 'Selected reliability release'."
    )
    assert extract_dictated_content_literals(visible_copy) == ["Selected reliability release"]


def test_diagnostic_protocol_examples_are_metadata_not_dictated_content():
    text = (
        "DIAGNOSTIC PROTOCOL — for EVERY tool call, FIRST output one line "
        '"PREDICT: <expected result>", and AFTER one line "OBSERVED: match" or '
        '"OBSERVED: mismatch — <what about THE TOOL\'S behavior surprised you>".\n'
        "TASK: Create a server with the h1 'Live Server Up'."
    )

    assert extract_dictated_content_literals(text) == ["Live Server Up"]


_DIAG_DEVSERVER_PROMPT = (
    "DIAGNOSTIC PROTOCOL — for EVERY tool call, FIRST output one line "
    '"PREDICT: <expected result>", and AFTER one line "OBSERVED: match" or '
    '"OBSERVED: mismatch — <what about THE TOOL\'S behavior surprised you>" '
    "(focus on the tool, not your mistakes). One sentence each.\n"
    "TASK: Create a minimal Python HTTP server (server.py using only the standard "
    "library) that serves a page with the h1 'Live Server Up'. The program must read "
    "its assigned port with int(os.environ.get('PORT', '8000')), preserving 8000 only "
    "as the no-environment default. Start it only through preview_start with command "
    "'python3 server.py' (never launch or kill a web server through shell), verify the "
    "returned platform preview in the browser, then finish."
)


def test_dictated_code_argument_literals_are_not_page_content():
    """F42 (counted wave-1 FAIL, `diag_devserver` seed 8, identical-tool-call thrash).

    The prompt dictates the port lookup verbatim. Both call arguments were mined
    as REQUIRED visible-text claims, so the host verifier demanded a page showing
    'PORT' — which no correct implementation of this task can ever render. It
    failed identically, and the model's re-probing was graded as thrash.

    This is the VERBATIM harness prompt (`harness/build_soak/scenarios.yaml`,
    `diag_devserver`). The pre-existing coverage above used an abbreviated form
    that dropped the `os.environ.get(...)` clause, which is exactly why the
    defect survived to a counted cell.
    """

    assert extract_dictated_content_literals(_DIAG_DEVSERVER_PROMPT) == ["Live Server Up"]

    conditions = dictated_content_conditions_from_events(
        [_user(_DIAG_DEVSERVER_PROMPT, 1), _plan(1, 2), _status("plan_approved", 3)]
    )
    assert [condition.literal for condition in conditions] == ["Live Server Up"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # An English parenthetical is NOT a call: the opener is not bound tight
        # to an identifier, so the literal stays dictated page content.
        ("Add a button (labeled 'Sign Up') to the hero.", ["Sign Up"]),
        ("Show the h1 'Live Server Up' on the page.", ["Live Server Up"]),
        # Nested and multi-argument calls: every argument is code, none is copy.
        ("Read the port with int(os.environ.get('PORT', '8000')).", []),
        ("Call render('Home', 'index.html') at startup.", []),
        # A literal AFTER a closed call is page copy again.
        ("Use os.environ.get('PORT') and title the page 'Atlas Home'.", ["Atlas Home"]),
    ],
)
def test_code_argument_discrimination(text: str, expected: list[str]):
    assert extract_dictated_content_literals(text) == expected


@pytest.mark.parametrize(
    ("protocol", "task", "expected"),
    [
        (
            'for EVERY tool call, FIRST output one line "PREDICT: <expected result>", '
            'and AFTER one line "OBSERVED: match" or "OBSERVED: mismatch — <what about '
            "THE TOOL'S behavior surprised you>\".",
            "Write primes.py, run it, and finish.",
            [],
        ),
        (
            'for EVERY tool call, FIRST output one line "PREDICT: <expected result>", '
            'and AFTER one line "OBSERVED: match".',
            "Create an index.html with h1 'Join the Waitlist' and button 'Sign Up'.",
            ["Join the Waitlist", "Sign Up"],
        ),
        (
            'for EVERY tool call, FIRST output one line "PREDICT: <what you expect this '
            'tool call to return/do>", and AFTER the result one line "OBSERVED: match".',
            "Build pages with 'Atlas Home', 'Our Services', and 'Reach Us' sharing '.topnav'.",
            ["Atlas Home", "Our Services", "Reach Us", ".topnav"],
        ),
        (
            "you MUST follow this for EVERY tool call. BEFORE, output a line starting with "
            '"PREDICT:". AFTER, write "OBSERVED: match" or "OBSERVED: mismatch — <why>".',
            "Create an index.html whose h1 is 'Build Smoke OK'.",
            ["Build Smoke OK"],
        ),
    ],
)
def test_build10_diagnostic_protocol_variants_keep_only_task_copy(
    protocol: str,
    task: str,
    expected: list[str],
):
    text = f"DIAGNOSTIC PROTOCOL — {protocol}\nTASK: {task}"
    assert extract_dictated_content_literals(text) == expected


def test_diagnostic_words_remain_dictated_when_requested_as_visible_copy():
    assert extract_dictated_content_literals('Build a page whose h1 says "OBSERVED: match".') == [
        "OBSERVED: match"
    ]
    assert extract_dictated_content_literals(
        "DIAGNOSTIC PROTOCOL — for every tool call, print one line first.\n"
        'TASK: Put "PREDICT: <expected result>" visibly on the page.'
    ) == ["PREDICT: <expected result>"]
    assert extract_dictated_content_literals(
        'DIAGNOSTIC PROTOCOL — output "PREDICT: <expected result>" around tools. '
        'The customer-facing brand must be "Acme Prime".\n'
        "TASK: Build the page."
    ) == ["Acme Prime"]
    assert extract_dictated_content_literals(
        'DIAGNOSTIC PROTOCOL — output "OBSERVED: match" around tools. '
        'COPY REQUIREMENT: h1 exactly "Release Ready".\n'
        "TASK: Build the page."
    ) == ["Release Ready"]


def test_task_marker_without_diagnostic_protocol_does_not_suppress_copy():
    assert extract_dictated_content_literals(
        'Context says "Keep this".\nTASK: Render "And this".'
    ) == ["Keep this", "And this"]
    assert extract_dictated_content_literals(
        'DIAGNOSTIC PROTOCOL — quote "Keep this" without a task section.'
    ) == ["Keep this"]


def test_retitle_supersedes_the_prior_application_title():
    """`retitle` is the exact synonym of `rename` for this slot, and both read
    naturally without a `to`/`as`.

    Before this, "retitle the app exactly 'X'" gave the NEW title no slot while
    the superseded one kept its own — so the old title stayed a required
    identity claim that the retitled app could never satisfy. An app has one
    name, so the two claims were mutually exclusive and the build could not
    finish however correctly the model behaved (Build-soak p4_appkit_restart,
    seed 406431).
    """
    for phrasing in (
        'Use only AppKit semantic capabilities to retitle the app exactly "Beta".',
        'Retitle the app to exactly "Beta".',
        'Retitle the site as exactly "Beta".',
        'Rename the app exactly "Beta".',
    ):
        events = [
            _user('Build a strict AppKit app titled exactly "Alpha".', 1),
            _plan(1, 2),
            _status("plan_approved", 3),
            _user(phrasing, 4),
            _status("planning", 5),
            _plan(2, 6),
        ]
        conditions = dictated_content_conditions_from_events(events)
        assert [(c.revision, c.literal) for c in conditions] == [(2, "Beta")], phrasing
        assert conditions[-1].requirement_slot == "application.title", phrasing


def test_an_unrelated_literal_never_claims_the_title_slot():
    """The replacement syntax must stay narrow: an ordinary quoted string in a
    later revision is content, not a retitle, and must not retire the title."""
    events = [
        _user('Build a strict AppKit app titled exactly "Alpha".', 1),
        _plan(1, 2),
        _status("plan_approved", 3),
        _user('Add a button that says "Beta".', 4),
        _status("planning", 5),
        _plan(2, 6),
    ]
    conditions = dictated_content_conditions_from_events(events)
    assert [(c.revision, c.literal, c.requirement_slot) for c in conditions] == [
        (1, "Alpha", "application.title"),
        (2, "Beta", None),
    ]
