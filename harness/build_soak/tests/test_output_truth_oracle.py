"""OutputTruthOracle unit tests (guidelines §11.5, PR S2)."""

from __future__ import annotations

import pytest

from _eventlog import clean_smoke_log

from harness.build_soak.events import normalize_events
from harness.build_soak.oracles.output_truth import OutputTruthOracle

_SCN = {
    "id": "static_html_minimal",
    "assertions": {
        "workspace": {"files": [{"path": "index.html", "must_contain": ["Build Smoke OK"]}]},
        "terminal_status_in": ["FINISHED", "VERIFIED"],
    },
}


def _run(scenario=_SCN, workspace=None, preview=None, events=None):
    return OutputTruthOracle().check(
        normalize_events(events or clean_smoke_log()),
        scenario=scenario,
        workspace_manifest=workspace,
        preview=preview,
    )


def test_finished_with_required_file_and_content_passes():
    results = _run(workspace={"index.html": "<h1>Build Smoke OK</h1>"})
    assert results[0].passed, results[0].to_dict()


def test_finished_with_missing_file_is_false_finish():
    results = _run(workspace={"other.html": "x"})
    assert results[0].code == "FALSE_FINISH_NO_OUTPUT"
    assert results[0].facts["required_path"] == "index.html"


def test_finished_with_missing_content_is_artifact_mismatch():
    results = _run(workspace={"index.html": "<h1>nope</h1>"})
    assert results[0].code == "ARTIFACT_TRUTH_MISMATCH"
    assert results[0].facts["missing_substring"] == "Build Smoke OK"


def test_no_output_assertion_skips():
    results = _run(scenario={"id": "x", "assertions": {}})
    assert results[0].skipped


# ---- HARNESS-FIX-4 rev2: proof + content-stability precedence fold -----------------------

_SCN2 = {
    "id": "two_files",
    "assertions": {
        "workspace": {
            "files": [
                {"path": "a.html", "must_contain": ["AAA"]},
                {"path": "b.html", "must_contain": ["BBB"]},
            ]
        },
        "terminal_status_in": ["FINISHED"],
    },
}

_CHECK_MATRIX = [
    (
        "must_contain",
        {"must_contain": ["TARGET"]},
        "plain text",
        "missing_substring",
        "TARGET",
    ),
    (
        "must_not_contain",
        {"must_not_contain": ["SECRET"]},
        "has SECRET inside",
        "forbidden_substring",
        "SECRET",
    ),
    (
        "equals",
        {"equals": "TARGET"},
        "different",
        None,
        None,
    ),
]

_AUTHORITY_MATRIX = [
    (
        "authoritative-proof",
        {"proof": "raw_sha", "content_stable": False},
        "ARTIFACT_TRUTH_MISMATCH",
        "raw_sha",
        False,
    ),
    (
        "stable-unproven",
        {"proof": "unproven_extended_stability", "content_stable": True},
        "ARTIFACT_TRUTH_MISMATCH",
        "unproven_extended_stability",
        True,
    ),
    (
        "churning-unproven",
        {"proof": "unproven_extended_stability", "content_stable": False},
        "WORKSPACE_SNAPSHOT_UNVERIFIED",
        "unproven_extended_stability",
        False,
    ),
    (
        "unknown+stable",
        {"proof": "unknown", "content_stable": True},
        "ARTIFACT_TRUTH_MISMATCH",
        "unknown",
        True,
    ),
    (
        "unknown+churning",
        {"proof": "unknown", "content_stable": False},
        "WORKSPACE_SNAPSHOT_UNVERIFIED",
        "unknown",
        False,
    ),
    (
        "legacy-no-field",
        {},
        "ARTIFACT_TRUTH_MISMATCH",
        None,
        False,
    ),
]


def _scenario_for_file_spec(spec):
    return {
        "id": "content_check",
        "assertions": {
            "workspace": {"files": [{"path": "index.html", **spec}]},
            "terminal_status_in": ["FINISHED"],
        },
    }


@pytest.mark.parametrize(
    ("check", "spec", "bad_content", "evidence_key", "evidence_value"), _CHECK_MATRIX
)
@pytest.mark.parametrize(
    ("authority_case", "entry_fields", "expected_code", "expected_proof", "expected_stable"),
    _AUTHORITY_MATRIX,
)
def test_content_mismatch_authority_matrix(
    check,
    spec,
    bad_content,
    evidence_key,
    evidence_value,
    authority_case,
    entry_fields,
    expected_code,
    expected_proof,
    expected_stable,
):
    del authority_case
    ws = {"index.html": {"content": bad_content, **entry_fields}}
    results = _run(scenario=_scenario_for_file_spec(spec), workspace=ws)

    assert results[0].code == expected_code
    mismatch = results[0].facts["mismatches"][0]
    assert mismatch["check"] == check
    assert mismatch["proof"] == expected_proof
    assert mismatch["content_stable"] is expected_stable
    if evidence_key is not None:
        assert mismatch[evidence_key] == evidence_value


