"""REL-RC-O — dictated quoted content: page vs. document scope and scoped edits.

Split out of the former single test_dictated_content_finish_gate.py. Covers
clause-bound document-artifact scoping of quoted literals (a literal assigned
to a written document is not a page/web requirement) and how a scoped edit
directive supersedes the literal it targets. See test_dictated_content_literals.py
for extraction/metadata classification and application-title slot identity, and
test_dictated_content_app_bundle.py for the selected-app entry/bundle inspection
and finish-gate refusal/cap behavior.
"""

from __future__ import annotations

from _dictated_content_support import _plan, _scoped_edit_directive, _status, _user
from disco.core.loop.finish.verify_gate_parts.host_claims import host_verification_claims
from disco.core.loop.plan_conditions import dictated_content_conditions_from_events
from disco.core.verification import VerificationClaimKind


def test_scoped_edit_supersedes_old_literal_without_harvesting_label_text():
    directive = _scoped_edit_directive(human_label='h1 — "NightOwl Coffee"')
    events = [
        _user('Build a hero titled "NightOwl Coffee" and include "Contact us today".', 1),
        _plan(1, 2),
        _status("plan_approved", 3),
        _user(directive, 4),
        _status("planning", 5),
        _plan(2, 6),
    ]

    conditions = dictated_content_conditions_from_events(events)

    assert [(c.revision, c.literal) for c in conditions] == [
        (1, "Contact us today"),
    ]


def test_label_less_scoped_edit_supersedes_no_prior_literal():
    directive = _scoped_edit_directive(
        human_label=None,
        instruction="Make the selected heading shorter.",
    )
    events = [
        _user('Build a hero titled "NightOwl Coffee".', 1),
        _plan(1, 2),
        _status("plan_approved", 3),
        _user(directive, 4),
        _status("planning", 5),
        _plan(2, 6),
    ]

    conditions = dictated_content_conditions_from_events(events)

    assert [(c.revision, c.literal) for c in conditions] == [
        (1, "NightOwl Coffee"),
    ]


def test_a_literal_assigned_to_a_written_document_is_not_a_page_requirement():
    """Epic-4 seed 460009. The instruction assigns its two literals to DIFFERENT
    artifacts — one to a markdown file, one to the served page — but quoted
    literals were collected with no notion of destination, so every one of them
    became a `web.visible_text` claim and the browser verifier demanded the
    REPORT.md heading in the rendered DOM.

    An agent that obeys the instruction then cannot finish: the dossier shows it
    oscillating (seq 143 fails 'Ledger Audit', 205 fails 'Ledger Audited' after
    fixing the first, 245 flips back) until the no-progress detector gave up. The
    runs that passed passed only because they happened to put the report heading
    on the page as well — luck, not correctness.

    Identical in shape to the `application.title` failure one slot over: mutually
    exclusive claims that "the build could never finish however correctly the
    model behaved" (seed 406431).
    """
    events = [
        _user(
            "Create REPORT.md headed exactly 'Ledger Audit 460009' comparing the "
            "ranges. Update index.html to show 'Ledger Audited 460009', serve it, "
            "and browser-verify it.",
            1,
        ),
        _plan(1, 2),
    ]

    conditions = dictated_content_conditions_from_events(events)
    scoped = {c.literal: c.document_artifact for c in conditions}

    assert scoped == {
        "Ledger Audit 460009": "REPORT.md",
        "Ledger Audited 460009": None,
    }


def test_page_scoped_and_unscoped_literals_keep_their_web_requirement():
    """Overhardening control. Scoping must only fire when the user names a
    NON-SERVED document in the literal's own clause. `.html` is the page itself,
    and naming no artifact at all is the common case — both must keep the
    accumulating web requirement they have always had. A false scope would
    DELETE a real verification requirement."""
    events = [
        _user(
            "Create about.html headed 'Our Team' and serve it. Set the site title "
            "to 'My App'. Make the page say 'Hello World'.",
            1,
        ),
        _plan(1, 2),
    ]

    conditions = dictated_content_conditions_from_events(events)

    assert {c.literal for c in conditions} == {"Our Team", "My App", "Hello World"}
    assert all(c.document_artifact is None for c in conditions)


def test_document_scope_is_clause_bound_not_sentence_bound():
    """One sentence routinely assigns literals to two places. A sentence-wide
    scan bound BOTH of these to notes.txt and silently dropped a legitimate page
    requirement, which is the failure mode this scoping exists to prevent —
    inverted. Each literal keeps the artifact ITS OWN clause names."""
    events = [
        _user(
            "Write notes.txt containing 'internal only' and show 'Public View' on the page.",
            1,
        ),
        _plan(1, 2),
    ]

    conditions = dictated_content_conditions_from_events(events)
    scoped = {c.literal: c.document_artifact for c in conditions}

    assert scoped == {"internal only": "notes.txt", "Public View": None}


def test_multifile_requirements_keep_source_surface_and_exact_html_target():
    """The generic host may only judge visible text on the artifact it opened.

    A CSS identifier is source text, not rendered copy, and a heading assigned
    to ``about.html`` is not a requirement on the selected ``index.html`` page.
    Both remain dictated-content conditions for exact workspace inspection.
    """

    prompt = (
        "Create a small multi-page static website with three files: index.html "
        "(the Home page), about.html (the About page), and a shared style.css. "
        "Give index.html an <h1> heading exactly 'Welcome Home' and about.html "
        "an <h1> heading exactly 'About Us'. Both pages must include a navigation "
        "bar using the CSS class 'site-nav' (defined in style.css) with links "
        "between the pages."
    )
    events = [_user(prompt, 1), _plan(1, 2)]

    conditions = dictated_content_conditions_from_events(events)

    assert [
        (condition.literal, condition.content_surface, condition.document_artifact)
        for condition in conditions
    ] == [
        ("Welcome Home", "visible_text", "index.html"),
        ("About Us", "visible_text", "about.html"),
        ("site-nav", "source_text", None),
    ]

    index_claims = host_verification_claims({}, events, artifact_path="index.html")
    index_visible_text = {
        claim.expected
        for claim in index_claims
        if claim.kind is VerificationClaimKind.VISIBLE_TEXT and claim.required
    }
    assert index_visible_text == {"Welcome Home"}

    about_claims = host_verification_claims({}, events, artifact_path="about.html")
    about_visible_text = {
        claim.expected
        for claim in about_claims
        if claim.kind is VerificationClaimKind.VISIBLE_TEXT and claim.required
    }
    assert about_visible_text == {"About Us"}


def test_compact_parenthesized_multifile_requirements_keep_exact_html_target():
    """Common ``page.html (h1 'copy')`` shorthand carries the same ownership."""
    prompt = (
        "Build a 3-page static site — index.html (h1 'Atlas Home'), "
        "services.html (h1 'Our Services'), contact.html (h1 'Reach Us') — "
        "sharing a style.css with a '.topnav' navbar."
    )
    events = [_user(prompt, 1), _plan(1, 2)]

    conditions = dictated_content_conditions_from_events(events)
    assert [(condition.literal, condition.document_artifact) for condition in conditions] == [
        ("Atlas Home", "index.html"),
        ("Our Services", "services.html"),
        ("Reach Us", "contact.html"),
        (".topnav", None),
    ]

    index_claims = host_verification_claims({}, events, artifact_path="index.html")
    required_copy = {
        claim.expected
        for claim in index_claims
        if claim.kind is VerificationClaimKind.VISIBLE_TEXT and claim.required
    }
    assert required_copy == {"Atlas Home"}