def test_mixed_multi_file_authoritative_mismatch_wins():
    # PRECEDENCE: one authoritative mismatch (b.html raw_sha) + one churning-unproven (a.html)
    # → the authoritative regression wins; both are recorded as evidence.
    ws = {
        "a.html": {
            "content": "x",
            "proof": "unproven_extended_stability",
            "content_stable": False,
        },
        "b.html": {"content": "x", "proof": "raw_sha", "content_stable": False},
    }
    results = _run(scenario=_SCN2, workspace=ws)
    assert results[0].code == "ARTIFACT_TRUTH_MISMATCH"
    assert results[0].facts["proven_mismatch_count"] == 1
    assert results[0].facts["unverified_mismatch_count"] == 1
    assert {m["proof"] for m in results[0].facts["mismatches"]} == {
        "unproven_extended_stability",
        "raw_sha",
    }


def test_all_churning_unproven_multi_file_mismatch_is_unverified():
    ws = {
        "a.html": {
            "content": "x",
            "proof": "unproven_extended_stability",
            "content_stable": False,
        },
        "b.html": {"content": "x", "proof": "unknown", "content_stable": False},
    }
    results = _run(scenario=_SCN2, workspace=ws)
    assert results[0].code == "WORKSPACE_SNAPSHOT_UNVERIFIED"
    assert len(results[0].facts["mismatches"]) == 2


def test_stable_unproven_content_present_still_passes():
    # Correct content passes even when sha identity is unproven; stability only promotes
    # mismatches, it does not create a failure on matching content.
    ws = {
        "index.html": {
            "content": "<h1>Build Smoke OK</h1>",
            "proof": "unproven_extended_stability",
            "content_stable": True,
        }
    }
    assert _run(workspace=ws)[0].passed


def test_preview_404_is_false_finish():
    scenario = {
        "id": "p",
        "assertions": {
            "preview": {"required": True, "must_contain": ["Build Smoke OK"]},
            "terminal_status_in": ["FINISHED"],
        },
    }
    results = _run(scenario=scenario, preview={"health": {"status": 404}, "content": ""})
    assert results[0].code == "FALSE_FINISH_PREVIEW_BROKEN"


def test_preview_content_mismatch():
    scenario = {
        "id": "p",
        "assertions": {
            "preview": {"required": True, "must_contain": ["Grand Opening"]},
            "terminal_status_in": ["FINISHED"],
        },
    }
    preview = {"health": {"status": 200}, "content": "<h1>old</h1>"}
    results = _run(scenario=scenario, preview=preview)
    assert results[0].code == "PREVIEW_TRUTH_MISMATCH"


def test_not_finished_with_required_output_fails_closed():
    # migration 2026_06_24_paused_incomplete_not_pass: a run that REQUIRES output but
    # never reached a finished terminal is NOT a "skip into PASS" — it is
    # BUILD_DID_NOT_FINISH (the build did not complete). (Previously this SKIPPED, which
    # let a paused-incomplete run score PASS — surfaced-bugs Bug 8.)
    unfinished = clean_smoke_log()[:-1]  # drop the FINISHED status
    results = _run(workspace={"other.html": "x"}, events=unfinished)
    assert results[0].code == "BUILD_DID_NOT_FINISH"
    assert results[0].facts["terminal_status"] is None


def test_not_finished_without_output_assertion_still_skips():
    # The gate only fires where a finish was actually REQUIRED. A scenario asserting no
    # output truth still SKIPs on a non-finished run (unchanged).
    unfinished = clean_smoke_log()[:-1]
    results = _run(scenario={"id": "x", "assertions": {}}, events=unfinished)
    assert results[0].skipped


# ---- CXT-5: destructive-elision scan over final deliverables ------------------

_SCN_ELISION = {
    "id": "elision",
    "assertions": {
        "workspace": {"files": [{"path": "index.html", "must_contain": ["Build Smoke OK"]}]},
        "terminal_status_in": ["FINISHED", "VERIFIED"],
    },
}

_SCN_ELISION_WAIVED = {
    "id": "elision_waived",
    "assertions": {
        "workspace": {
            "allow_elision": True,
            "files": [{"path": "index.html", "must_contain": ["Build Smoke OK"]}],
        },
        "terminal_status_in": ["FINISHED", "VERIFIED"],
    },
}


def test_destructive_elision_in_deliverable_fails():
    results = _run(
        scenario=_SCN_ELISION,
        workspace={"index.html": "<h1>Build Smoke OK</h1>\n<!-- ...(elided)... -->"},
    )
    assert results[0].code == "DESTRUCTIVE_ELISION", results[0].to_dict()
    assert results[0].facts["path"] == "index.html"


def test_recoverable_marker_in_deliverable_passes():
    # a destructive phrase paired with a recover cue on the same line is fine
    results = _run(
        scenario=_SCN_ELISION,
        workspace={"index.html": "<h1>Build Smoke OK</h1>\n<!-- content omitted — file_read app.js -->"},
    )
    assert results[0].passed, results[0].to_dict()


def test_elision_waiver_allows_marker():
    results = _run(
        scenario=_SCN_ELISION_WAIVED,
        workspace={"index.html": "<h1>Build Smoke OK</h1>\n<!-- truncated for brevity -->"},
    )
    assert results[0].passed, results[0].to_dict()
